"""Live checks for create_sketch plane lookup and the Part helper fixes.

Runs the production tools against a real FreeCAD through the XML-RPC bridge:
sketches in a second body (numbered origin planes) and on datum planes,
parametric Part::Scale, solid extrusion, slice outlines and the
``parametric`` flag.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools.objects import register_object_tools
from freecad_mcp.tools.partdesign import register_partdesign_tools
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration

Tool = Callable[..., Awaitable[Any]]


def _register(bridge: XmlRpcBridge) -> dict[str, Tool]:
    """Register object and PartDesign tools against a live bridge."""
    registered: dict[str, Tool] = {}
    mcp = MagicMock()
    mcp.tool = lambda: lambda func: registered.setdefault(func.__name__, func)

    async def get_bridge() -> XmlRpcBridge:
        return bridge

    register_object_tools(mcp, get_bridge)
    register_partdesign_tools(mcp, get_bridge)
    return registered


@pytest_asyncio.fixture
async def live() -> AsyncIterator[tuple[XmlRpcBridge, dict[str, Tool], str]]:
    """A connected bridge, the tools, and a fresh document (closed after)."""
    doc_name = f"McpCustomA_{uuid4().hex[:12]}"
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    created = await bridge.execute_python(
        f"FreeCAD.newDocument({doc_name!r})\n_result_ = True"
    )
    assert created.success, created.error_traceback
    try:
        yield bridge, _register(bridge), doc_name
    finally:
        try:
            await bridge.execute_python(
                f"FreeCAD.closeDocument({doc_name!r})\n_result_ = True"
            )
        finally:
            await bridge.disconnect()


async def _run(bridge: XmlRpcBridge, code: str) -> Any:
    result = await bridge.execute_python(code)
    assert result.success, result.error_traceback
    return result.result


@pytest.mark.asyncio
async def test_sketch_attaches_in_second_body_and_to_datum(live) -> None:
    """The second body's numbered origin planes and a datum plane are used."""
    bridge, tools, doc = live
    await _run(
        bridge,
        f"""
doc = FreeCAD.getDocument({doc!r})
doc.addObject("PartDesign::Body", "Body")
body2 = doc.addObject("PartDesign::Body", "Body001")
doc.recompute()
xz = [f for f in body2.Origin.OriginFeatures if f.Name.startswith("XZ_Plane")][0]
datum = body2.newObject("PartDesign::Plane", "DatumPlane")
datum.AttachmentSupport = [(xz, "")]
datum.MapMode = "FlatFace"
datum.AttachmentOffset = FreeCAD.Placement(FreeCAD.Vector(0, 0, 7), FreeCAD.Rotation())
doc.recompute()
_result_ = True
""",
    )
    sketch = await tools["create_sketch"](
        body_name="Body001", plane="XZ_Plane", doc_name=doc
    )
    assert sketch["attached_to"]["name"] == "XZ_Plane001"
    assert sketch["attachment_kind"] == "origin_plane"
    assert sketch["map_mode"] == "FlatFace"
    assert sketch["normal"] == pytest.approx([0.0, -1.0, 0.0], abs=1e-6)

    on_datum = await tools["create_sketch"](
        body_name="Body001", plane="DatumPlane", doc_name=doc
    )
    assert on_datum["attached_to"]["name"] == "DatumPlane"
    assert on_datum["attachment_kind"] == "datum_plane"
    assert on_datum["origin"] == pytest.approx([0.0, -7.0, 0.0], abs=1e-6)

    with pytest.raises(ValueError, match="No plane named"):
        await tools["create_sketch"](body_name="Body001", plane="Nope", doc_name=doc)
    with pytest.raises(ValueError, match="belongs to 'Body001'"):
        await tools["create_sketch"](body_name="Body", plane="DatumPlane", doc_name=doc)
    sketch_count = await _run(
        bridge,
        f"_result_ = len([o for o in FreeCAD.getDocument({doc!r}).Objects"
        " if o.TypeId == 'Sketcher::SketchObject'])",
    )
    assert sketch_count == 2


@pytest.mark.asyncio
async def test_scale_is_parametric_and_exact(live) -> None:
    """Part::Scale keeps volume = s^3 and follows changes to the source."""
    bridge, tools, doc = live
    await _run(
        bridge,
        f"""
import Part
doc = FreeCAD.getDocument({doc!r})
base = doc.addObject("Part::Feature", "Fused")
# transformGeometry made this shape invalid in the old implementation.
base.Shape = Part.makeBox(30, 20, 10).fuse(
    Part.makeSphere(8, FreeCAD.Vector(30, 20, 10))
)
box = doc.addObject("Part::Box", "Box")
doc.recompute()
_result_ = True
""",
    )
    s = 1.1575
    fused = await tools["scale_object"](object_name="Fused", scale=s, doc_name=doc)
    assert fused["valid"] is True
    assert fused["warnings"] == []
    assert fused["volume"] / fused["source_volume"] == pytest.approx(s**3, rel=1e-9)

    scaled = await tools["scale_object"](object_name="Box", scale=s, doc_name=doc)
    assert scaled["type_id"] == "Part::Scale"
    assert scaled["parametric"] is True
    assert scaled["source"] == "Box"
    assert scaled["solid_count"] == 1
    assert scaled["volume"] == pytest.approx(scaled["expected_volume"], rel=1e-9)
    ratio = await _run(
        bridge,
        f"""
doc = FreeCAD.getDocument({doc!r})
doc.getObject("Box").Height = 25
doc.recompute()
scaled = doc.getObject({scaled["name"]!r})
_result_ = [scaled.Shape.Volume / doc.getObject("Box").Shape.Volume,
            scaled.Shape.isValid()]
""",
    )
    assert ratio[0] == pytest.approx(s**3, rel=1e-9)
    assert ratio[1] is True


