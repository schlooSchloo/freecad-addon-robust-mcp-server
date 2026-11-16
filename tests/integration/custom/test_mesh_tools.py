"""create_mesh_from_shape and check_mesh against real FreeCAD, known answers."""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools.custom.mesh import register_mesh_tools
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration

_SETUP = """
import FreeCAD
import Part
import Mesh

doc = FreeCAD.newDocument({doc_name!r})
cube = doc.addObject("Part::Box", "Cube")
cube.Length = cube.Width = cube.Height = 10
sphere = doc.addObject("Part::Sphere", "Ball")
sphere.Radius = 3
placed = doc.addObject("Part::Box", "Placed")
placed.Length, placed.Width, placed.Height = 10, 20, 30
placed.Placement = FreeCAD.Placement(
    FreeCAD.Vector(100, 50, 25), FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), 90)
)
box_a = doc.addObject("Part::Box", "BoxA")
box_b = doc.addObject("Part::Box", "BoxB")
box_b.Placement.Base = FreeCAD.Vector(30, 0, 0)
two = doc.addObject("Part::Compound", "TwoBoxes")
two.Links = [box_a, box_b]

# Broken meshes built with the Mesh API from a cube's 12 triangles.
import MeshPart
good = MeshPart.meshFromShape(
    Shape=Part.makeBox(10, 10, 10), LinearDeflection=0.1, AngularDeflection=0.5
)
pts, facs = good.Topology
triangles = [[(pts[i].x, pts[i].y, pts[i].z) for i in f] for f in facs]
holed = Mesh.Mesh(triangles[1:])
flipped_tris = [t if i != 3 else [t[0], t[2], t[1]] for i, t in enumerate(triangles)]
flipped = Mesh.Mesh(flipped_tris)
inside_out = Mesh.Mesh([[t[0], t[2], t[1]] for t in triangles])
dupe = Mesh.Mesh(triangles + [triangles[0]])
shifted = [[(x + 10, y + 10, z + 10) for x, y, z in t] for t in triangles]
doc.addObject("Mesh::Feature", "Pinched").Mesh = Mesh.Mesh(triangles + shifted)
for name, mesh in (("Holed", holed), ("Flipped", flipped), ("InsideOut", inside_out), ("Dupe", dupe)):
    doc.addObject("Mesh::Feature", name).Mesh = mesh
doc.recompute()
_result_ = True
"""


@pytest.fixture
async def mesh_doc() -> AsyncIterator[tuple[XmlRpcBridge, str, dict[str, Any]]]:
    """Fixture document, bridge, and the registered tool functions."""
    doc_name = f"McpMesh_{uuid4().hex}"
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    registered: dict[str, Any] = {}

    class _Mcp:
        def tool(self, **_kwargs: Any) -> Any:
            return lambda func: registered.setdefault(func.__name__, func)

    async def get_bridge() -> XmlRpcBridge:
        return bridge

    register_mesh_tools(_Mcp(), get_bridge)
    try:
        setup = await bridge.execute_python(_SETUP.format(doc_name=doc_name))
        assert setup.success, setup.error_traceback or setup.stderr
        yield bridge, doc_name, registered
    finally:
        try:
            await bridge.execute_python(
                f"FreeCAD.closeDocument({doc_name!r})\n_result_ = True"
            )
        finally:
            await bridge.disconnect()


async def _state(bridge: XmlRpcBridge, doc_name: str) -> dict[str, Any]:
    result = await bridge.execute_python(
        f"d = FreeCAD.getDocument({doc_name!r})\n"
        "_result_ = {'objects': len(d.Objects), 'undo': d.UndoCount,"
        " 'touched': [o.Name for o in d.Objects if 'Touched' in o.State]}"
    )
    assert result.success, result.error_traceback
    return result.result


