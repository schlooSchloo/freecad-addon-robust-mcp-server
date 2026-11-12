"""Tests for fail-safe tool module loading.

A tool module that cannot be imported, or that raises while registering, must
be skipped without stopping the rest of the server. A module that fails
part-way must add none of its tools. Duplicate tool names must be reported
instead of being dropped silently.
"""

import asyncio
import logging
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

import freecad_mcp.tools as tools_package
from freecad_mcp.tools.custom import loader
from freecad_mcp.tools.custom.loader import (
    MAX_REPORTED_ERROR_LENGTH,
    RegistrationReport,
    ToolModule,
    get_registration_report,
    register_tool_modules,
    start_registration_report,
)

FAKE_PACKAGE = "fake_tool_modules"


async def _get_bridge() -> Any:
    return None


def _tool_names(server: MCPServer) -> set[str]:
    return {tool.name for tool in asyncio.run(server.list_tools())}


class _RecordingMCP:
    """Minimal server that records registered tool names."""

    def __init__(self, reject: set[str] | None = None) -> None:
        self.names: list[str] = []
        self.reject = reject or set()

    def tool(self, name: str | None = None, **_kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            tool_name = name or function.__name__
            if tool_name in self.reject:
                raise TypeError(f"cannot build schema for {tool_name}")
            self.names.append(tool_name)
            return function

        return decorator


@pytest.fixture
def fake_modules(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Install fake tool modules under a throwaway package name."""
    created: dict[str, Any] = {}

    def add(name: str, register: Any, function: str = "register") -> ToolModule:
        module = types.ModuleType(f"{FAKE_PACKAGE}.{name}")
        setattr(module, function, register)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        created[name] = module
        return ToolModule(name, module.__name__, function)

    yield {"add": add, "created": created}


def _register_ok(mcp: Any, _get_bridge: Any) -> None:
    @mcp.tool()
    async def ok_tool() -> str:
        return "ok"


def _register_partial_then_fail(mcp: Any, _get_bridge: Any) -> None:
    @mcp.tool()
    async def partial_tool() -> str:
        return "partial"

    raise RuntimeError("broken after the first tool")


class TestFailedModules:
    """Modules that fail are skipped and reported."""

    def test_missing_module_is_skipped_and_others_load(self, fake_modules):
        good = fake_modules["add"]("good", _register_ok)
        missing = ToolModule("missing", f"{FAKE_PACKAGE}.does_not_exist", "register")
        server = MCPServer("test")
        report = RegistrationReport()

        register_tool_modules(server, (missing, good), _get_bridge, report=report)

        assert _tool_names(server) == {"ok_tool"}
        assert "missing" in report.failed_modules
        assert report.failed_modules["missing"].startswith("ModuleNotFoundError")
        assert report.registered == {"good": ["ok_tool"]}

    def test_syntax_error_in_module_is_skipped(self, tmp_path, monkeypatch):
        package = tmp_path / "broken_pkg"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "tools.py").write_text("def register(mcp, get_bridge):\n    if\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        server = MCPServer("test")
        report = RegistrationReport()

        register_tool_modules(
            server,
            (ToolModule("broken", "broken_pkg.tools", "register"),),
            _get_bridge,
            report=report,
        )

        assert _tool_names(server) == set()
        assert report.failed_modules["broken"].startswith("SyntaxError")

    def test_missing_registration_function_is_reported(self, fake_modules):
        spec = fake_modules["add"]("nofunc", _register_ok, function="register")
        renamed = ToolModule(spec.label, spec.module, "not_there")
        report = RegistrationReport()

        register_tool_modules(_RecordingMCP(), (renamed,), _get_bridge, report=report)

        assert report.failed_modules["nofunc"].startswith("AttributeError")

    def test_module_failing_part_way_adds_no_tools(self, fake_modules):
        partial = fake_modules["add"]("partial", _register_partial_then_fail)
        good = fake_modules["add"]("good", _register_ok)
        server = MCPServer("test")
        report = RegistrationReport()

        register_tool_modules(server, (partial, good), _get_bridge, report=report)

        assert _tool_names(server) == {"ok_tool"}
        assert "partial" not in report.registered
        assert report.failed_modules["partial"] == (
            "RuntimeError: broken after the first tool"
        )

    def test_failure_is_logged_with_traceback(self, fake_modules, caplog):
        partial = fake_modules["add"]("partial", _register_partial_then_fail)

        with caplog.at_level(logging.ERROR, logger=loader.__name__):
            register_tool_modules(
                _RecordingMCP(), (partial,), _get_bridge, report=RegistrationReport()
            )

        record = next(r for r in caplog.records if "partial" in r.getMessage())
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None

    def test_long_error_is_truncated(self, fake_modules):
        def register(_mcp: Any, _get_bridge: Any) -> None:
            raise ValueError("x" * (MAX_REPORTED_ERROR_LENGTH * 2))

        spec = fake_modules["add"]("long", register)
        report = RegistrationReport()

        register_tool_modules(_RecordingMCP(), (spec,), _get_bridge, report=report)

        message = report.failed_modules["long"]
        assert message.endswith("... (truncated)")
        assert len(message) <= MAX_REPORTED_ERROR_LENGTH + len(" ... (truncated)")


class TestRejectedTools:
    """A single tool the server rejects is skipped without losing its module."""

    def test_rejected_tool_is_reported_and_siblings_kept(self, fake_modules):
        def register(mcp: Any, _get_bridge: Any) -> None:
            @mcp.tool()
            async def keep_me() -> str:
                return "kept"

            @mcp.tool()
            async def reject_me() -> str:
                return "rejected"

        spec = fake_modules["add"]("mixed", register)
        server = _RecordingMCP(reject={"reject_me"})
        report = RegistrationReport()

        register_tool_modules(server, (spec,), _get_bridge, report=report)

        assert server.names == ["keep_me"]
        assert report.registered == {"mixed": ["keep_me"]}
        assert report.failed_tools["mixed.reject_me"].startswith("TypeError")


class TestDuplicateNames:
    """The first tool registered under a name is kept; later ones are reported."""

    def test_later_duplicate_is_skipped_and_reported(self, fake_modules, caplog):
        first = fake_modules["add"]("first", _register_ok)
        second = fake_modules["add"]("second", _register_ok)
        server = MCPServer("test")
        report = RegistrationReport()

        with caplog.at_level(logging.ERROR, logger=loader.__name__):
            register_tool_modules(server, (first, second), _get_bridge, report=report)

        assert _tool_names(server) == {"ok_tool"}
        assert report.registered == {"first": ["ok_tool"], "second": []}
        assert report.duplicates == [
            {"tool": "ok_tool", "kept_from": "first", "skipped_from": "second"}
        ]
        assert any("ok_tool" in r.getMessage() for r in caplog.records)

    def test_duplicates_detected_across_calls_with_same_report(self, fake_modules):
        first = fake_modules["add"]("first", _register_ok)
        second = fake_modules["add"]("second", _register_ok)
        report = RegistrationReport()
        server = _RecordingMCP()

        register_tool_modules(server, (first,), _get_bridge, report=report)
        register_tool_modules(server, (second,), _get_bridge, report=report)

        assert server.names == ["ok_tool"]
        assert len(report.duplicates) == 1

    def test_explicit_tool_name_is_used(self, fake_modules):
        def register(mcp: Any, _get_bridge: Any) -> None:
            @mcp.tool(name="ok_tool")
            async def different_function_name() -> str:
                return "renamed"

        first = fake_modules["add"]("first", _register_ok)
        renamed = fake_modules["add"]("renamed", register)
        report = RegistrationReport()

        register_tool_modules(
            _RecordingMCP(), (first, renamed), _get_bridge, report=report
        )

        assert report.duplicates[0]["tool"] == "ok_tool"


class TestUnsafeFlag:
    """allow_unsafe_code is passed only to functions that accept it."""

    def test_flag_passed_when_accepted(self, fake_modules):
        seen: list[bool] = []

        def register(_mcp: Any, _get_bridge: Any, *, allow_unsafe_code: bool) -> None:
            seen.append(allow_unsafe_code)

        spec = fake_modules["add"]("unsafe", register)

        register_tool_modules(
            _RecordingMCP(),
            (spec,),
            _get_bridge,
            allow_unsafe_code=True,
            report=RegistrationReport(),
        )

        assert seen == [True]

    def test_flag_not_passed_when_not_accepted(self, fake_modules):
        spec = fake_modules["add"]("plain", _register_ok)
        report = RegistrationReport()

        register_tool_modules(
            _RecordingMCP(), (spec,), _get_bridge, allow_unsafe_code=True, report=report
        )

        assert report.failed_modules == {}
        assert report.registered == {"plain": ["ok_tool"]}


class TestReport:
    """The report summary and the module-level current report."""

    def test_as_dict_ok_when_clean(self):
        report = RegistrationReport(registered={"a": ["x", "y"], "b": ["z"]})

        summary = report.as_dict()

        assert summary["ok"] is True
        assert summary["tool_count"] == 3
        assert summary["modules"] == {"a": 2, "b": 1}

    @pytest.mark.parametrize(
        "field, value",
        [
            ("failed_modules", {"a": "Error"}),
            ("failed_tools", {"a.x": "Error"}),
            ("duplicates", [{"tool": "x", "kept_from": "a", "skipped_from": "b"}]),
        ],
    )
    def test_as_dict_not_ok_on_any_problem(self, field, value):
        report = RegistrationReport(**{field: value})

        assert report.as_dict()["ok"] is False

    def test_start_replaces_current_report(self):
        old = get_registration_report()

        new = start_registration_report()

        assert new is not old
        assert get_registration_report() is new
        assert new.as_dict()["tool_count"] == 0


class TestRegisterAllTools:
    """register_all_tools keeps the server running when a module is broken."""

    def test_broken_upstream_module_does_not_stop_others(self, monkeypatch):
        broken = ToolModule("broken", f"{FAKE_PACKAGE}.missing", "register")
        monkeypatch.setattr(
            tools_package,
            "TOOL_MODULES",
            (broken, *tools_package.TOOL_MODULES),
        )
        server = MCPServer("test")

        tools_package.register_all_tools(server, _get_bridge)

        report = get_registration_report()
        names = _tool_names(server)
        assert "broken" in report.failed_modules
        assert "create_partdesign_body" in names
        assert "get_tool_registration_status" in names
        assert len(names) == report.as_dict()["tool_count"]

    @pytest.mark.parametrize("allow_unsafe_code", [False, True])
    def test_real_modules_register_cleanly(self, allow_unsafe_code):
        server = MCPServer("test")

        tools_package.register_all_tools(
            server, _get_bridge, allow_unsafe_code=allow_unsafe_code
        )

        summary = get_registration_report().as_dict()
        assert summary["ok"] is True, summary
        assert len(_tool_names(server)) == summary["tool_count"]
        assert ("execute_python" in _tool_names(server)) is allow_unsafe_code
