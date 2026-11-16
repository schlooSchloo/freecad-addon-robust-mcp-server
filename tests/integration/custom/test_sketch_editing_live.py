"""Sketch editing tools against a real FreeCAD.

Known-answer fixtures: a 20 x 10 mm rectangle drawn as a closed polyline, a
fully constrained copy of it, circles and arcs, and a pad that must follow a
changed dimension.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools.custom.sketch_editing import register_sketch_editing_tools
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration

Tool = Callable[..., Awaitable[Any]]

_SETUP = """
import FreeCAD
doc = FreeCAD.newDocument({doc!r})
body = doc.addObject("PartDesign::Body", "Body")
xy = [f for f in body.Origin.OriginFeatures if f.Role == "XY_Plane"][0]
sketch = body.newObject("Sketcher::SketchObject", "Sketch")
sketch.AttachmentSupport = [(xy, [""])]
sketch.MapMode = "FlatFace"
doc.recompute()
_result_ = True
"""


def _register(bridge: XmlRpcBridge) -> dict[str, Tool]:
    """Register the sketch editing tools against a live bridge."""
    registered: dict[str, Tool] = {}
    mcp = MagicMock()
    mcp.tool = lambda **_kwargs: lambda func: registered.setdefault(func.__name__, func)

    async def get_bridge() -> XmlRpcBridge:
        return bridge

    register_sketch_editing_tools(mcp, get_bridge)
    return registered


@pytest_asyncio.fixture
async def live() -> AsyncIterator[tuple[XmlRpcBridge, dict[str, Tool], str]]:
    """A bridge, the tools, and a document with an empty attached sketch."""
    doc = f"McpSketchEdit_{uuid4().hex[:12]}"
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    try:
        created = await bridge.execute_python(_SETUP.format(doc=doc))
        assert created.success, created.error_traceback
        yield bridge, _register(bridge), doc
    finally:
        try:
            await bridge.execute_python(
                f"FreeCAD.closeDocument({doc!r})\n_result_ = True"
            )
        finally:
            await bridge.disconnect()


async def _run(bridge: XmlRpcBridge, code: str) -> Any:
    result = await bridge.execute_python(code)
    assert result.success, result.error_traceback
    return result.result


async def _vertices(bridge: XmlRpcBridge, doc: str) -> list[list[float]]:
    """Start points of every line in the sketch, rounded."""
    return await _run(
        bridge,
        f"""
s = FreeCAD.getDocument({doc!r}).getObject("Sketch")
_result_ = [[round(g.StartPoint.x, 6), round(g.StartPoint.y, 6)] for g in s.Geometry]
""",
    )


async def _undo_names(bridge: XmlRpcBridge, doc: str) -> list[str]:
    return await _run(bridge, f"_result_ = FreeCAD.getDocument({doc!r}).UndoNames")


async def _rectangle(tools: dict[str, Tool], doc: str) -> dict[str, Any]:
    return await tools["add_sketch_polyline"](
        sketch_name="Sketch",
        points=[[0, 0], [20, 0], [20, 10], [0, 10]],
        doc_name=doc,
    )


async def _constrain(bridge: XmlRpcBridge, doc: str, *constraints: str) -> None:
    """Add named constraints to the sketch: entries are 'Name=Constraint(...)'."""
    lines = []
    for entry in constraints:
        name, _, text = entry.partition("=")
        lines.append(f"i = s.addConstraint(Sketcher.{text})")
        if name:
            lines.append(f"s.renameConstraint(i, {name!r})")
    body = "\n".join(lines)
    await _run(
        bridge,
        f"""
import Sketcher
doc = FreeCAD.getDocument({doc!r})
s = doc.getObject("Sketch")
{body}
doc.recompute()
_result_ = True
""",
    )


@pytest.mark.asyncio
async def test_rectangle_polyline(live) -> None:
    """20 x 10 closed: 4 coincident, 2 horizontal, 2 vertical, DoF 4."""
    bridge, tools, doc = live
    result = await _rectangle(tools, doc)

    assert result["geometry_indices"] == [0, 1, 2, 3]
    assert result["closed"] is True
    assert result["constraint_indices"] == {
        "coincident": [0, 1, 2, 3],
        "horizontal": [4, 5],
        "vertical": [6, 7],
    }
    assert result["vertices"] == [[0, 0], [20, 0], [20, 10], [0, 10]]
    state = result["sketch_state"]
    assert state["geometry_count"] == 4
    assert state["constraint_count"] == 8
    assert state["dof"] == 4
    assert state["fully_constrained"] is False
    assert state["solver_status"] == "ok"
    assert state["conflicting"] == state["redundant"] == []
    assert state["wire_count"] == state["closed_wires"] == 1
    assert state["all_wires_closed"] is True
    types = await _run(
        bridge,
        f"_result_ = [c.Type for c in FreeCAD.getDocument({doc!r}).Sketch.Constraints]",
    )
    assert types == ["Coincident"] * 4 + ["Horizontal"] * 2 + ["Vertical"] * 2
    assert (await _undo_names(bridge, doc))[0] == "Add polyline to Sketch"
    # One undo step removes the whole polyline and its constraints.
    undone = await _run(
        bridge,
        f"""
