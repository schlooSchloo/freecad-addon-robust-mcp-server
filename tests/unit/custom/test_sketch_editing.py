"""Unit tests for the sketch editing tools.

Server side: the arguments, operation names and undo names sent to FreeCAD,
and validation before any call. Workbench side: the polyline planner and the
small helpers that don't need FreeCAD.
"""

import ast
import importlib.util
import json
import re
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools.custom.sketch_editing import register_sketch_editing_tools

RUNTIME_DIR = (
    Path(__file__).resolve().parents[3]
    / "freecad/RobustMCPBridge/freecad_mcp_bridge/custom_runtime"
)


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------


def _tools(result: ExecutionResult | None = None) -> tuple[dict[str, Any], AsyncMock]:
    """Register the tools against a mock bridge."""
    registered: dict[str, Any] = {}
    mcp = MagicMock()
    mcp.tool = lambda **_kwargs: lambda func: registered.setdefault(func.__name__, func)
    bridge = AsyncMock()
    bridge.execute_python = AsyncMock(
        return_value=result
        or ExecutionResult(
            success=True,
            result={"ok": True},
            stdout="",
            stderr="",
            execution_time_ms=1.0,
        )
    )

    async def get_bridge() -> Any:
        return bridge

    register_sketch_editing_tools(mcp, get_bridge)
    return registered, bridge


def _sent(bridge: AsyncMock) -> tuple[str, dict[str, Any], str]:
    """Return the operation, its JSON arguments and the code sent."""
    code = bridge.execute_python.call_args.args[0]
    match = re.search(r"_p3d_dispatch\(\d+, '(\w+)', _p3d_json\.loads\((.+)\)\)", code)
    assert match, code
    return match.group(1), json.loads(ast.literal_eval(match.group(2))), code


def test_registers_four_tools() -> None:
    tools, _ = _tools()

    assert set(tools) == {
        "move_sketch_point",
        "set_sketch_radius",
        "set_sketch_constraint_value",
        "add_sketch_polyline",
    }


async def test_move_point_sends_arguments_and_undo_name() -> None:
    tools, bridge = _tools()

    result = await tools["move_sketch_point"](
        sketch_name="Sketch",
        geometry_index=2,
        position=[1, 2.5],
        point="end",
        relative=True,
        doc_name="Doc",
    )

    operation, args, code = _sent(bridge)
    assert result == {"ok": True}
    assert operation == "sketch_move_point"
    assert args == {
        "sketch_name": "Sketch",
        "geometry_index": 2,
        "position": [1.0, 2.5],
        "point": "end",
        "relative": True,
        "doc_name": "Doc",
    }
    assert "openTransaction('Move Sketch point 2.end')" in code
    assert "FreeCAD.getDocument('Doc')" in code


async def test_default_document_is_left_out() -> None:
    tools, bridge = _tools()

    await tools["set_sketch_radius"](sketch_name="S", geometry_index=0, radius=4)

    operation, args, code = _sent(bridge)
    assert operation == "sketch_set_radius"
    assert args == {"sketch_name": "S", "geometry_index": 0, "radius": 4.0}
    assert "_txn_doc = FreeCAD.ActiveDocument" in code
    assert "openTransaction('Set S radius 0')" in code


async def test_constraint_value_by_name_and_units() -> None:
    tools, bridge = _tools()

    await tools["set_sketch_constraint_value"](
        sketch_name="S", constraint="Width", value="12 mm"
    )

    operation, args, code = _sent(bridge)
    assert operation == "sketch_set_constraint_value"
    assert args["constraint"] == "Width"
    assert args["value"] == "12 mm"
    assert "openTransaction('Set S constraint Width')" in code


async def test_polyline_sends_clean_points() -> None:
    tools, bridge = _tools()

    await tools["add_sketch_polyline"](
        sketch_name="S",
        points=[[0, 0], [20, 0], [20, 10]],
        closed=False,
        constrain="none",
        auto_horizontal_vertical=False,
        construction=True,
    )

    operation, args, code = _sent(bridge)
    assert operation == "sketch_add_polyline"
    assert args == {
        "sketch_name": "S",
        "points": [[0.0, 0.0], [20.0, 0.0], [20.0, 10.0]],
        "closed": False,
        "constrain": "none",
        "auto_horizontal_vertical": False,
        "construction": True,
    }
    assert "openTransaction('Add polyline to S')" in code


