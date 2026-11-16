"""Mesh tools: server-side argument handling and runtime-side validation."""

import ast
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools.custom.mesh import register_mesh_tools

RUNTIME_DIR = (
    Path(__file__).resolve().parents[3]
    / "freecad/RobustMCPBridge/freecad_mcp_bridge/custom_runtime"
)


@pytest.fixture
def tools() -> tuple[dict[str, Any], dict[str, Any], AsyncMock]:
    """Mesh tools registered against a mock bridge."""
    registered: dict[str, Any] = {}
    annotations: dict[str, Any] = {}

    def tool(**kwargs: Any) -> Any:
        def decorate(func: Any) -> Any:
            registered[func.__name__] = func
            annotations[func.__name__] = kwargs.get("annotations")
            return func

        return decorate

    mcp = MagicMock()
    mcp.tool = tool
    bridge = AsyncMock()
    bridge.execute_python = AsyncMock(
        return_value=ExecutionResult(
            success=True,
            result={"mesh": "Box_mesh"},
            stdout="",
            stderr="",
            execution_time_ms=1.0,
        )
    )

    async def get_bridge() -> AsyncMock:
        return bridge

    register_mesh_tools(mcp, get_bridge)
    return registered, annotations, bridge


def _sent_arguments(bridge: AsyncMock) -> dict[str, Any]:
    code = bridge.execute_python.call_args.args[0]
    start = code.index("_p3d_json.loads(") + len("_p3d_json.loads(")
    end = code.index("'))", start) + 1
    return json.loads(ast.literal_eval(code[start:end]))


class TestRegistration:
    def test_both_tools_registered(self, tools):
        registered, _, _ = tools
        assert set(registered) == {"create_mesh_from_shape", "check_mesh"}

    def test_check_mesh_is_read_only_and_create_is_not(self, tools):
        _, annotations, _ = tools
        assert annotations["check_mesh"].read_only_hint is True
        assert annotations["check_mesh"].destructive_hint is False
        assert annotations["create_mesh_from_shape"] is None


class TestCreateMeshFromShape:
    async def test_sends_arguments_in_one_transaction(self, tools):
        registered, _, bridge = tools
        result = await registered["create_mesh_from_shape"](
            "Box", linear_deflection=0.05, doc_name="Doc"
        )
        assert result == {"mesh": "Box_mesh"}
        code = bridge.execute_python.call_args.args[0]
        assert "create_mesh_from_shape" in code
        assert "Create mesh from shape" in code
        assert "FreeCAD.getDocument('Doc')" in code
        sent = _sent_arguments(bridge)
        assert sent["source"] == "Box"
        assert sent["linear_deflection"] == 0.05
        assert sent["angular_deflection"] == 30.0
        assert sent["relative"] is False
        assert "name" not in sent  # None is left out

    async def test_runtime_error_reaches_caller(self, tools):
        registered, _, bridge = tools
        bridge.execute_python.return_value = ExecutionResult(
            success=False,
            result=None,
            stdout="",
            stderr="",
            execution_time_ms=1.0,
            error_type="ValueError",
            error_traceback="Traceback\nValueError: Object 'X' not found",
        )
        with pytest.raises(ValueError, match="Object 'X' not found"):
            await registered["create_mesh_from_shape"]("X")


class TestCheckMesh:
    async def test_is_not_wrapped_in_a_transaction(self, tools):
        registered, _, bridge = tools
        await registered["check_mesh"]("Mesh", check_self_intersections=True)
        code = bridge.execute_python.call_args.args[0]
        assert "openTransaction" not in code
        sent = _sent_arguments(bridge)
        assert sent["mesh"] == "Mesh"
        assert sent["check_self_intersections"] is True
        assert sent["tolerance"] == 1e-4


class TestRuntimeValidation:
    @pytest.fixture
    def helpers(self):
        spec = importlib.util.spec_from_file_location(
            "_p3d_mesh_helpers", RUNTIME_DIR / "_mesh_helpers.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_valid_settings(self, helpers):
        assert helpers.check_deflection(1, 15) == (1.0, 15.0)

    @pytest.mark.parametrize(
        ("linear", "angular", "message"),
        [
            (0, 10, "linear_deflection must be greater than 0"),
            (-1, 10, "linear_deflection must be greater than 0"),
            (0.1, 0, "angular_deflection must be greater than 0"),
            (0.1, 91, "90 degrees or less"),
            (True, 10, "must be a number"),
            ("x", 10, "must be a number"),
            (float("nan"), 10, "greater than 0"),
        ],
    )
    def test_bad_settings(self, helpers, linear, angular, message):
        with pytest.raises(ValueError, match=message):
            helpers.check_deflection(linear, angular)
