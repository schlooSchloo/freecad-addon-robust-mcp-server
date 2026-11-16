"""Binder, cross-body projection and attached datum tools against real FreeCAD.

Fixture with known answers: a box body (20 x 10 x 5 mm pad, body moved to
z = 2) and a cylinder body (radius 3, height 8, body moved to x = 40). The
box's top face is at global z = 7 with area 200 mm².
"""

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools.custom.references import register_references_tools
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration

_SETUP = """
import FreeCAD
import Part

def _rect(sketch, x0, y0, x1, y1):
    points = [FreeCAD.Vector(x0, y0, 0), FreeCAD.Vector(x1, y0, 0),
              FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x0, y1, 0)]
    for index in range(4):
        sketch.addGeometry(Part.LineSegment(points[index], points[(index + 1) % 4]))

doc = FreeCAD.newDocument({doc_name!r})
doc.UndoMode = 1
box = doc.addObject("PartDesign::Body", "BoxBody")
xy = [f for f in box.Origin.OriginFeatures if f.Role == "XY_Plane"][0]
sketch = box.newObject("Sketcher::SketchObject", "BoxSketch")
sketch.AttachmentSupport = [(xy, [""])]
sketch.MapMode = "FlatFace"
_rect(sketch, 0, 0, 20, 10)
pad = box.newObject("PartDesign::Pad", "BoxPad")
pad.Profile = sketch
pad.Length = 5
box.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 2), FreeCAD.Rotation())
cyl_body = doc.addObject("PartDesign::Body", "CylBody")
cyl = cyl_body.newObject("PartDesign::AdditiveCylinder", "Cyl")
cyl.Radius = 3
cyl.Height = 8
cyl_body.Placement = FreeCAD.Placement(FreeCAD.Vector(40, 0, 0), FreeCAD.Rotation())
cyl_xy = [f for f in cyl_body.Origin.OriginFeatures if f.Role == "XY_Plane"][0]
cyl_xz = [f for f in cyl_body.Origin.OriginFeatures if f.Role == "XZ_Plane"][0]
for name, plane in (("CylSketchXY", cyl_xy), ("CylSketchXZ", cyl_xz)):
    s = cyl_body.newObject("Sketcher::SketchObject", name)
    s.AttachmentSupport = [(plane, [""])]
    s.MapMode = "FlatFace"
doc.recompute()

def _find(items, test):
    for index, item in enumerate(items, start=1):
        if test(item):
            return index
    raise RuntimeError("fixture element not found")

shape = pad.Shape
top = _find(shape.Faces, lambda f: abs(f.normalAt(0, 0).z - 1) < 1e-9)
def _is_x_edge(edge, y, z):
    a, b = (v.Point for v in edge.Vertexes)
    return abs(a.y - y) < 1e-9 and abs(b.y - y) < 1e-9 and abs(a.z - z) < 1e-9 \\
        and abs(b.z - z) < 1e-9 and abs(a.x - b.x) > 1
front = _find(shape.Edges, lambda e: _is_x_edge(e, 0, 0))
back = _find(shape.Edges, lambda e: _is_x_edge(e, 10, 0))
back_edge = shape.Edges[back - 1]
tangent = back_edge.tangentAt(back_edge.LastParameter)
end = back_edge.valueAt(back_edge.LastParameter)
_result_ = {{
    "top": "Face%d" % top,
    "front": "Edge%d" % front,
    "back": "Edge%d" % back,
    "back_tangent": [tangent.x, tangent.y, tangent.z],
    "back_end": [end.x, end.y, end.z + 2],
}}
"""


def _close(a: list[float], b: list[float], tolerance: float = 1e-6) -> bool:
    """Return True if two vectors match within the tolerance."""
    return len(a) == len(b) and all(
        abs(x - y) <= tolerance for x, y in zip(a, b, strict=True)
    )