@pytest.mark.parametrize(
    ("tool", "kwargs", "message"),
    [
        (
            "move_sketch_point",
            {"geometry_index": 0, "position": [1, 2, 3]},
            "position must be",
        ),
        (
            "move_sketch_point",
            {"geometry_index": 0, "position": [1, float("nan")]},
            "position must be",
        ),
        ("set_sketch_radius", {"geometry_index": 0, "radius": 0}, "above 0"),
        ("set_sketch_radius", {"geometry_index": 0, "radius": -2}, "above 0"),
        (
            "set_sketch_constraint_value",
            {"constraint": "Width", "value": float("inf")},
            "finite",
        ),
        (
            "set_sketch_constraint_value",
            {"constraint": "  ", "value": 3},
            "index or a constraint name",
        ),
        ("add_sketch_polyline", {"points": [[0, 0]]}, "at least 2"),
        ("add_sketch_polyline", {"points": [[0, 0], [1]]}, "Point 1 must be"),
        ("add_sketch_polyline", {"points": [[0, 0], [True, 1]]}, "Point 1 must be"),
    ],
)
async def test_validation_before_calling_freecad(
    tool: str, kwargs: dict[str, Any], message: str
) -> None:
    tools, bridge = _tools()

    with pytest.raises(ValueError, match=message):
        await tools[tool](sketch_name="S", **kwargs)

    bridge.execute_python.assert_not_called()


async def test_freecad_errors_become_value_errors() -> None:
    failure = ExecutionResult(
        success=False,
        result=None,
        stdout="",
        stderr="",
        execution_time_ms=1.0,
        error_type="ValueError",
        error_traceback="Traceback...\nValueError: Sketch not found: 'S'\n",
    )
    tools, _ = _tools(failure)

    with pytest.raises(ValueError, match=r"^Sketch not found: 'S'$"):
        await tools["set_sketch_radius"](sketch_name="S", geometry_index=0, radius=1)


