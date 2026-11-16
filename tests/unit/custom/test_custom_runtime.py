"""Tests for the custom runtime: the workbench dispatcher and the server helper."""

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools.custom import runtime
from freecad_mcp.tools.custom.runtime import (
    MISSING_RUNTIME,
    P3D_API_VERSION,
    build_runtime_code,
    run_operation,
)

RUNTIME_DIR = (
    Path(__file__).resolve().parents[3]
    / "freecad/RobustMCPBridge/freecad_mcp_bridge/custom_runtime"
)


def _load_runtime_package(name: str = "_test_custom_runtime") -> types.ModuleType:
    """Import a fresh copy of the workbench's custom_runtime package."""
    spec = importlib.util.spec_from_file_location(
        name,
        RUNTIME_DIR / "__init__.py",
        submodule_search_locations=[str(RUNTIME_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def package():
    name = "_test_custom_runtime"
    module = _load_runtime_package(name)
    yield module
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


class TestDispatcher:
    """The workbench side, which runs without FreeCAD for these tests."""

    def test_versions_match(self, package):
        assert package.P3D_API_VERSION == P3D_API_VERSION

    def test_runtime_info_without_freecad(self, package, monkeypatch):
        monkeypatch.setitem(sys.modules, "FreeCAD", None)

        info = package.dispatch(P3D_API_VERSION, "runtime_info", {})

        assert info["api_version"] == P3D_API_VERSION
        assert "runtime_info" in info["operations"]
        assert info["load_errors"] == {}
        assert info["freecad_version"] is None

    @pytest.mark.parametrize(
        "version, side", [(P3D_API_VERSION + 1, "workbench"), (0, "MCP server")]
    )
    def test_version_mismatch_names_side_to_update(self, package, version, side):
        with pytest.raises(RuntimeError, match=f"Update the {side}"):
            package.dispatch(version, "runtime_info", {})

    def test_unknown_operation(self, package):
        with pytest.raises(ValueError, match="Unknown custom operation: 'nope'"):
            package.dispatch(P3D_API_VERSION, "nope", {})

    def test_arguments_must_be_an_object(self, package):
        with pytest.raises(ValueError, match="must be an object"):
            package.dispatch(P3D_API_VERSION, "runtime_info", [])  # type: ignore[arg-type]

    def test_broken_module_is_skipped_and_reported(self, package, monkeypatch):
        real_import = importlib.import_module

        def fake_import(name: str, *args: Any) -> Any:
            if name.endswith(".info"):
                raise SyntaxError("invalid syntax")
            return real_import(name, *args)

        monkeypatch.setattr(package.importlib, "import_module", fake_import)

        with pytest.raises(ValueError, match="info \\(SyntaxError"):
            package.dispatch(P3D_API_VERSION, "runtime_info", {})
        assert "info" in package.load_errors()

    def test_duplicate_operation_names_are_refused(self, package, monkeypatch):
        first = types.SimpleNamespace(OPERATIONS={"same": lambda _a: 1})
        second = types.SimpleNamespace(OPERATIONS={"same": lambda _a: 2})
        infos = [types.SimpleNamespace(name="a"), types.SimpleNamespace(name="b")]
        monkeypatch.setattr(package.pkgutil, "iter_modules", lambda _path: infos)
        monkeypatch.setattr(
            package.importlib,
            "import_module",
            lambda name: first if name.endswith(".a") else second,
        )

        with pytest.raises(RuntimeError, match="defined in both 'a' and 'b'"):
            package.available_operations()

    def test_private_modules_are_ignored(self, package, monkeypatch):
        infos = [types.SimpleNamespace(name="_helpers")]
        monkeypatch.setattr(package.pkgutil, "iter_modules", lambda _path: infos)

        assert package.available_operations() == []


class TestBuildRuntimeCode:
    """The program the server sends."""

    def test_compiles_and_calls_dispatcher(self):
        code = build_runtime_code("runtime_info", {"a": 1})

        compile(code, "<test>", "exec")
        assert "_p3d_dispatch(1, 'runtime_info'" in code

    def test_arguments_travel_as_json_not_code(self):
        hostile = {"name": "x'); import os; os.system('rm -rf /'); ('", "n": "\n"}
        code = build_runtime_code("runtime_info", hostile)
        captured: dict[str, Any] = {}

        def fake_dispatch(_version: int, _operation: str, args: dict[str, Any]) -> Any:
            captured.update(args)
            return "ok"

        namespace: dict[str, Any] = {"_p3d_dispatch": fake_dispatch}
        exec(compile(code, "<test>", "exec"), namespace)  # noqa: S102

        assert captured == hostile
        assert namespace["_result_"] == "ok"

    def test_missing_dispatcher_raises_clear_error(self):
        code = build_runtime_code("runtime_info", {})

        with pytest.raises(RuntimeError, match="custom runtime"):
            exec(compile(code, "<test>", "exec"), {})  # noqa: S102

    def test_transaction_wrapping(self):
        code = build_runtime_code(
            "runtime_info", {}, transaction="Make thing", doc_name="Doc"
        )

        compile(code, "<test>", "exec")
        assert "openTransaction('Make thing')" in code or "Make thing" in code
        assert "getDocument('Doc')" in code


def _result(**kwargs: Any) -> ExecutionResult:
    defaults: dict[str, Any] = {
        "success": True,
        "result": None,
        "stdout": "",
        "stderr": "",
        "execution_time_ms": 1.0,
    }
    defaults.update(kwargs)
    return ExecutionResult(**defaults)


def _bridge(result: ExecutionResult) -> tuple[Any, AsyncMock]:
    bridge = AsyncMock()
    bridge.execute_python = AsyncMock(return_value=result)

    async def get_bridge() -> Any:
        return bridge

    return get_bridge, bridge


class TestRunOperation:
    """Result and error handling."""

    async def test_returns_result_and_drops_none_arguments(self):
        get_bridge, bridge = _bridge(_result(result={"ok": True}))

        value = await run_operation(get_bridge, "op", {"a": 1, "b": None})

        assert value == {"ok": True}
        code = bridge.execute_python.call_args.args[0]
        assert '"a": 1' in code and '"b"' not in code

    async def test_value_error_message_is_passed_through(self):
        tb = "Traceback...\nValueError: Object not found: Pad"
        get_bridge, _ = _bridge(_result(success=False, error_traceback=tb))

        with pytest.raises(ValueError, match=r"^Object not found: Pad$"):
            await run_operation(get_bridge, "op", {})

    @pytest.mark.parametrize(
        "message",
        [MISSING_RUNTIME, "Incompatible custom tools interface: workbench 1, server 2"],
    )
    async def test_workbench_problems_raise_tool_error(self, message):
        tb = f"Traceback...\nRuntimeError: {message}"
        get_bridge, _ = _bridge(_result(success=False, error_traceback=tb))

        with pytest.raises(ToolError, match="custom"):
            await run_operation(get_bridge, "op", {})

    async def test_other_errors_report_last_line(self):
        tb = "Traceback...\nPart.OCCError: BRep_API: command not done"
        get_bridge, _ = _bridge(_result(success=False, error_traceback=tb))

        with pytest.raises(ValueError, match="OCCError: BRep_API"):
            await run_operation(get_bridge, "op", {})

    async def test_timeout_is_passed_on(self):
        get_bridge, bridge = _bridge(_result(result=1))

        await run_operation(get_bridge, "op", {}, timeout_ms=5000)

        assert bridge.execute_python.call_args.kwargs["timeout_ms"] == 5000

    def test_default_timeout_is_generous(self):
        assert runtime.DEFAULT_TIMEOUT_MS >= 60_000