doc = FreeCAD.getDocument({doc!r})
doc.undo()
doc.recompute()
_result_ = [len(doc.Sketch.Geometry), len(doc.Sketch.Constraints)]
""",
    )
    assert undone == [0, 0]


@pytest.mark.asyncio
async def test_polyline_variants(live) -> None:
    """Open, loose, construction, near-axis snapping and a repeated end point."""
    bridge, tools, doc = live
    open_line = await tools["add_sketch_polyline"](
        sketch_name="Sketch",
        points=[[0, 0], [10, 0.05], [10, 8]],
        closed=False,
        doc_name=doc,
    )
    assert open_line["closed"] is False
    assert open_line["constraint_indices"] == {
        "coincident": [0],
        "horizontal": [1],
        "vertical": [2],
    }
    # The 0.29 degree segment snaps to horizontal; the solver levels it.
    assert open_line["vertices"][1][1] == pytest.approx(
        open_line["vertices"][0][1], abs=1e-6
    )
    assert open_line["sketch_state"]["all_wires_closed"] is False

    triangle = await tools["add_sketch_polyline"](
        sketch_name="Sketch",
        points=[[30, 0], [40, 0], [35, 5], [30, 0]],
        closed=False,
        constrain="none",
        auto_horizontal_vertical=False,
        construction=True,
        doc_name=doc,
    )
    assert triangle["closed"] is True
    assert triangle["geometry_indices"] == [2, 3, 4]
    assert triangle["constraint_indices"] == {
        "coincident": [],
        "horizontal": [],
        "vertical": [],
    }
    construction = await _run(
        bridge,
        f"s = FreeCAD.getDocument({doc!r}).Sketch\n"
        "_result_ = [s.getConstruction(i) for i in range(5)]",
    )
    assert construction == [False, False, True, True, True]

    with pytest.raises(ValueError, match=r"Segment 1 .* has no length"):
        await tools["add_sketch_polyline"](
            sketch_name="Sketch", points=[[0, 0], [5, 0], [5, 0]], doc_name=doc
        )
    assert len(await _vertices(bridge, doc)) == 5


@pytest.mark.asyncio
async def test_move_free_corner(live) -> None:
    """Dragging a corner keeps the rectangle square-cornered and joined."""
    bridge, tools, doc = live
    await _rectangle(tools, doc)
    moved = await tools["move_sketch_point"](
        sketch_name="Sketch",
        geometry_index=1,
        point="end",
        position=[25, 12],
        doc_name=doc,
    )
    assert moved["before"] == [20, 10]
    assert moved["after"] == [25, 12]
    assert moved["moved"] is True
    assert moved["reached"] is True
    assert moved["sketch_state"]["dof"] == 4
    assert moved["sketch_state"]["all_wires_closed"] is True
    assert await _vertices(bridge, doc) == [[0, 0], [25, 0], [25, 12], [0, 12]]

    relative = await tools["move_sketch_point"](
        sketch_name="Sketch",
        geometry_index=0,
        point="start",
        position=[-5, -2],
        relative=True,
        doc_name=doc,
    )
    assert relative["after"] == [-5, -2]
    assert await _vertices(bridge, doc) == [[-5, -2], [25, -2], [25, 12], [-5, 12]]
    names = await _undo_names(bridge, doc)
    assert names[:2] == ["Move Sketch point 0.start", "Move Sketch point 1.end"]

    with pytest.raises(ValueError, match=r"no center point; use 'start' or 'end'"):
        await tools["move_sketch_point"](
            sketch_name="Sketch",
            geometry_index=0,
            point="center",
            position=[0, 0],
            doc_name=doc,
        )
    with pytest.raises(ValueError, match="out of range"):
        await tools["move_sketch_point"](
            sketch_name="Sketch", geometry_index=9, position=[0, 0], doc_name=doc
        )


@pytest.mark.asyncio
async def test_move_point_held_by_constraints(live) -> None:
    """A fully constrained corner stays put and the result says why."""
    bridge, tools, doc = live
    await _rectangle(tools, doc)
    await _constrain(
        bridge,
        doc,
        "=Constraint('DistanceX', -1, 1, 0, 1, 0.0)",
        "=Constraint('DistanceY', -1, 1, 0, 1, 0.0)",
        "Width=Constraint('DistanceX', 0, 1, 0, 2, 20.0)",
        "Height=Constraint('DistanceY', 1, 1, 1, 2, 10.0)",
    )
    moved = await tools["move_sketch_point"](
        sketch_name="Sketch",
        geometry_index=1,
        point="end",
        position=[25, 12],
        doc_name=doc,
    )
    assert moved["moved"] is False
    assert moved["reached"] is False
    assert moved["after"] == [20, 10]
    assert moved["distance_from_requested"] == pytest.approx(5.385165, abs=1e-6)
    assert "Constraints hold it" in moved["message"]
    held = {c.get("name") for c in moved["constraints_on_geometry"]}
    assert "Height" in held
    assert moved["sketch_state"]["dof"] == 0
    assert moved["sketch_state"]["fully_constrained"] is True
    assert await _vertices(bridge, doc) == [[0, 0], [20, 0], [20, 10], [0, 10]]


@pytest.mark.asyncio
async def test_set_named_constraint_value_and_pad_follows(live) -> None:
    """Changing 'Width' moves the geometry and the pad that uses it."""
    bridge, tools, doc = live
    await _rectangle(tools, doc)
    await _constrain(
        bridge,
        doc,
        "=Constraint('DistanceX', -1, 1, 0, 1, 0.0)",
        "=Constraint('DistanceY', -1, 1, 0, 1, 0.0)",
        "Width=Constraint('DistanceX', 0, 1, 0, 2, 20.0)",
        "Height=Constraint('DistanceY', 1, 1, 1, 2, 10.0)",
    )
    await _run(
        bridge,
        f"""