@pytest_asyncio.fixture
async def ref_doc() -> AsyncIterator[dict[str, Any]]:
    """Two bodies with known geometry, the tools, and helpers; closed afterwards."""
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    tools: dict[str, Callable[..., Any]] = {}
    server = MagicMock()

    def tool(**_kwargs: Any) -> Callable[[Any], Any]:
        def register(function: Any) -> Any:
            tools[function.__name__] = function
            return function

        return register

    server.tool = tool

    async def get_bridge() -> XmlRpcBridge:
        return bridge

    register_references_tools(server, get_bridge)
    doc_name = f"McpRefs_{uuid4().hex[:12]}"
    setup = await bridge.execute_python(_SETUP.format(doc_name=doc_name))
    assert setup.success, setup.error_traceback or setup.stderr

    async def run(code: str) -> Any:
        result = await bridge.execute_python(
            f"doc = FreeCAD.getDocument({doc_name!r})\n{code}"
        )
        assert result.success, result.error_traceback or result.stderr
        return result.result

    extra_docs: list[str] = []
    try:
        yield {
            "bridge": bridge,
            "tools": tools,
            "doc": doc_name,
            "run": run,
            "names": setup.result,
            "extra_docs": extra_docs,
        }
    finally:
        try:
            for name in [doc_name, *extra_docs]:
                await bridge.execute_python(
                    f"if {name!r} in FreeCAD.listDocuments():\n"
                    f"    FreeCAD.closeDocument({name!r})\n_result_ = True"
                )
        finally:
            await bridge.disconnect()


async def _object_names(env: dict[str, Any]) -> list[str]:
    """Return the names of every object in the fixture document."""
    return await env["run"]("_result_ = [o.Name for o in doc.Objects]")


# ---------------------------------------------------------------------------
# create_subshape_binder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_binder_of_top_face_in_other_body(ref_doc) -> None:
    """The bound face keeps its global position: area 200 mm², z = 7."""
    env = ref_doc
    top = env["names"]["top"]
    result = await env["tools"]["create_subshape_binder"](
        "CylBody", [f"BoxPad.{top}"], name="TopBinder", doc_name=env["doc"]
    )

    assert result["name"] == "TopBinder"
    assert result["bind_mode"] == "Synchronized"
    assert result["support"] == [
        {"object": "BoxBody", "document": env["doc"], "subnames": [f"BoxPad.{top}"]}
    ]
    shape = result["shape"]
    assert shape["faces"] == 1
    assert shape["area_mm2"] == pytest.approx(200.0)
    assert shape["bound_box"] == {"min": [0.0, 0.0, 7.0], "max": [20.0, 10.0, 7.0]}
    assert result["matches_source_position"] is True
    assert result["body_tip"] == "Cyl"

    state = await env["run"](
        "b = doc.getObject('TopBinder')\n"
        "_result_ = [doc.UndoNames[0], b.getParentGeoFeatureGroup().Name,"
        " b.Shape.BoundBox.XMin]"
    )
    assert state == ["Create binder in CylBody", "CylBody", pytest.approx(-40.0)]
    await env["run"]("doc.undo()\n_result_ = True")
    assert "TopBinder" not in await _object_names(env)


@pytest.mark.asyncio
async def test_synchronized_follows_and_frozen_keeps(ref_doc) -> None:
    """A synchronized binder follows a taller pad; a frozen one stays put."""
    env = ref_doc
    top = env["names"]["top"]
    for name, mode in (("Live", "synchronized"), ("Still", "frozen")):
        await env["tools"]["create_subshape_binder"](
            "CylBody",
            [f"BoxPad.{top}"],
            name=name,
            bind_mode=mode,
            doc_name=env["doc"],
        )
    heights = await env["run"](
        "doc.getObject('BoxPad').Length = 8\ndoc.recompute()\n"
        "_result_ = [doc.getObject(n).Shape.BoundBox.ZMin for n in ('Live', 'Still')]"
    )
    assert heights == [pytest.approx(10.0), pytest.approx(7.0)]


@pytest.mark.asyncio
async def test_whole_object_relative_and_not(ref_doc) -> None:
    """Relative keeps global position; relative=False copies local coordinates."""
    env = ref_doc
    placed = await env["tools"]["create_subshape_binder"](
        "CylBody", ["BoxPad"], doc_name=env["doc"]
    )
    assert placed["shape"]["volume_mm3"] == pytest.approx(1000.0)
    assert placed["shape"]["bound_box"] == {
        "min": [0.0, 0.0, 2.0],
        "max": [20.0, 10.0, 7.0],
    }
    local = await env["tools"]["create_subshape_binder"](
        "CylBody", ["BoxPad"], relative=False, doc_name=env["doc"]
    )
    # In the cylinder body (moved to x = 40), unpositioned: global x 40..60.
    assert local["shape"]["bound_box"] == {
        "min": [40.0, 0.0, 0.0],
        "max": [60.0, 10.0, 5.0],
    }
    assert local["matches_source_position"] is None


