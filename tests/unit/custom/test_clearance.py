"""Unit tests for the clearance tools (check_overlap, sweep_clash, spheres).

The FreeCAD side is covered by tests/integration/custom/test_clearance_live.py.
These tests check what the server sends, its validation before calling
FreeCAD, and that the runtime module loads without FreeCAD.
"""

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools.custom import clearance
from freecad_mcp.tools.custom.clearance import (
    register_clearance_tools,
    sweep_sample_estimate,
)


def _tools(bridge: Any) -> tuple[dict[str, Callable[..., Any]], dict[str, Any]]:
    """Register the clearance tools against a stand-in server."""
    tools: dict[str, Callable[..., Any]] = {}
    annotations: dict[str, Any] = {}
    server = MagicMock()

    def tool(**kwargs: Any) -> Callable[[Any], Any]:
        def register(function: Any) -> Any:
            tools[function.__name__] = function
            annotations[function.__name__] = kwargs.get("annotations")
            return function

        return register

    server.tool = tool

    async def get_bridge() -> Any:
        return bridge

    register_clearance_tools(server, get_bridge)
    return tools, annotations


def _bridge(result: Any = None) -> AsyncMock:
    bridge = AsyncMock()
    bridge.execute_python = AsyncMock(
        return_value=ExecutionResult(
            success=True,
            result=result if result is not None else {"status": "separate"},
            stdout="",
            stderr="",
            execution_time_ms=1.0,
        )
    )
    return bridge


def _sent(bridge: AsyncMock) -> tuple[str, dict[str, Any], int]:
    """Return the operation, its JSON arguments and the timeout sent."""
    call = bridge.execute_python.call_args
    code = call.args[0]
    line = next(item for item in code.splitlines() if "_p3d_dispatch(" in item)
    operation = line.split(", ")[1].strip("'")
    payload = line.split("_p3d_json.loads(", 1)[1].rsplit("))", 1)[0]
    args = json.loads(eval(payload))  # noqa: S307 - a repr of our own JSON
    return operation, args, call.kwargs["timeout_ms"]


def test_all_tools_are_read_only() -> None:
    tools, annotations = _tools(_bridge())

    assert set(tools) == {"check_overlap", "sweep_clash", "find_sphere_centres"}
    for name, annotation in annotations.items():
        assert annotation.read_only_hint is True, name
        assert annotation.destructive_hint is False, name


# --- check_overlap -------------------------------------------------------------


async def test_check_overlap_sends_arguments() -> None:
    bridge = _bridge()
    tools, _ = _tools(bridge)

    result = await tools["check_overlap"](
        "A",
        "B",
        elements_b=["Face3"],
        pose_b={"axis": [0, 0, 1], "angle": 15, "pivot": [1, 2, 3]},
        volume_threshold=0.01,
        doc_name="Doc",
    )

    assert result == {"status": "separate"}
    operation, args, timeout = _sent(bridge)
    assert operation == "check_overlap"
    assert timeout == clearance.CHECK_TIMEOUT_MS
    assert args == {
        "object_a": "A",
        "object_b": "B",
        "elements_b": ["Face3"],
        "pose_b": {"angle": 15.0, "axis": [0.0, 0.0, 1.0], "pivot": [1.0, 2.0, 3.0]},
        "volume_threshold": 0.01,
        "touch_tolerance": 0.001,
        "doc_name": "Doc",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"pose_a": {"spin": 3}}, "unknown keys"),
        ({"pose_a": {"angle": 10}}, "give an axis"),
        ({"pose_b": {"axis": [0, 0, 0], "angle": 1}}, "must not be zero"),
        ({"pose_b": {"offset": [1, 2]}}, r"\[x, y, z\]"),
        ({"pose_b": {"angle": "wide", "axis": [0, 0, 1]}}, "must be a number"),
        ({"volume_threshold": -1}, "must not be negative"),
        ({"touch_tolerance": -0.1}, "must not be negative"),
    ],
)
async def test_check_overlap_validates_before_calling(
    kwargs: dict[str, Any], message: str
) -> None:
    bridge = _bridge()
    tools, _ = _tools(bridge)

    with pytest.raises(ValueError, match=message):
        await tools["check_overlap"]("A", "B", **kwargs)
    bridge.execute_python.assert_not_called()


async def test_check_overlap_passes_freecad_errors_on() -> None:
    bridge = AsyncMock()
    bridge.execute_python = AsyncMock(
        return_value=ExecutionResult(
            success=False,
            result=None,
            stdout="",
            stderr="",
            execution_time_ms=1.0,
            error_traceback="Traceback...\nValueError: Object not found in Doc: B",
        )
    )
    tools, _ = _tools(bridge)

    with pytest.raises(ValueError, match="Object not found in Doc: B"):
        await tools["check_overlap"]("A", "B")


# --- sweep_clash -----------------------------------------------------------------


