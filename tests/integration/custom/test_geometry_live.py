"""get_shape_geometry and get_section_outline against real FreeCAD.

Every fixture has a known answer: a 10 mm cube, a radius-3 cylinder, a
radius-4 sphere, a cone, a tube, a B-spline, a moved and rotated box, a
PartDesign body with a datum plane, and a link. The tests also check that
the tools leave the document untouched.
"""

import math
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools.custom.geometry import register_geometry_tools
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration

_SETUP = """
import FreeCAD
import Part

doc = FreeCAD.newDocument({doc_name!r})
box = doc.addObject("Part::Box", "Box")
box.Length = box.Width = box.Height = 10
cyl = doc.addObject("Part::Cylinder", "Cyl")
cyl.Radius = 3
cyl.Height = 10
sph = doc.addObject("Part::Sphere", "Sph")
sph.Radius = 4
sph.Placement.Base = FreeCAD.Vector(20, 0, 0)
cone = doc.addObject("Part::Cone", "Cone")
cone.Radius1 = 5
cone.Radius2 = 2
cone.Height = 10
tube = doc.addObject("Part::Feature", "Tube")
tube.Shape = Part.makeCylinder(5, 10).cut(Part.makeCylinder(2, 10))
curve = Part.BSplineCurve()
curve.interpolate([FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(5, 3, 0),
                   FreeCAD.Vector(10, 0, 0)])
spline = doc.addObject("Part::Feature", "Spline")
spline.Shape = curve.toShape()
sketch = doc.addObject("Sketcher::SketchObject", "Sketch")
sketch.Placement = FreeCAD.Placement(
    FreeCAD.Vector(0, 0, 10), FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 90))
sketch.addGeometry(Part.Circle(FreeCAD.Vector(3, 4, 0), FreeCAD.Vector(0, 0, 1), 2))
sketch.addGeometry(curve)
moved = doc.addObject("Part::Box", "Moved")
moved.Length, moved.Width, moved.Height = 10, 20, 30
moved.Placement = FreeCAD.Placement(
    FreeCAD.Vector(5, 6, 7), FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), 90))
link = doc.addObject("App::Link", "BoxLink")
link.LinkedObject = box
link.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 50, 0), FreeCAD.Rotation())

body = doc.addObject("PartDesign::Body", "Body")
body.Placement = FreeCAD.Placement(FreeCAD.Vector(100, 0, 0), FreeCAD.Rotation())
xy = [f for f in body.Origin.OriginFeatures if f.Role == "XY_Plane"][0]
profile = body.newObject("Sketcher::SketchObject", "Profile")
profile.AttachmentSupport = [(xy, [""])]
profile.MapMode = "FlatFace"
corners = [FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(10, 0, 0),
           FreeCAD.Vector(10, 10, 0), FreeCAD.Vector(0, 10, 0)]
for index in range(4):
    profile.addGeometry(Part.LineSegment(corners[index], corners[(index + 1) % 4]))
pad = body.newObject("PartDesign::Pad", "Pad")
pad.Profile = profile
pad.Length = 5
datum = body.newObject("PartDesign::Plane", "DatumPlane")
datum.AttachmentSupport = [(xy, [""])]
datum.MapMode = "FlatFace"
datum.AttachmentOffset = FreeCAD.Placement(FreeCAD.Vector(0, 0, 2), FreeCAD.Rotation())
asm = doc.addObject("App::Part", "Asm")
asm.Placement.Base = FreeCAD.Vector(0, 0, 100)
inner = doc.addObject("PartDesign::Body", "InnerBody")
asm.addObject(inner)
inner.Placement.Base = FreeCAD.Vector(10, 0, 0)
cube = inner.newObject("PartDesign::AdditiveBox", "Cube")
socket = doc.addObject("Part::Feature", "Socket")
socket.Shape = Part.makeBox(20, 20, 20).cut(
    Part.makeSphere(6, FreeCAD.Vector(10, 10, 20)))
many = doc.addObject("Part::Feature", "Many")
many.Shape = Part.makeCompound([
    Part.makeBox(1, 1, 1, FreeCAD.Vector(2 * i, 2 * j, 0))
    for i in range(15) for j in range(15)])
doc.recompute()
_result_ = len(doc.Objects)
"""