@pytest.mark.asyncio
async def test_detached_binder_keeps_shape_without_links(ref_doc) -> None:
    """A detached binder is a static copy: no links, same area."""
    env = ref_doc
    top = env["names"]["top"]
    result = await env["tools"]["create_subshape_binder"](
        "CylBody",
        [f"BoxPad.{top}"],
        name="Copy",
        bind_mode="detached",
        doc_name=env["doc"],
    )
    assert result["bind_mode"] == "Detached"
    assert result["shape"]["area_mm2"] == pytest.approx(200.0)
    support = await env["run"]("_result_ = len(doc.getObject('Copy').Support)")
    assert support == 0


@pytest.mark.asyncio
async def test_binder_links_below_shared_container(ref_doc) -> None:
    """Inside an App::Part the link starts at the source body, not the Part."""
    env = ref_doc
    top = env["names"]["top"]
    await env["run"](
        "part = doc.addObject('App::Part', 'Assembly')\n"
        "part.addObject(doc.getObject('BoxBody'))\n"
        "part.addObject(doc.getObject('CylBody'))\n"
        "part.Placement.Base = FreeCAD.Vector(0, 0, 100)\n"
        "doc.recompute()\n_result_ = True"
    )
    result = await env["tools"]["create_subshape_binder"](
        "CylBody", [f"BoxPad.{top}"], doc_name=env["doc"]
    )
    assert result["support"][0]["object"] == "BoxBody"
    assert result["support"][0]["subnames"] == [f"BoxPad.{top}"]
    assert result["shape"]["bound_box"]["min"] == [0.0, 0.0, 107.0]
    assert result["matches_source_position"] is True


@pytest.mark.asyncio
async def test_bad_element_leaves_nothing(ref_doc) -> None:
    """A missing element is a clear error and no binder is left behind."""
    env = ref_doc
    before = await _object_names(env)
    with pytest.raises(ValueError, match="BoxPad has no Face99"):
        await env["tools"]["create_subshape_binder"](
            "CylBody", ["BoxPad.Face99"], doc_name=env["doc"]
        )
    with pytest.raises(ValueError, match="into itself"):
        await env["tools"]["create_subshape_binder"](
            "CylBody", ["CylBody"], doc_name=env["doc"]
        )
    assert await _object_names(env) == before


@pytest.mark.asyncio
async def test_cross_document_binding(ref_doc, tmp_path: Path) -> None:
    """Other documents bind only once both are saved, at their global position."""
    env = ref_doc
    other = f"McpRefsOther_{uuid4().hex[:12]}"
    env["extra_docs"].append(other)
    await env["bridge"].execute_python(
        f"od = FreeCAD.newDocument({other!r})\n"
        "b = od.addObject('PartDesign::Body', 'OtherBody')\n"
        "b.Placement.Base = FreeCAD.Vector(0, 50, 0)\n"
        "c = b.newObject('PartDesign::AdditiveBox', 'Cube')\n"
        "c.Length = c.Width = c.Height = 4\nod.recompute()\n_result_ = True"
    )
    with pytest.raises(ValueError, match="saved"):
        await env["tools"]["create_subshape_binder"](
            "CylBody", [f"{other}#Cube"], doc_name=env["doc"]
        )
    await env["bridge"].execute_python(
        f"FreeCAD.getDocument({other!r}).saveAs({str(tmp_path / 'other.FCStd')!r})\n"
        f"FreeCAD.getDocument({env['doc']!r}).saveAs("
        f"{str(tmp_path / 'main.FCStd')!r})\n_result_ = True"
    )
    result = await env["tools"]["create_subshape_binder"](
        "CylBody", [f"{other}#Cube"], doc_name=env["doc"]
    )
    assert result["support"][0]["document"] == other
    assert result["shape"]["volume_mm3"] == pytest.approx(64.0)
    assert result["shape"]["bound_box"] == {
        "min": [0.0, 50.0, 0.0],
        "max": [4.0, 54.0, 4.0],
    }