async def test_sweep_clash_sends_arguments_with_long_timeout() -> None:
    bridge = _bridge({"status": "done"})
    tools, _ = _tools(bridge)

    await tools["sweep_clash"](
        ["Ball"],
        ["Socket", "Stop"],
        pivot=[10, 20, 30],
        axis=[1, 0, 0],
        min_angle=-70,
        max_angle=70,
        allow_start_overlap=True,
        exclude_pairs=[["Ball", "Socket"]],
        doc_name="Doc",
    )

    operation, args, timeout = _sent(bridge)
    assert operation == "sweep_clash"
    assert timeout == clearance.SWEEP_TIMEOUT_MS == 600_000
    assert args == {
        "moving": ["Ball"],
        "fixed": ["Socket", "Stop"],
        "pivot": [10.0, 20.0, 30.0],
        "axis": [1.0, 0.0, 0.0],
        "min_angle": -70,
        "max_angle": 70,
        "step": 5.0,
        "precision": 0.5,
        "overlap_threshold": 0.001,
        "allow_start_overlap": True,
        "exclude_pairs": [["Ball", "Socket"]],
        "max_samples": 200,
        "max_log": 40,
        "doc_name": "Doc",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"moving": []}, "at least one"),
        ({"fixed": [""]}, "at least one"),
        ({"fixed": ["Arm"]}, "both moving and fixed"),
        ({"pivot": [0, 0]}, r"\[x, y, z\]"),
        ({"axis": [0, 0, 0]}, "must not be zero"),
        ({"axis": [0, float("nan"), 1]}, "finite"),
        ({"min_angle": 5}, "min_angle <= 0 <= max_angle"),
        ({"max_angle": -5}, "min_angle <= 0 <= max_angle"),
        ({"min_angle": 0, "max_angle": 0}, "not both zero"),
        ({"step": 0}, "must be positive"),
        ({"precision": -1}, "must be positive"),
        ({"overlap_threshold": -1}, "must not be negative"),
        ({"max_samples": 0}, "at least 1"),
        ({"step": 0.1}, "can need .* samples"),
        ({"exclude_pairs": [["Wall", "Arm"]]}, r"must be \[moving, fixed\]"),
        ({"exclude_pairs": [["Arm"]]}, r"must be \[moving, fixed\]"),
    ],
)
async def test_sweep_clash_validates_before_calling(
    kwargs: dict[str, Any], message: str
) -> None:
    bridge = _bridge()
    tools, _ = _tools(bridge)
    arguments: dict[str, Any] = {
        "moving": ["Arm"],
        "fixed": ["Wall"],
        "pivot": [0, 0, 0],
        "axis": [0, 0, 1],
    }
    arguments.update(kwargs)

    with pytest.raises(ValueError, match=message):
        await tools["sweep_clash"](**arguments)
    bridge.execute_python.assert_not_called()


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        # start + 18 coarse + 4 bisections, each way.
        ((-90, 90, 5, 0.5), 1 + 2 * (18 + 4)),
        # One direction only.
        ((0, 90, 5, 0.5), 1 + 18 + 4),
        # Precision coarser than the step: no refinement.
        ((-10, 10, 5, 10), 1 + 2 * 2),
        # A range that isn't a multiple of the step ends on the limit.
        ((-12, 12, 5, 5), 1 + 2 * 3),
    ],
)
def test_sweep_sample_estimate(limits: tuple[float, ...], expected: int) -> None:
    assert sweep_sample_estimate(*limits) == expected


# --- find_sphere_centres ---------------------------------------------------------


async def test_find_sphere_centres_sends_arguments() -> None:
    bridge = _bridge({"spheres": []})
    tools, _ = _tools(bridge)

    await tools["find_sphere_centres"]("Ball", faces=["Face1"], max_results=3)

    operation, args, _timeout = _sent(bridge)
    assert operation == "find_sphere_centres"
    assert args == {"object_name": "Ball", "faces": ["Face1"], "max_results": 3}


async def test_find_sphere_centres_validates_max_results() -> None:
    bridge = _bridge()
    tools, _ = _tools(bridge)

    with pytest.raises(ValueError, match="at least 1"):
        await tools["find_sphere_centres"]("Ball", max_results=0)
    bridge.execute_python.assert_not_called()


# --- runtime module --------------------------------------------------------------


def test_runtime_module_loads_without_freecad() -> None:
    path = (
        Path(__file__).resolve().parents[3]
        / "freecad/RobustMCPBridge/freecad_mcp_bridge/custom_runtime/clearance.py"
    )
    spec = importlib.util.spec_from_file_location("_test_clearance_runtime", path)
    assert spec and spec.loader
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)

    assert set(runtime.OPERATIONS) == {
        "check_overlap",
        "sweep_clash",
        "find_sphere_centres",
    }
    # The sweep stops itself before the server's timeout runs out.
    assert runtime.SWEEP_TIME_LIMIT_S * 1000 < clearance.SWEEP_TIMEOUT_MS
