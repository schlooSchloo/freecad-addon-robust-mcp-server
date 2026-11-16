"""Tests for get_shape_geometry and get_section_outline (server side and helpers).

The FreeCAD-side geometry is checked against real FreeCAD in
``tests/integration/custom/test_geometry_live.py``. Here: the arguments the
tools send, validation before any call, read-only annotations, and the
runtime helpers that don't need FreeCAD.
"""

import ast
import importlib.util
import re
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools.custom.geometry import register_geometry_tools

RUNTIME_DIR = (
    Path(__file__).resolve().parents[3]
    / "freecad/RobustMCPBridge/freecad_mcp_bridge/custom_runtime"
)


class _CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.kwargs: dict[str, dict[str, Any]] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            self.tools[function.__name__] = function
            self.kwargs[function.__name__] = kwargs
            return function

        return decorator


def _setup(result: Any = None) -> tuple[_CapturingMCP, AsyncMock]:
    bridge = AsyncMock()
    bridge.execute_python = AsyncMock(
        return_value=ExecutionResult(
            success=True,
            result=result if result is not None else {"ok": True},
            stdout="",
            stderr="",
            execution_time_ms=1.0,
        )
    )

    async def get_bridge() -> Any:
        return bridge

    mcp = _CapturingMCP()
    register_geometry_tools(mcp, get_bridge)
    return mcp, bridge


def _sent(bridge: AsyncMock) -> tuple[str, dict[str, Any]]:
    """Return the operation name and arguments sent to the runtime."""
    code = bridge.execute_python.call_args.args[0]
    match = re.search(r"_p3d_dispatch\(1, '(\w+)', _p3d_json\.loads\((.+)\)\)", code)
    assert match, code
    import json

    return match.group(1), json.loads(ast.literal_eval(match.group(2)))


class TestShapeGeometryTool:
    async def test_sends_arguments_and_returns_result(self):
        mcp, bridge = _setup({"object": "Box"})

        result = await mcp.tools["get_shape_geometry"](
            "Box",
            elements=[" Edge3", "Face2"],
            include=["edges", "faces"],
            max_items=10,
            offset=5,
            sample_tolerance=0.05,
            frame="Sketch",
            doc_name="Doc",
        )

        assert result == {"object": "Box"}
        operation, args = _sent(bridge)
        assert operation == "get_shape_geometry"
        assert args == {
            "object_name": "Box",
            "elements": ["Edge3", "Face2"],
            "include": ["edges", "faces"],
            "max_items": 10,
            "offset": 5,
            "sample_tolerance": 0.05,
            "frame": "Sketch",
            "doc_name": "Doc",
        }

    async def test_defaults_leave_optional_arguments_out(self):
        mcp, bridge = _setup()

        await mcp.tools["get_shape_geometry"]("Box")

        _, args = _sent(bridge)
        assert args == {"object_name": "Box", "max_items": 25, "offset": 0}

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"elements": ["Edge0"]}, "Unknown element names"),
            ({"elements": ["Solid1"]}, "Unknown element names"),
            ({"include": ["solids"]}, "include may list"),
            ({"include": []}, "include may list"),
            ({"max_items": 0}, "max_items"),
            ({"offset": -1}, "offset"),
            ({"samples": 1}, "samples must be between"),
            ({"sample_tolerance": 0.0}, "greater than 0"),
            ({"samples": 5, "sample_tolerance": 0.1}, "not both"),
        ],
    )
    async def test_rejects_bad_arguments_before_calling(self, kwargs, message):
        mcp, bridge = _setup()

        with pytest.raises(ValueError, match=message):
            await mcp.tools["get_shape_geometry"]("Box", **kwargs)

        bridge.execute_python.assert_not_called()

    async def test_runtime_errors_reach_the_caller(self):
        mcp, bridge = _setup()
        bridge.execute_python.return_value = ExecutionResult(
            success=False,
            result=None,
            stdout="",
            stderr="",
            execution_time_ms=1.0,
            error_traceback="Traceback...\nValueError: Object 'X' not found",
        )

        with pytest.raises(ValueError, match="Object 'X' not found"):
            await mcp.tools["get_shape_geometry"]("X")


