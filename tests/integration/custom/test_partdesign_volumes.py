"""Volume before/after reporting on PartDesign features, with known answers."""

import math
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from tests.integration.auth_support import integration_credentials
from tests.integration.test_partdesign_symmetry import _register_tools

pytestmark = pytest.mark.integration

_SETUP = """
import FreeCAD
import Part

def _rect(sketch, x0, y0, x1, y1):
    pts = [FreeCAD.Vector(x0, y0, 0), FreeCAD.Vector(x1, y0, 0),
           FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x0, y1, 0)]
    for i in range(4):
        sketch.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % 4]))

def _sketch(body, name, plane, rect, z=0):
    sk = body.newObject("Sketcher::SketchObject", name)
    role = plane + "_Plane"
    support = [f for f in body.Origin.OriginFeatures if f.Role == role][0]
    sk.AttachmentSupport = [(support, [""])]
    sk.MapMode = "FlatFace"
    if z:
        sk.AttachmentOffset = FreeCAD.Placement(FreeCAD.Vector(0, 0, z), FreeCAD.Rotation())
    _rect(sk, *rect)
    return sk

doc = FreeCAD.newDocument({doc_name!r})

# A: sketch only (pad, then groove that misses the solid, are done by tools)
a = doc.addObject("PartDesign::Body", "BodyA")
_sketch(a, "SketchA", "XY", (0, 0, 10, 10))
_sketch(a, "GrooveMiss", "XZ", (30, 0, 32, 2))

# P: pad made here, then a pocket made by the tool
p = doc.addObject("PartDesign::Body", "BodyP")
sk = _sketch(p, "SketchP", "XY", (0, 0, 10, 10))
pad = p.newObject("PartDesign::Pad", "PadP")
pad.Profile = sk
pad.Length = 5
_sketch(p, "PocketSketch", "XY", (2, 2, 4, 4), z=5)

# F and C: padded blocks for fillet and chamfer
for label in ("F", "C"):
    b = doc.addObject("PartDesign::Body", "Body" + label)
    s = _sketch(b, "Sketch" + label, "XY", (0, 0, 10, 10))
    pd = b.newObject("PartDesign::Pad", "Pad" + label)
    pd.Profile = s
    pd.Length = 5

# R: revolution of a 5..10 by 0..2 rectangle about Z
r = doc.addObject("PartDesign::Body", "BodyR")
_sketch(r, "SketchR", "XZ", (5, 0, 10, 2))

# L: loft from a 10x10 square to a 4x4 square 10 mm above
l = doc.addObject("PartDesign::Body", "BodyL")
_sketch(l, "LoftBottom", "XY", (0, 0, 10, 10))
_sketch(l, "LoftTop", "XY", (3, 3, 7, 7), z=10)

doc.recompute()
_result_ = True
"""

_VERTICAL_EDGE = """
doc = FreeCAD.getDocument({doc_name!r})
shape = doc.getObject({name!r}).Shape
_result_ = [
    "Edge%d" % (i + 1)
    for i, e in enumerate(shape.Edges)
    if abs(e.Vertexes[0].Z - e.Vertexes[-1].Z) > 1e-6
][0]
"""


@pytest.fixture
async def live() -> AsyncIterator[tuple[XmlRpcBridge, str, dict[str, Any]]]:
    doc_name = f"McpVolumes_{uuid4().hex}"
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    try:
        setup = await bridge.execute_python(_SETUP.format(doc_name=doc_name))
        assert setup.success, setup.error_traceback or setup.stderr
        yield bridge, doc_name, _register_tools(bridge)
    finally:
        try:
            await bridge.execute_python(
                f"FreeCAD.closeDocument({doc_name!r})\n_result_ = True"
            )
        finally:
            await bridge.disconnect()


async def _vertical_edge(bridge: XmlRpcBridge, doc_name: str, name: str) -> str:
    result = await bridge.execute_python(
        _VERTICAL_EDGE.format(doc_name=doc_name, name=name)
    )
    assert result.success, result.error_traceback or result.stderr
    return result.result


async def test_pad_from_empty_body(live: Any) -> None:
    _, doc, tools = live
    result = await tools["pad_sketch"](sketch_name="SketchA", length=5, doc_name=doc)
    assert result["volume_before"] == pytest.approx(0.0)
    assert result["volume_after"] == pytest.approx(500.0)
    assert result["volume_change"] == pytest.approx(500.0)
    assert "warning" not in result


async def test_pocket_reports_change(live: Any) -> None:
    _, doc, tools = live
    result = await tools["pocket_sketch"](
        sketch_name="PocketSketch", length=2, doc_name=doc
    )
    assert result["volume_before"] == pytest.approx(500.0)
    assert result["volume_removed"] == pytest.approx(8.0)
    assert result["volume_change"] == pytest.approx(-8.0)
    assert "warning" not in result


async def test_fillet_decreases_volume(live: Any) -> None:
    bridge, doc, tools = live
    edge = await _vertical_edge(bridge, doc, "PadF")
    result = await tools["fillet_edges"](
        object_name="PadF", radius=1.0, edges=[edge], doc_name=doc
    )
    assert result["volume_before"] == pytest.approx(500.0)
    assert result["volume_change"] == pytest.approx(-(1 - math.pi / 4) * 5, abs=1e-6)
    assert result["volume_after"] < result["volume_before"]
    assert "warning" not in result


async def test_chamfer_decreases_volume(live: Any) -> None:
    bridge, doc, tools = live
    edge = await _vertical_edge(bridge, doc, "PadC")
    result = await tools["chamfer_edges"](
        object_name="PadC", size=1.0, edges=[edge], doc_name=doc
    )
    assert result["volume_change"] == pytest.approx(-2.5, abs=1e-6)


async def test_groove_that_misses_the_solid_warns(live: Any) -> None:
    _, doc, tools = live
    await tools["pad_sketch"](sketch_name="SketchA", length=5, doc_name=doc)
    result = await tools["groove_sketch"](
        sketch_name="GrooveMiss", axis="Base_Z", doc_name=doc
    )
    assert result["volume_change"] == pytest.approx(0.0)
    assert result["volume_removed"] == pytest.approx(0.0)
    assert "removed no material" in result["warning"]


async def test_revolution_adds_ring(live: Any) -> None:
    _, doc, tools = live
    result = await tools["revolution_sketch"](
        sketch_name="SketchR", axis="Base_Z", doc_name=doc
    )
    assert result["volume_before"] == pytest.approx(0.0)
    assert result["volume_change"] == pytest.approx(math.pi * (100 - 25) * 2, rel=1e-6)
    assert "warning" not in result


async def test_loft_adds_frustum(live: Any) -> None:
    _, doc, tools = live
    result = await tools["loft_sketches"](
        sketch_names=["LoftBottom", "LoftTop"], ruled=True, doc_name=doc
    )
    assert result["volume_change"] == pytest.approx(10 / 3 * (100 + 16 + 40), rel=1e-6)
    assert "warning" not in result