# ---------------------------------------------------------------------------
# project_into_sketch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_box_edge_into_other_body(ref_doc) -> None:
    """The box's front edge lands at x -40..-20 in the cylinder body's sketch."""
    env = ref_doc
    front = env["names"]["front"]
    result = await env["tools"]["project_into_sketch"](
        "CylSketchXY", "BoxPad", [front], doc_name=env["doc"]
    )
    assert result["route"] == "binder"
    binder = result["binder"]
    assert binder["created"] is True
    assert binder["name"] == "BoxPadBinder"
    item = result["projected"][0]
    assert item["geo_ids"] == [-3]
    line = item["geometry"][0]
    assert line["type"] == "LineSegment"
    assert sorted([line["start"], line["end"]]) == [[-40.0, 0.0], [-20.0, 0.0]]
    assert line.get("defining", False) is False

    state = await env["run"](
        "body = doc.getObject('CylBody')\n"
        "_result_ = [[o.Name for o in body.Group], "
        "doc.getObject('BoxPadBinder').Visibility, body.Tip.Name, doc.UndoNames[0]]"
    )
    group, visible, tip, undo = state
    assert group.index("BoxPadBinder") < group.index("CylSketchXY")
    assert visible is False
    assert tip == "Cyl"
    assert undo == "Project BoxPad into CylSketchXY"

    again = await env["tools"]["project_into_sketch"](
        "CylSketchXY", "BoxPad", [front], doc_name=env["doc"]
    )
    assert again["binder"]["created"] is False
    assert again["binder"]["name"] == "BoxPadBinder"
    assert again["already_projected"] == [front]
    assert again["projected"] == []


@pytest.mark.asyncio
async def test_project_face_defining_on_xz_sketch(ref_doc) -> None:
    """The top face (z = 7) projects to a line at sketch y = 7 on an XZ sketch."""
    env = ref_doc
    top, front = env["names"]["top"], env["names"]["front"]
    result = await env["tools"]["project_into_sketch"](
        "CylSketchXZ", "BoxPad", [top, front], defining=True, doc_name=env["doc"]
    )
    face, edge = result["projected"]
    points = sorted(p for g in face["geometry"] for p in (g["start"], g["end"]))
    assert points[0] == [-40.0, 7.0] and points[-1] == [-20.0, 7.0]
    assert all(g.get("defining") is True for g in face["geometry"])
    line = edge["geometry"][0]
    assert sorted([line["start"], line["end"]]) == [[-40.0, 2.0], [-20.0, 2.0]]
    assert result["truncated"] is False
    count = await env["run"]("_result_ = len(doc.getObject('CylSketchXZ').Shape.Edges)")
    assert count >= 1


@pytest.mark.asyncio
async def test_project_same_body_is_direct(ref_doc) -> None:
    """Geometry from the sketch's own body is linked without a binder."""
    env = ref_doc
    before = await _object_names(env)
    result = await env["tools"]["project_into_sketch"](
        "CylSketchXY", "Cyl", ["Edge2"], max_geometry=1, doc_name=env["doc"]
    )
    assert result["route"] == "direct"
    assert result["binder"] is None
    assert result["projected"][0]["linked_as"] == "Cyl.Edge2"
    assert await _object_names(env) == before


@pytest.mark.asyncio
async def test_project_missing_element_is_an_error(ref_doc) -> None:
    """A missing element is refused before anything is made."""
    env = ref_doc
    before = await _object_names(env)
    with pytest.raises(ValueError, match="BoxPad has no Edge99"):
        await env["tools"]["project_into_sketch"](
            "CylSketchXY", "BoxPad", ["Edge99"], doc_name=env["doc"]
        )
    assert await _object_names(env) == before