class TestCreateMeshFromShape:
    async def test_cube_at_coarse_settings(self, mesh_doc):
        _, doc, tools = mesh_doc
        result = await tools["create_mesh_from_shape"]("Cube", doc_name=doc)
        assert result["facets"] == 12
        assert result["points"] == 8
        assert result["bounds"]["min"] == [0, 0, 0]
        assert result["bounds"]["max"] == [10, 10, 10]
        assert result["settings"]["linear_deflection"] == 0.1
        assert "static snapshot" in result["note"].lower()
        report = await tools["check_mesh"](result["mesh"], doc_name=doc)
        assert report["signed_volume_mm3"] == pytest.approx(1000, abs=1e-3)

    async def test_one_undo_step_and_label(self, mesh_doc):
        bridge, doc, tools = mesh_doc
        before = await _state(bridge, doc)
        result = await tools["create_mesh_from_shape"]("Cube", doc_name=doc)
        after = await _state(bridge, doc)
        assert after["objects"] == before["objects"] + 1
        assert after["undo"] == before["undo"] + 1
        assert "Cube" in result["label"] and "0.1" in result["label"]
        await bridge.execute_python(f"FreeCAD.getDocument({doc!r}).undo()")
        assert (await _state(bridge, doc))["objects"] == before["objects"]

    async def test_sphere_facets_grow_with_finer_deflection(self, mesh_doc):
        _, doc, tools = mesh_doc
        coarse = await tools["create_mesh_from_shape"](
            "Ball", linear_deflection=0.5, doc_name=doc
        )
        fine = await tools["create_mesh_from_shape"](
            "Ball", linear_deflection=0.01, angular_deflection=10, doc_name=doc
        )
        assert fine["facets"] > coarse["facets"] * 2
        report = await tools["check_mesh"](fine["mesh"], doc_name=doc)
        exact = 4 / 3 * 3.14159265 * 27
        assert report["signed_volume_mm3"] == pytest.approx(exact, rel=0.02)
        assert report["signed_volume_mm3"] < exact  # inscribed polyhedron
        assert report["is_closed"] and report["components"] == 1

    async def test_placement_is_global_and_feature_placement_identity(self, mesh_doc):
        bridge, doc, tools = mesh_doc
        result = await tools["create_mesh_from_shape"]("Placed", doc_name=doc)
        # 10x20x30 box rotated 90 deg about Z at (100,50,25): x in [80,100],
        # y in [50,60], z in [25,55].
        assert result["bounds"]["min"] == pytest.approx([80, 50, 25], abs=1e-4)
        assert result["bounds"]["max"] == pytest.approx([100, 60, 55], abs=1e-4)
        check = await bridge.execute_python(
            f"o = FreeCAD.getDocument({doc!r}).getObject({result['mesh']!r})\n"
            "_result_ = {'base': list(o.Placement.Base),"
            " 'angle': o.Placement.Rotation.Angle,"
            " 'box': [o.Mesh.BoundBox.XMin, o.Mesh.BoundBox.YMax]}"
        )
        assert check.result["base"] == [0, 0, 0]
        assert check.result["angle"] == 0
        assert check.result["box"] == pytest.approx([80, 60], abs=1e-4)

    async def test_custom_name_and_relative(self, mesh_doc):
        _, doc, tools = mesh_doc
        result = await tools["create_mesh_from_shape"](
            "Cube",
            name="CubeSnapshot",
            relative=True,
            linear_deflection=0.1,
            doc_name=doc,
        )
        assert result["mesh"] == "CubeSnapshot"
        assert result["settings"]["relative"] is True

    async def test_errors_leave_document_unchanged(self, mesh_doc):
        bridge, doc, tools = mesh_doc
        before = await _state(bridge, doc)
        with pytest.raises(ValueError, match="not found"):
            await tools["create_mesh_from_shape"]("Nope", doc_name=doc)
        with pytest.raises(ValueError, match="greater than 0"):
            await tools["create_mesh_from_shape"](
                "Cube", linear_deflection=0, doc_name=doc
            )
        assert await _state(bridge, doc) == before

    async def test_no_faces_is_refused(self, mesh_doc):
        bridge, doc, tools = mesh_doc
        await bridge.execute_python(
            f"d = FreeCAD.getDocument({doc!r})\n"
            "import Part\n"
            "f = d.addObject('Part::Feature', 'Wire')\n"
            "f.Shape = Part.makeLine((0,0,0),(1,0,0))\n_result_ = True"
        )
        before = await _state(bridge, doc)
        with pytest.raises(ValueError, match="no faces"):
            await tools["create_mesh_from_shape"]("Wire", doc_name=doc)
        assert await _state(bridge, doc) == before