class TestSectionOutlineTool:
    async def test_sends_arguments(self):
        mcp, bridge = _setup()

        await mcp.tools["get_section_outline"](
            "Box",
            origin=[0, 0, 1],
            normal=[0, 0, 2],
            offsets=[1.0, 2.0],
            samples=4,
            doc_name="Doc",
        )

        operation, args = _sent(bridge)
        assert operation == "get_section_outline"
        assert args == {
            "object_name": "Box",
            "origin": [0, 0, 1],
            "normal": [0, 0, 2],
            "offsets": [1.0, 2.0],
            "max_edges": 200,
            "samples": 4,
            "doc_name": "Doc",
        }

    async def test_named_plane(self):
        mcp, bridge = _setup()

        await mcp.tools["get_section_outline"]("Box", plane="XY")

        _, args = _sent(bridge)
        assert args == {"object_name": "Box", "plane": "XY", "max_edges": 200}

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({}, "Give plane"),
            ({"plane": "XY", "normal": [0, 0, 1]}, "not both"),
            ({"normal": [0, 0]}, "normal must be"),
            ({"normal": [0, 0, 0]}, "zero vector"),
            ({"normal": [0, 0, 1], "origin": [1, 2]}, "origin must be"),
            ({"plane": "XY", "offsets": []}, "1 to 50"),
            ({"plane": "XY", "offsets": [0.0] * 51}, "1 to 50"),
            ({"plane": "XY", "max_edges": 0}, "max_edges"),
            ({"plane": "XY", "samples": 501}, "samples must be between"),
        ],
    )
    async def test_rejects_bad_arguments_before_calling(self, kwargs, message):
        mcp, bridge = _setup()

        with pytest.raises(ValueError, match=message):
            await mcp.tools["get_section_outline"]("Box", **kwargs)

        bridge.execute_python.assert_not_called()


@pytest.mark.parametrize("name", ["get_shape_geometry", "get_section_outline"])
def test_tools_are_marked_read_only(name):
    mcp, _ = _setup()

    annotations = mcp.kwargs[name]["annotations"]
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True


@pytest.fixture
def runtime():
    """The workbench's custom_runtime package, imported without FreeCAD."""
    name = "_test_geometry_runtime"
    spec = importlib.util.spec_from_file_location(
        name,
        RUNTIME_DIR / "__init__.py",
        submodule_search_locations=[str(RUNTIME_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


class TestRuntimeHelpers:
    def test_operations_are_discovered(self, runtime):
        operations = runtime.available_operations()

        assert {"get_shape_geometry", "get_section_outline"} <= set(operations)
        assert runtime.load_errors() == {}

    def _helpers(self, runtime: types.ModuleType) -> Any:
        runtime.available_operations()
        return sys.modules[f"{runtime.__name__}._shape_serialise"]

    def test_num_rounds_and_drops_negative_zero(self, runtime):
        helpers = self._helpers(runtime)

        assert helpers.num(1.23456789) == 1.234568
        assert str(helpers.num(-1e-9)) == "0.0"

    def test_chain_edges_flips_edges_into_one_path(self, runtime):
        helpers = self._helpers(runtime)
        edges = [
            {"start": [0, 10, 0], "end": [10, 10, 0], "direction": [1, 0, 0]},
            {"start": [0, 10, 0], "end": [0, 0, 0], "direction": [0, -1, 0]},
            {"start": [0, 0, 0], "end": [10, 0, 0], "direction": [1, 0, 0]},
            {
                "start": [10, 10, 0],
                "end": [10, 0, 0],
                "direction": [0, -1, 0],
                "samples": [[10, 10, 0], [10, 0, 0]],
            },
        ]

        chained = helpers.chain_edges(edges)

        starts = [e["start"] for e in chained]
        ends = [e["end"] for e in chained]
        assert starts == [[10, 10, 0], [0, 10, 0], [0, 0, 0], [10, 0, 0]]
        assert ends == [[0, 10, 0], [0, 0, 0], [10, 0, 0], [10, 10, 0]]
        assert chained[0]["direction"] == [-1.0, 0.0, 0.0]
        assert chained[3]["samples"] == [[10, 0, 0], [10, 10, 0]]

    def test_chain_edges_leaves_unconnected_edges(self, runtime):
        helpers = self._helpers(runtime)
        edges = [
            {"start": [0, 0, 0], "end": [1, 0, 0]},
            {"start": [5, 5, 0], "end": [6, 5, 0]},
        ]

        assert helpers.chain_edges([dict(e) for e in edges]) == edges

    def test_size_budget_stops_when_full(self, runtime):
        helpers = self._helpers(runtime)
        item = {"name": "Edge1", "start": [0.0, 0.0, 0.0]}
        cost = helpers.result_cost(item)

        # dict + 2 keys + string value + list of three floats.
        assert cost == 16 + (16 + 4) + (16 + 5) + (16 + 5) + 16 * 4
        size = helpers.SizeBudget(limit=2 * cost)
        assert size.take(item) and size.take(item)
        assert size.take(item) is False
        assert size.exhausted is True

    def test_argument_checks_in_the_runtime(self, runtime):
        runtime.available_operations()
        geometry = sys.modules[f"{runtime.__name__}.geometry"]

        with pytest.raises(ValueError, match="not both"):
            geometry._sample_budget({"samples": 3, "sample_tolerance": 0.1})
        with pytest.raises(ValueError, match="whole number"):
            geometry._int_arg({"max_items": "5"}, "max_items", 25, 1, 1000)
        page = geometry._page(list(range(12)), 10, 5)
        assert page == {
            "total": 12,
            "offset": 10,
            "returned": 2,
            "truncated": False,
            "chosen": [10, 11],
        }
        assert geometry._page(list(range(12)), 0, 5)["truncated"] is True