_STATE = """
doc = FreeCAD.getDocument({doc_name!r})
_result_ = {{
    "objects": len(doc.Objects),
    "touched": bool(doc.isTouched()),
    "undo": doc.UndoCount,
    "touched_objects": [o.Name for o in doc.Objects if "Touched" in o.State],
}}
"""


class _CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            self.tools[function.__name__] = function
            return function

        return decorator


@pytest.fixture
async def geometry() -> AsyncIterator[tuple[dict[str, Any], str, XmlRpcBridge]]:
    """Build the known-answer document; yield the tools; close it afterwards."""
    doc_name = f"McpGeometry_{uuid4().hex}"
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()

    async def get_bridge() -> XmlRpcBridge:
        return bridge

    mcp = _CapturingMCP()
    register_geometry_tools(mcp, get_bridge)
    try:
        setup = await bridge.execute_python(_SETUP.format(doc_name=doc_name))
        assert setup.success, setup.error_traceback or setup.stderr
        before = await bridge.execute_python(_STATE.format(doc_name=doc_name))
        assert before.success, before.error_traceback
        yield mcp.tools, doc_name, bridge
        after = await bridge.execute_python(_STATE.format(doc_name=doc_name))
        assert after.success, after.error_traceback
        assert after.result == before.result, "the geometry tools changed the document"
    finally:
        try:
            await bridge.execute_python(
                f"FreeCAD.closeDocument({doc_name!r})\n_result_ = True"
            )
        finally:
            await bridge.disconnect()


def _close(actual: list[float], expected: list[float], tol: float = 1e-6) -> bool:
    return all(abs(a - e) <= tol for a, e in zip(actual, expected, strict=True))


@pytest.mark.asyncio
async def test_box_summary_edges_and_faces(geometry) -> None:
    """A 10 mm cube: counts, volume, centre, plane normals and paging."""
    tools, doc_name, _ = geometry
    result = await tools["get_shape_geometry"]("Box", doc_name=doc_name, max_items=5)

    summary = result["summary"]
    assert result["frame"] == "global"
    assert result["needs_recompute"] is False
    assert summary["shape_type"] == "Solid"
    assert summary["is_valid"] is True
    assert summary["is_closed"] is True
    assert (summary["face_count"], summary["edge_count"]) == (6, 12)
    assert summary["vertex_count"] == 8
    assert summary["volume"] == pytest.approx(1000.0)
    assert summary["area"] == pytest.approx(600.0)
    assert summary["center_of_mass"] == pytest.approx([5.0, 5.0, 5.0])
    assert summary["bound_box"]["size"] == pytest.approx([10.0, 10.0, 10.0])
    assert result["edges"]["total"] == 12
    assert result["edges"]["returned"] == 5
    assert result["edges"]["truncated"] is True
    assert "vertices" not in result
    faces = result["faces"]["items"]
    assert result["faces"]["truncated"] is True
    for face in faces:
        assert face["surface_type"] == "Plane"
        assert face["area"] == pytest.approx(100.0)
        # Outward normals: from the cube centre towards the face centre.
        outward = [c - 5.0 for c in face["center_of_mass"]]
        assert _close(face["normal"], [v / 5.0 for v in outward])
        assert len(face["edges"]) == 4
    for edge in result["edges"]["items"]:
        assert edge["curve_type"] == "Line"
        assert edge["length"] == pytest.approx(10.0)
        assert len(edge["vertices"]) == 2

    page = await tools["get_shape_geometry"](
        "Box", doc_name=doc_name, include=["edges"], max_items=5, offset=10
    )
    assert "summary" not in page
    assert page["edges"]["returned"] == 2
    assert page["edges"]["truncated"] is False
    assert [e["name"] for e in page["edges"]["items"]] == ["Edge11", "Edge12"]