# ---------------------------------------------------------------------------
# Workbench side (no FreeCAD needed)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime() -> Any:
    """Import the workbench's sketch_editing module on its own."""
    name = "_test_sketch_editing_runtime"
    spec = importlib.util.spec_from_file_location(
        name, RUNTIME_DIR / "sketch_editing.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[name]


def _plan(runtime: Any, points: Any, **overrides: Any) -> dict[str, Any]:
    options = {"closed": True, "coincident": True, "auto_horizontal_vertical": True}
    options.update(overrides)
    return runtime.plan_polyline(points, **options)


def test_operations_are_registered(runtime: Any) -> None:
    assert set(runtime.OPERATIONS) == {
        "sketch_move_point",
        "sketch_set_radius",
        "sketch_set_constraint_value",
        "sketch_add_polyline",
    }


def test_rectangle_plan(runtime: Any) -> None:
    plan = _plan(runtime, [[0, 0], [20, 0], [20, 10], [0, 10]])

    assert plan["closed"] is True
    assert plan["segments"][3] == ((0.0, 10.0), (0.0, 0.0))
    assert plan["joints"] == [(0, 1), (1, 2), (2, 3), (3, 0)]
    assert plan["horizontal"] == [0, 2]
    assert plan["vertical"] == [1, 3]


def test_open_plan_has_no_closing_joint(runtime: Any) -> None:
    plan = _plan(runtime, [[0, 0], [10, 0], [10, 5]], closed=False)

    assert len(plan["segments"]) == 2
    assert plan["joints"] == [(0, 1)]


def test_repeated_end_point_closes(runtime: Any) -> None:
    plan = _plan(runtime, [[0, 0], [10, 0], [5, 5], [0, 0]], closed=False)

    assert plan["closed"] is True
    assert len(plan["segments"]) == 3
    assert plan["joints"][-1] == (2, 0)


def test_axis_tolerance(runtime: Any) -> None:
    # 0.4 degrees off horizontal snaps; 0.6 degrees off vertical doesn't.
    near = 10 * runtime.math.tan(runtime.math.radians(0.4))
    far = 10 * runtime.math.tan(runtime.math.radians(0.6))
    plan = _plan(
        runtime,
        [[0, 0], [10, near], [10 + far, 10 + near]],
        closed=False,
    )

    assert plan["horizontal"] == [0]
    assert plan["vertical"] == []


def test_options_off(runtime: Any) -> None:
    plan = _plan(
        runtime,
        [[0, 0], [20, 0], [20, 10]],
        coincident=False,
        auto_horizontal_vertical=False,
    )

    assert plan["joints"] == []
    assert plan["horizontal"] == plan["vertical"] == []


@pytest.mark.parametrize(
    ("points", "closed", "message"),
    [
        ("0,0", True, "list of"),
        ([[0, 0], [1, 0, 0]], False, "Point 1 must be"),
        ([[0, 0], ["a", 1]], False, "Point 1 must be"),
        ([[0, 0]], False, "at least 2 points"),
        ([[0, 0], [5, 0]], True, "at least 3 distinct"),
        ([[0, 0], [5, 0], [5, 0], [0, 5]], True, "Segment 1 .* has no length"),
    ],
)
def test_bad_polylines(runtime: Any, points: Any, closed: bool, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _plan(runtime, points, closed=closed)


def _fake_sketch(constraints: list[Any], **lists: list[int]) -> Any:
    sketch = types.SimpleNamespace(Name="Sketch", Constraints=constraints)
    for key, value in lists.items():
        setattr(sketch, key, value)
    return sketch


def _constraint(kind: str, name: str = "", first: int = 0) -> Any:
    return types.SimpleNamespace(
        Type=kind, Name=name, First=first, Second=-2000, Third=-2000
    )


def test_solver_lists_become_zero_based(runtime: Any) -> None:
    sketch = _fake_sketch(
        [_constraint("Coincident"), _constraint("Distance", "Width")],
        ConflictingConstraints=[1, 2, 9],
    )

    assert runtime._solver_list(sketch, "ConflictingConstraints") == [0, 1]
    assert runtime._solver_list(sketch, "MalformedConstraints") == []
    assert runtime._constraint_summary(sketch, 1) == {
        "index": 1,
        "type": "Distance",
        "name": "Width",
    }


def test_find_constraint(runtime: Any) -> None:
    sketch = _fake_sketch([_constraint("Coincident"), _constraint("Distance", "Width")])

    assert runtime._find_constraint(sketch, "Width") == 1
    assert runtime._find_constraint(sketch, 0) == 0
    assert runtime._find_constraint(sketch, " 1 ") == 1
    with pytest.raises(ValueError, match="out of range"):
        runtime._find_constraint(sketch, 2)
    with pytest.raises(ValueError, match=r"Named constraints: \['Width'\]"):
        runtime._find_constraint(sketch, "Height")
    with pytest.raises(ValueError, match="0-based index or a constraint name"):
        runtime._find_constraint(sketch, True)


def test_expression_lookup(runtime: Any) -> None:
    sketch = _fake_sketch([_constraint("Distance", "Width"), _constraint("Radius")])
    sketch.ExpressionEngine = [
        (".Constraints.Width", "Sheet.width"),
        (".Constraints[1]", "3 mm"),
    ]

    assert runtime._expression_for(sketch, 0) == "Sheet.width"
    assert runtime._expression_for(sketch, 1) == "3 mm"
    sketch.ExpressionEngine = []
    assert runtime._expression_for(sketch, 0) is None


def test_constraints_on_geometry(runtime: Any) -> None:
    sketch = _fake_sketch(
        [_constraint("Horizontal", first=0), _constraint("Radius", "R", first=2)]
    )

    assert runtime._constraints_on_geometry(sketch, 2) == [
        {"index": 1, "type": "Radius", "name": "R"}
    ]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Invalid constraint index: 8", "can't be solved with that value"),
        ("Cannot set the datum because the sketch contains conflicting", "first"),
        ("Negative datum values are not valid", "not valid."),
    ],
)
def test_datum_failure_messages(runtime: Any, message: str, expected: str) -> None:
    assert expected in runtime._datum_failure(message)
