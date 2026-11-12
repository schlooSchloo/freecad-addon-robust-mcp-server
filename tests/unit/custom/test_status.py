"""Tests for the get_tool_registration_status tool."""

from typing import Any

from freecad_mcp.tools.custom.loader import start_registration_report
from freecad_mcp.tools.custom.status import register_status_tools


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


async def _get_bridge() -> Any:
    raise AssertionError("the status tool must not contact FreeCAD")


def _status_tool() -> tuple[Any, dict[str, Any]]:
    mcp = _CapturingMCP()
    register_status_tools(mcp, _get_bridge)
    name = "get_tool_registration_status"
    return mcp.tools[name], mcp.kwargs[name]


async def test_reports_current_registration():
    report = start_registration_report()
    report.registered["documents"] = ["list_documents", "create_document"]
    report.failed_modules["partdesign"] = "SyntaxError: invalid syntax"
    tool, _ = _status_tool()

    result = await tool()

    assert result["ok"] is False
    assert result["tool_count"] == 2
    assert result["modules"] == {"documents": 2}
    assert result["failed_modules"] == {"partdesign": "SyntaxError: invalid syntax"}


async def test_reflects_a_newer_report():
    tool, _ = _status_tool()
    start_registration_report().registered["view"] = ["fit_all"]

    result = await tool()

    assert result == {
        "ok": True,
        "tool_count": 1,
        "modules": {"view": 1},
        "failed_modules": {},
        "failed_tools": {},
        "duplicate_tools": [],
    }


def test_is_marked_read_only():
    _, kwargs = _status_tool()

    annotations = kwargs["annotations"]
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