@pytest.mark.asyncio
async def test_listed_elements_only(geometry) -> None:
    """Named elements are returned in the sections for their types."""
    tools, doc_name, _ = geometry
    result = await tools["get_shape_geometry"](
        "Box", elements=["Edge3", "Face2", "Vertex1"], doc_name=doc_name
    )

    assert [e["name"] for e in result["edges"]["items"]] == ["Edge3"]
    assert [f["name"] for f in result["faces"]["items"]] == ["Face2"]
    assert result["vertices"]["items"] == [
        {"name": "Vertex1", "point": [0.0, 0.0, 10.0]}
    ]
    assert result["faces"]["items"][0]["normal"] == [1.0, 0.0, 0.0]
    assert "summary" in result

    with pytest.raises(ValueError, match="Face99 does not exist: the shape has 6"):
        await tools["get_shape_geometry"]("Box", elements=["Face99"], doc_name=doc_name)


@pytest.mark.asyncio
async def test_cylinder_sphere_cone_and_tube(geometry) -> None:
    """Curved faces report axis, centre, radius, apex and which way they face."""
    tools, doc_name, _ = geometry
    cyl = await tools["get_shape_geometry"]("Cyl", include=["faces"], doc_name=doc_name)
    side = next(f for f in cyl["faces"]["items"] if f["surface_type"] == "Cylinder")
    assert side["radius"] == pytest.approx(3.0)
    assert [abs(v) for v in side["axis"]] == [0.0, 0.0, 1.0]
    assert side["axis_point"][:2] == [0.0, 0.0]
    assert side["facing"] == "outward"
    assert side["area"] == pytest.approx(2 * math.pi * 3 * 10, abs=1e-5)

    sph = await tools["get_shape_geometry"]("Sph", doc_name=doc_name)
    face = sph["faces"]["items"][0]
    assert face["surface_type"] == "Sphere"
    assert face["center"] == [20.0, 0.0, 0.0]
    assert face["radius"] == 4.0
    assert face["facing"] == "outward"
    assert sph["summary"]["volume"] == pytest.approx(4 / 3 * math.pi * 64, abs=1e-5)
    assert sph["summary"]["center_of_mass"] == pytest.approx([20.0, 0.0, 0.0])

    cone = await tools["get_shape_geometry"](
        "Cone", include=["faces"], doc_name=doc_name
    )
    conical = next(f for f in cone["faces"]["items"] if f["surface_type"] == "Cone")
    assert conical["apex"] == pytest.approx([0.0, 0.0, 50 / 3], abs=1e-5)
    assert abs(conical["semi_angle_deg"]) == pytest.approx(
        math.degrees(math.atan(3 / 10)), abs=1e-5
    )
    assert conical["facing"] == "outward"

    tube = await tools["get_shape_geometry"](
        "Tube", include=["faces"], doc_name=doc_name
    )
    radii = {
        f["radius"]: f["facing"]
        for f in tube["faces"]["items"]
        if f["surface_type"] == "Cylinder"
    }
    assert radii == {5.0: "outward", 2.0: "inward"}