doc = FreeCAD.getDocument({doc!r})
pad = doc.Body.newObject("PartDesign::Pad", "Pad")
pad.Profile = doc.Sketch
pad.Length = 5
doc.recompute()
_result_ = round(pad.Shape.Volume, 6)
""",
    )
    result = await tools["set_sketch_constraint_value"](
        sketch_name="Sketch", constraint="Width", value=30, doc_name=doc
    )
    assert result["constraint"] == {"index": 10, "type": "DistanceX", "name": "Width"}
    assert result["value_before"] == 20
    assert result["value"] == 30
    assert result["unit"] == "mm"
    assert result["sketch_state"]["fully_constrained"] is True
    assert result["sketch_state"]["objects_in_error"] == []
    assert await _vertices(bridge, doc) == [[0, 0], [30, 0], [30, 10], [0, 10]]
    volume = await _run(
        bridge, f"_result_ = round(FreeCAD.getDocument({doc!r}).Pad.Shape.Volume, 6)"
    )
    assert volume == pytest.approx(30 * 10 * 5)

    by_index = await tools["set_sketch_constraint_value"](
        sketch_name="Sketch", constraint=11, value="0.5 in", doc_name=doc
    )
    assert by_index["constraint"]["name"] == "Height"
    assert by_index["value"] == pytest.approx(12.7)
    assert (await _vertices(bridge, doc))[2] == [30, 12.7]
    names = await _undo_names(bridge, doc)
    assert names[:2] == ["Set Sketch constraint 11", "Set Sketch constraint Width"]


@pytest.mark.asyncio
async def test_constraint_value_refusals_change_nothing(live) -> None:
    """Wrong type, wrong unit, expressions and conflicts leave the sketch as is."""
    bridge, tools, doc = live
    await _rectangle(tools, doc)
    await _constrain(
        bridge,
        doc,
        "Width=Constraint('DistanceX', 0, 1, 0, 2, 20.0)",
        "Diag=Constraint('Distance', 0, 1, 2, 1, 22.360679775)",
    )
    set_value = tools["set_sketch_constraint_value"]
    with pytest.raises(ValueError, match="Coincident constraint, which has no value"):
        await set_value(sketch_name="Sketch", constraint=0, value=5, doc_name=doc)
    with pytest.raises(ValueError, match="is not a length"):
        await set_value(
            sketch_name="Sketch", constraint="Width", value="30 deg", doc_name=doc
        )
    with pytest.raises(ValueError, match="No constraint named 'Nope'"):
        await set_value(sketch_name="Sketch", constraint="Nope", value=5, doc_name=doc)

    # A width of 30 mm can't fit inside a 22.36 mm diagonal.
    before = await _undo_names(bridge, doc)
    with pytest.raises(
        ValueError,
        match=r"Couldn't set constraint 8 \('Width'\) to 30.0 mm: the sketch can't be solved",
    ):
        await set_value(
            sketch_name="Sketch", constraint="Width", value=30, doc_name=doc
        )
    assert await _vertices(bridge, doc) == [[0, 0], [20, 0], [20, 10], [0, 10]]
    assert await _undo_names(bridge, doc) == before
    state = await _run(
        bridge, f"_result_ = list(FreeCAD.getDocument({doc!r}).Sketch.State)"
    )
    assert "Invalid" not in state

    await _run(
        bridge,
        f"""