@pytest.mark.asyncio
async def test_extrude_closed_wire_makes_solid(live) -> None:
    """A closed wire gives one closed solid; an open wire with solid=True fails."""
    _, tools, doc = live
    await tools["make_wire"](
        points=[[0, 0, 0], [10, 0, 0], [10, 5, 0], [0, 5, 0]],
        closed=True,
        name="Closed",
        doc_name=doc,
    )
    await tools["make_wire"](
        points=[[0, 0, 0], [10, 0, 0], [10, 5, 0]], name="Open", doc_name=doc
    )
    solid = await tools["extrude_shape"](
        object_name="Closed", direction=[0, 0, 4], doc_name=doc
    )
    assert solid["shape_type"] == "Solid"
    assert solid["closed"] is True
    assert solid["solid_count"] == 1
    assert solid["volume"] == pytest.approx(200.0)

    surface = await tools["extrude_shape"](
        object_name="Open", direction=[0, 0, 4], doc_name=doc
    )
    assert surface["solid"] is False
    assert surface["solid_count"] == 0
    with pytest.raises(ValueError, match="Wire is not closed"):
        await tools["extrude_shape"](
            object_name="Open", direction=[0, 0, 4], solid=True, doc_name=doc
        )


@pytest.mark.asyncio
async def test_section_returns_outline(live) -> None:
    """A section through a box with a hole lists a rectangle and a circle."""
    bridge, tools, doc = live
    await _run(
        bridge,
        f"""
doc = FreeCAD.getDocument({doc!r})
box = doc.addObject("Part::Box", "Box")
box.Length = 20
box.Width = 10
box.Height = 10
cyl = doc.addObject("Part::Cylinder", "Cyl")
cyl.Radius = 3
cyl.Height = 20
cyl.Placement.Base = FreeCAD.Vector(10, 5, -5)
cut = doc.addObject("Part::Cut", "Cut")
cut.Base = box
cut.Tool = cyl
doc.recompute()
_result_ = True
""",
    )
    section = await tools["section_shape"](
        object_name="Cut", plane="XY", offset=5, doc_name=doc
    )
    assert section["parametric"] is False
    outline = section["outline"]
    assert (outline["wire_count"], outline["edge_count"]) == (2, 5)
    assert outline["truncated"] is False
    assert all(wire["closed"] for wire in outline["wires"])
    edges = [e for wire in outline["wires"] for e in wire["edges"]]
    circles = [e for e in edges if e["curve_type"] == "Circle"]
    assert len(circles) == 1
    assert circles[0]["radius"] == pytest.approx(3.0)
    assert circles[0]["center"] == pytest.approx([10.0, 5.0, 5.0])

    capped = await tools["section_shape"](
        object_name="Cut", plane="XY", offset=5, max_edges=2, doc_name=doc
    )
    assert capped["outline"]["edges_returned"] == 2
    assert capped["outline"]["truncated"] is True


@pytest.mark.asyncio
async def test_parametric_flag_matches_live_behaviour(live) -> None:
    """copy_object is static and boolean_operation follows its sources."""
    bridge, tools, doc = live
    await _run(
        bridge,
        f"""
doc = FreeCAD.getDocument({doc!r})
doc.addObject("Part::Box", "A")
b = doc.addObject("Part::Box", "B")
b.Placement.Base = FreeCAD.Vector(5, 5, 5)
doc.recompute()
_result_ = True
""",
    )
    copy = await tools["copy_object"](object_name="A", doc_name=doc)
    fused = await tools["boolean_operation"](
        operation="fuse", object1_name="A", object2_name="B", doc_name=doc
    )
    changed = await _run(
        bridge,
        f"""
doc = FreeCAD.getDocument({doc!r})
before = [doc.getObject(n).Shape.Volume for n in ({copy["name"]!r}, {fused["name"]!r})]
doc.getObject("A").Height = 30
doc.recompute()
after = [doc.getObject(n).Shape.Volume for n in ({copy["name"]!r}, {fused["name"]!r})]
_result_ = [abs(x - y) > 1e-9 for x, y in zip(before, after)]
""",
    )
    assert changed == [copy["parametric"], fused["parametric"]] == [False, True]