@pytest.mark.asyncio
async def test_bspline_edges_and_samples(geometry) -> None:
    """B-spline edges give degree, poles and sample points on the curve."""
    tools, doc_name, bridge = geometry
    by_count = await tools["get_shape_geometry"]("Spline", samples=5, doc_name=doc_name)
    edge = by_count["edges"]["items"][0]
    assert edge["curve_type"] == "BSplineCurve"
    assert (edge["degree"], edge["pole_count"]) == (2, 3)
    assert edge["start"] == [0.0, 0.0, 0.0]
    assert edge["end"] == [10.0, 0.0, 0.0]
    assert len(edge["samples"]) == 5
    assert edge["samples"][0] == [0.0, 0.0, 0.0]
    assert edge["samples"][-1] == [10.0, 0.0, 0.0]
    # An interpolating spline through three points passes through the middle
    # one, at the middle of its parameter range here (symmetric points).
    assert edge["samples"][2] == pytest.approx([5.0, 3.0, 0.0], abs=1e-6)
    assert by_count["summary"]["volume"] is None

    fine = await tools["get_shape_geometry"](
        "Spline", include=["edges"], sample_tolerance=0.01, doc_name=doc_name
    )
    points = fine["edges"]["items"][0]["samples"]
    assert len(points) > 5
    check = await bridge.execute_python(
        "import FreeCAD, Part\n"
        f"edge = FreeCAD.getDocument({doc_name!r}).Spline.Shape.Edges[0]\n"
        f"points = {points!r}\n"
        "_result_ = max(edge.distToShape(Part.Vertex(FreeCAD.Vector(*p)))[0]"
        " for p in points)\n"
    )
    assert check.success, check.error_traceback
    assert check.result < 1e-5

    sketch = await tools["get_shape_geometry"](
        "Sketch", include=["edges"], doc_name=doc_name
    )
    types = {e["curve_type"] for e in sketch["edges"]["items"]}
    assert types == {"Circle", "BSplineCurve"}


@pytest.mark.asyncio
async def test_local_frame(geometry) -> None:
    """Coordinates in another object's frame undo that object's placement."""
    tools, doc_name, _ = geometry
    glob = await tools["get_shape_geometry"](
        "Moved", include=["summary"], doc_name=doc_name
    )
    # Rotated 90 degrees about Z: 10 x 20 becomes 20 x 10, shifted to (5, 6, 7).
    assert glob["summary"]["bound_box"]["min"] == pytest.approx([-15.0, 6.0, 7.0])
    assert glob["summary"]["bound_box"]["size"] == pytest.approx([20.0, 10.0, 30.0])

    local = await tools["get_shape_geometry"](
        "Moved", include=["summary"], frame="Moved", doc_name=doc_name
    )
    assert local["frame"]["object"] == "Moved"
    assert local["frame"]["position"] == [5.0, 6.0, 7.0]
    assert local["frame"]["rotation_angle_deg"] == pytest.approx(90.0)
    assert local["summary"]["bound_box"]["min"] == pytest.approx([0.0, 0.0, 0.0])
    assert local["summary"]["bound_box"]["max"] == pytest.approx([10.0, 20.0, 30.0])
    assert local["summary"]["center_of_mass"] == pytest.approx([5.0, 10.0, 15.0])

    sketch = await tools["get_shape_geometry"](
        "Sketch", include=["edges"], doc_name=doc_name
    )
    circle = next(e for e in sketch["edges"]["items"] if e["curve_type"] == "Circle")
    # Sketch (3, 4) on a plane rotated 90 degrees about X and lifted to z=10.
    assert circle["center"] == pytest.approx([3.0, 0.0, 14.0])
    in_sketch = await tools["get_shape_geometry"](
        "Sketch", include=["edges"], frame="Sketch", doc_name=doc_name
    )
    circle = next(e for e in in_sketch["edges"]["items"] if e["curve_type"] == "Circle")
    assert circle["center"] == pytest.approx([3.0, 4.0, 0.0])
    assert circle["radius"] == pytest.approx(2.0)
    assert circle["axis"] == pytest.approx([0.0, 0.0, 1.0])