doc = FreeCAD.getDocument({doc!r})
doc.Sketch.setExpression("Constraints.Diag", "25 mm")
doc.recompute()
_result_ = True
""",
    )
    with pytest.raises(ValueError, match="set by the expression '25 mm'"):
        await set_value(sketch_name="Sketch", constraint="Diag", value=8, doc_name=doc)


@pytest.mark.asyncio
async def test_angle_constraint_in_degrees(live) -> None:
    """Angle values are degrees, numbers or strings."""
    bridge, tools, doc = live
    await tools["add_sketch_polyline"](
        sketch_name="Sketch",
        points=[[0, 0], [10, 0], [10, 10]],
        closed=False,
        auto_horizontal_vertical=False,
        doc_name=doc,
    )
    await _constrain(bridge, doc, "Bend=Constraint('Angle', 0, 2, 1, 1, 1.5707963)")
    result = await tools["set_sketch_constraint_value"](
        sketch_name="Sketch", constraint="Bend", value=60, doc_name=doc
    )
    assert result["unit"] == "deg"
    assert result["value_before"] == pytest.approx(90, abs=1e-4)
    assert result["value"] == pytest.approx(60, abs=1e-6)
    again = await tools["set_sketch_constraint_value"](
        sketch_name="Sketch", constraint="Bend", value="45 deg", doc_name=doc
    )
    assert again["value"] == pytest.approx(45, abs=1e-6)


@pytest.mark.asyncio
async def test_set_radius(live) -> None:
    """Free circles resize; an arc's joined line follows; constrained ones refuse."""
    bridge, tools, doc = live
    await _run(
        bridge,
        f"""
import math, Part, Sketcher
V = FreeCAD.Vector
doc = FreeCAD.getDocument({doc!r})
s = doc.Sketch
s.addGeometry(Part.Circle(V(5, 5, 0), V(0, 0, 1), 3))
s.addGeometry(Part.ArcOfCircle(Part.Circle(V(20, 0, 0), V(0, 0, 1), 4), 0, math.pi / 2))
s.addGeometry(Part.LineSegment(V(20, 4, 0), V(10, 4, 0)))
s.addConstraint(Sketcher.Constraint("Coincident", 1, 3, -1, 1))
s.addConstraint(Sketcher.Constraint("Coincident", 1, 2, 2, 1))
s.addGeometry(Part.Circle(V(40, 0, 0), V(0, 0, 1), 2))
i = s.addConstraint(Sketcher.Constraint("Diameter", 3, 4.0))
s.renameConstraint(i, "Bore")
s.addGeometry(Part.LineSegment(V(0, -5, 0), V(5, -5, 0)))
doc.recompute()
_result_ = True
""",
    )
    circle = await tools["set_sketch_radius"](
        sketch_name="Sketch", geometry_index=0, radius=7, doc_name=doc
    )
    assert circle["radius_before"] == 3
    assert circle["radius"] == 7
    assert circle["center"] == [5, 5]

    # The arc's centre sits on the origin (moved there by the coincident).
    arc = await tools["set_sketch_radius"](
        sketch_name="Sketch", geometry_index=1, radius=6, doc_name=doc
    )
    assert arc["radius"] == 6
    joined = await _run(
        bridge,
        f"""
s = FreeCAD.getDocument({doc!r}).Sketch
a = s.getPoint(1, 2)
b = s.getPoint(2, 1)
_result_ = [round(a.distanceToPoint(b), 6), round(a.distanceToPoint(s.getPoint(1, 3)), 6)]
""",
    )
    assert joined == [0, 6]
    constraint_count = arc["sketch_state"]["constraint_count"]
    assert constraint_count == 3

    with pytest.raises(ValueError, match="Diameter constraint 2 \\('Bore'\\)"):
        await tools["set_sketch_radius"](
            sketch_name="Sketch", geometry_index=3, radius=5, doc_name=doc
        )
    with pytest.raises(ValueError, match="is a LineSegment"):
        await tools["set_sketch_radius"](
            sketch_name="Sketch", geometry_index=4, radius=5, doc_name=doc
        )