# ---------------------------------------------------------------------------
# create_attached_datum
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_to_edge_at_end(ref_doc) -> None:
    """At the edge end; normal opposite the edge direction unless reversed."""
    env = ref_doc
    back = env["names"]["back"]
    tangent = env["names"]["back_tangent"]
    plain = await env["tools"]["create_attached_datum"](
        "BoxBody",
        [f"BoxPad.{back}"],
        mode="NormalToEdge",
        path_parameter=1.0,
        doc_name=env["doc"],
    )
    assert _close(plain["position"], env["names"]["back_end"])
    assert _close(plain["normal"], [-c for c in tangent])
    assert plain["in_body"]["position"][2] == pytest.approx(0.0)

    flipped = await env["tools"]["create_attached_datum"](
        "BoxBody",
        [f"BoxPad.{back}"],
        mode="NormalToEdge",
        path_parameter=1.0,
        reverse=True,
        doc_name=env["doc"],
    )
    assert _close(flipped["normal"], tangent)
    undo = await env["run"]("_result_ = doc.UndoNames[0]")
    assert undo == "Create datum plane in BoxBody"


@pytest.mark.asyncio
async def test_flat_face_with_offset_and_rotation(ref_doc) -> None:
    """Offset (1, 2, 3) and pitch 30 on the top face (z = 7 globally)."""
    env = ref_doc
    result = await env["tools"]["create_attached_datum"](
        "BoxBody",
        [f"BoxPad.{env['names']['top']}"],
        mode="FlatFace",
        offset=[1, 2, 3],
        rotation=[0, 30, 0],
        name="Tilted",
        doc_name=env["doc"],
    )
    assert result["name"] == "Tilted"
    assert _close(result["position"], [1.0, 2.0, 10.0])
    assert _close(result["normal"], [0.5, 0.0, 0.866025])


@pytest.mark.asyncio
async def test_invalid_mode_lists_suggestions(ref_doc) -> None:
    """A mode that doesn't fit is refused with the ones that do; nothing made."""
    env = ref_doc
    before = await _object_names(env)
    with pytest.raises(ValueError, match="Modes that fit: FlatFace") as error:
        await env["tools"]["create_attached_datum"](
            "BoxBody",
            [f"BoxPad.{env['names']['top']}"],
            mode="NormalToEdge",
            doc_name=env["doc"],
        )
    assert "best fit: FlatFace" in str(error.value)
    assert await _object_names(env) == before


@pytest.mark.asyncio
async def test_datum_refuses_other_body_and_uses_binder(ref_doc) -> None:
    """Other bodies' geometry is refused; attaching to a binder works."""
    env = ref_doc
    top = env["names"]["top"]
    with pytest.raises(ValueError, match="create_subshape_binder"):
        await env["tools"]["create_attached_datum"](
            "CylBody", [f"BoxPad.{top}"], doc_name=env["doc"]
        )
    await env["tools"]["create_subshape_binder"](
        "CylBody", [f"BoxPad.{top}"], name="TopBinder", doc_name=env["doc"]
    )
    result = await env["tools"]["create_attached_datum"](
        "CylBody", ["TopBinder.Face1"], doc_name=env["doc"]
    )
    assert result["mode"] == "FlatFace"
    assert result["position"][2] == pytest.approx(7.0)
    assert _close([abs(c) for c in result["normal"]], [0.0, 0.0, 1.0])


@pytest.mark.asyncio
async def test_origin_role_line_and_point(ref_doc) -> None:
    """Origin roles use the body's own origin; lines and points work too."""
    env = ref_doc
    plane = await env["tools"]["create_attached_datum"](
        "CylBody", ["XZ_Plane"], offset=[0, 0, 4], doc_name=env["doc"]
    )
    assert plane["attached_to"][0].startswith("XZ_Plane")
    assert _close(plane["position"], [40.0, -4.0, 0.0])
    back = env["names"]["back"]
    line = await env["tools"]["create_attached_datum"](
        "BoxBody", [f"BoxPad.{back}"], kind="line", doc_name=env["doc"]
    )
    assert line["mode"] == "TwoPointLine"
    assert _close([abs(c) for c in line["direction"]], [1.0, 0.0, 0.0])
    point = await env["tools"]["create_attached_datum"](
        "BoxBody",
        [f"BoxPad.{back}"],
        kind="point",
        mode="OnEdge",
        path_parameter=0.25,
        doc_name=env["doc"],
    )
    assert point["position"][1:] == [10.0, 2.0]
    assert point["position"][0] in (pytest.approx(5.0), pytest.approx(15.0))