@pytest.mark.asyncio
async def test_body_feature_link_and_datum(geometry) -> None:
    """Bodies, their features and links are reported in global coordinates."""
    tools, doc_name, _ = geometry
    for name in ("Body", "Pad"):
        result = await tools["get_shape_geometry"](
            name, include=["summary"], doc_name=doc_name
        )
        box = result["summary"]["bound_box"]
        assert box["min"] == pytest.approx([100.0, 0.0, 0.0]), name
        assert box["max"] == pytest.approx([110.0, 10.0, 5.0]), name
        assert result["summary"]["volume"] == pytest.approx(500.0)

    in_body = await tools["get_shape_geometry"](
        "Pad", include=["summary"], frame="Body", doc_name=doc_name
    )
    assert in_body["summary"]["bound_box"]["min"] == pytest.approx([0.0, 0.0, 0.0])

    link = await tools["get_shape_geometry"](
        "BoxLink", include=["summary"], doc_name=doc_name
    )
    assert link["summary"]["bound_box"]["min"] == pytest.approx([0.0, 50.0, 0.0])
    assert link["summary"]["volume"] == pytest.approx(1000.0)

    datum = await tools["get_shape_geometry"]("DatumPlane", doc_name=doc_name)
    assert datum["summary"]["unbounded"] is True
    assert datum["summary"]["area"] is None
    assert datum["summary"]["bound_box"] is None
    face = datum["faces"]["items"][0]
    assert face["area"] is None
    assert face["unbounded"] is True
    assert [abs(v) for v in face["normal"]] == [0.0, 0.0, 1.0]
    assert face["position"] == pytest.approx([100.0, 0.0, 2.0])


@pytest.mark.asyncio
async def test_section_of_box_and_sphere(geometry) -> None:
    """A cube cut at z=5 has area 100; a sphere cut through its centre is a circle."""
    tools, doc_name, _ = geometry
    result = await tools["get_section_outline"](
        "Box", plane="XY", offsets=[5.0, 20.0], doc_name=doc_name
    )
    hit, miss = result["sections"]
    assert hit["hit"] is True
    assert hit["closed_wire_count"] == 1
    assert hit["area"] == pytest.approx(100.0)
    wire = hit["wires"][0]
    assert wire["enclosed_area"] == pytest.approx(100.0)
    assert wire["length"] == pytest.approx(40.0)
    edges = wire["edges"]
    assert len(edges) == 4
    for previous, current in zip(edges, edges[1:] + edges[:1], strict=True):
        assert previous["end"] == pytest.approx(current["start"])
        assert previous["start"][2] == pytest.approx(5.0)
    assert miss["hit"] is False
    assert miss["area"] == 0.0
    assert "does not cut" in miss["note"]
    assert result["plane"]["shape_offset_range"] == [0.0, 10.0]

    sphere = await tools["get_section_outline"](
        "Sph",
        origin=[20.0, 0.0, 0.0],
        normal=[1.0, 0.0, 0.0],
        offsets=[0.0, 3.0],
        doc_name=doc_name,
    )
    centre, off_centre = sphere["sections"]
    radii = {e["radius"] for w in centre["wires"] for e in w["edges"]}
    assert radii == {4.0}
    assert centre["area"] == pytest.approx(math.pi * 16, abs=1e-5)
    assert {e["center"][0] for w in centre["wires"] for e in w["edges"]} == {20.0}
    assert off_centre["area"] == pytest.approx(math.pi * 7, abs=1e-5)


@pytest.mark.asyncio
async def test_section_holes_datum_plane_and_frame(geometry) -> None:
    """Holes are subtracted; datum planes and frames are honoured."""
    tools, doc_name, _ = geometry
    tube = await tools["get_section_outline"](
        "Tube", plane="XY", offsets=[5.0], doc_name=doc_name
    )
    section = tube["sections"][0]
    assert section["closed_wire_count"] == 2
    assert sorted(w["enclosed_area"] for w in section["wires"]) == pytest.approx(
        [math.pi * 4, math.pi * 25], abs=1e-5
    )
    assert section["area"] == pytest.approx(math.pi * 21, abs=1e-5)

    datum = await tools["get_section_outline"](
        "Pad", plane="DatumPlane", offsets=[0.0, 1.0], doc_name=doc_name
    )
    assert datum["plane"]["origin"] == pytest.approx([100.0, 0.0, 2.0])
    assert [s["area"] for s in datum["sections"]] == pytest.approx([100.0, 100.0])
    assert datum["sections"][1]["plane_point"][2] == pytest.approx(3.0)

    local = await tools["get_section_outline"](
        "Pad", plane="DatumPlane", frame="DatumPlane", doc_name=doc_name
    )
    points = [e["start"] for e in local["sections"][0]["wires"][0]["edges"]]
    assert all(abs(p[2]) < 1e-6 for p in points)
    assert sorted({p[0] for p in points}) == pytest.approx([0.0, 10.0])
    assert local["plane"]["normal"] == pytest.approx([0.0, 0.0, 1.0])

    capped = await tools["get_section_outline"](
        "Box", plane="XY", offsets=[2.0, 4.0], max_edges=6, doc_name=doc_name
    )
    assert capped["truncated"] is True
    assert [s["edges_returned"] for s in capped["sections"]] == [4, 2]
    assert [s["area"] for s in capped["sections"]] == pytest.approx([100.0, 100.0])