class TestCheckMesh:
    async def test_cube_shape_is_closed_single_component(self, mesh_doc):
        bridge, doc, tools = mesh_doc
        before = await _state(bridge, doc)
        report = await tools["check_mesh"]("Cube", doc_name=doc)
        assert report["source_kind"] == "shape_tessellation"
        assert report["facets"] == 12
        assert report["signed_volume_mm3"] == pytest.approx(1000, abs=1e-3)
        assert report["area_mm2"] == pytest.approx(600, abs=1e-3)
        assert report["components"] == 1
        assert (
            report["is_closed"] and report["is_solid"] and report["consistent_winding"]
        )
        assert report["volume_valid"] and not report["inverted"]
        assert report["open_edges"] == 0 and report["nonmanifold_edges"] == 0
        assert report["issues"] == []
        assert report["tolerances"]["point_merge_mm"] == 1e-4
        assert await _state(bridge, doc) == before  # read-only

    async def test_two_boxes_are_two_components(self, mesh_doc):
        _, doc, tools = mesh_doc
        report = await tools["check_mesh"]("TwoBoxes", doc_name=doc)
        assert report["components"] == 2
        assert report["component_facets"] == [12, 12]
        assert report["is_closed"]
        assert report["signed_volume_mm3"] == pytest.approx(2000, abs=1e-3)
        assert any("2 separate components" in issue for issue in report["issues"])

    async def test_removed_facet_has_open_edges(self, mesh_doc):
        _, doc, tools = mesh_doc
        report = await tools["check_mesh"]("Holed", doc_name=doc)
        assert report["source_kind"] == "mesh"
        assert report["facets"] == 11
        assert report["open_edges"] == 3
        assert not report["is_closed"] and not report["is_solid"]
        assert report["volume_valid"] is False
        assert "volume_note" in report
        assert any("fill holes" in r for r in report["suggested_repairs"])

    async def test_flipped_facet_is_inconsistent_winding(self, mesh_doc):
        _, doc, tools = mesh_doc
        report = await tools["check_mesh"]("Flipped", doc_name=doc)
        assert report["consistent_winding"] is False
        assert report["inconsistent_orientation_edges"] == 3
        assert report["is_closed"] is True
        assert report["is_solid"] is False
        assert any("harmonize" in r for r in report["suggested_repairs"])

    async def test_inside_out_mesh_has_negative_volume(self, mesh_doc):
        _, doc, tools = mesh_doc
        report = await tools["check_mesh"]("InsideOut", doc_name=doc)
        assert report["consistent_winding"] is True
        assert report["signed_volume_mm3"] == pytest.approx(-1000, abs=1e-3)
        assert report["inverted"] is True
        assert any("flip normals" in r for r in report["suggested_repairs"])

    async def test_duplicated_facet_is_reported(self, mesh_doc):
        _, doc, tools = mesh_doc
        report = await tools["check_mesh"]("Dupe", doc_name=doc)
        assert report["duplicated_facets"] == 1
        assert report["nonmanifold_edges"] == 3

    async def test_cubes_touching_at_a_corner_have_a_non_manifold_point(self, mesh_doc):
        _, doc, tools = mesh_doc
        report = await tools["check_mesh"]("Pinched", doc_name=doc)
        assert report["nonmanifold_points"] == 1
        assert report["nonmanifold_edges"] == 0
        assert report["is_solid"] is False
        assert report["signed_volume_mm3"] == pytest.approx(2000, abs=1e-3)

    async def test_self_intersections_optional(self, mesh_doc):
        _, doc, tools = mesh_doc
        plain = await tools["check_mesh"]("Cube", doc_name=doc)
        assert plain["self_intersections"] is None
        checked = await tools["check_mesh"](
            "Cube", check_self_intersections=True, doc_name=doc
        )
        assert checked["self_intersections"]["has_self_intersections"] is False

    async def test_mesh_feature_placement_is_respected(self, mesh_doc):
        bridge, doc, tools = mesh_doc
        await bridge.execute_python(
            f"d = FreeCAD.getDocument({doc!r})\n"
            "d.getObject('Holed').Placement = FreeCAD.Placement("
            "FreeCAD.Vector(5, 0, 0), FreeCAD.Rotation())\n_result_ = True"
        )
        report = await tools["check_mesh"]("Holed", doc_name=doc)
        assert report["bounds"]["min"][0] == pytest.approx(5)

    async def test_bad_arguments(self, mesh_doc):
        _, doc, tools = mesh_doc
        with pytest.raises(ValueError, match="tolerance"):
            await tools["check_mesh"]("Cube", tolerance=0, doc_name=doc)
        with pytest.raises(ValueError, match="not found"):
            await tools["check_mesh"]("Nope", doc_name=doc)