@pytest.mark.asyncio
async def test_socket_and_nested_body(geometry) -> None:
    """A spherical socket faces inward; nested placements are all applied."""
    tools, doc_name, _ = geometry
    socket = await tools["get_shape_geometry"](
        "Socket", include=["faces"], doc_name=doc_name
    )
    sphere = next(f for f in socket["faces"]["items"] if f["surface_type"] == "Sphere")
    assert sphere["facing"] == "inward"
    assert sphere["center"] == [10.0, 10.0, 20.0]
    assert sphere["radius"] == 6.0

    # Cut 3 mm below the sphere centre: a circle of radius sqrt(36 - 9).
    cut = await tools["get_section_outline"](
        "Socket", plane="XY", offsets=[17.0], doc_name=doc_name
    )
    section = cut["sections"][0]
    assert section["closed_wire_count"] == 2
    assert section["area"] == pytest.approx(400 - math.pi * 27, abs=1e-5)

    cube = await tools["get_shape_geometry"](
        "Cube", include=["summary"], doc_name=doc_name
    )
    assert cube["summary"]["bound_box"]["min"] == pytest.approx([10.0, 0.0, 100.0])
    assert cube["summary"]["bound_box"]["max"] == pytest.approx([20.0, 10.0, 110.0])


@pytest.mark.asyncio
async def test_large_listing_stays_within_result_limit(geometry) -> None:
    """A big listing is cut short and flagged rather than failing."""
    tools, doc_name, _ = geometry
    result = await tools["get_shape_geometry"](
        "Many",
        include=["summary", "vertices", "edges", "faces"],
        max_items=1000,
        doc_name=doc_name,
    )

    assert result["summary"]["face_count"] == 1350
    assert "size_note" in result
    for section in ("vertices", "edges", "faces"):
        assert result[section]["returned"] > 100, section
    assert result["faces"]["truncated"] is True
    assert result["faces"]["returned"] < 1000

    section = await tools["get_section_outline"](
        "Many", plane="XY", offsets=[0.5], doc_name=doc_name
    )
    assert section["sections"][0]["closed_wire_count"] == 225
    assert section["sections"][0]["area"] == pytest.approx(225.0)
    assert section["truncated"] is True


@pytest.mark.asyncio
async def test_caller_errors(geometry) -> None:
    """Missing objects and unsuitable planes give clear errors."""
    tools, doc_name, _ = geometry
    with pytest.raises(ValueError, match="'NoSuchThing' not found"):
        await tools["get_shape_geometry"]("NoSuchThing", doc_name=doc_name)
    with pytest.raises(ValueError, match="Frame object 'Nowhere' not found"):
        await tools["get_shape_geometry"]("Box", frame="Nowhere", doc_name=doc_name)
    with pytest.raises(ValueError, match="not a plane"):
        await tools["get_section_outline"]("Box", plane="Cyl", doc_name=doc_name)
    with pytest.raises(ValueError, match="no faces to cut"):
        await tools["get_section_outline"]("Spline", plane="XY", doc_name=doc_name)
