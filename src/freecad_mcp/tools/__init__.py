"""MCP tool implementations for FreeCAD.

This package contains all MCP tool definitions for interacting with FreeCAD.
Tools are organized by category:

- execution: Python code execution tools
- cam: CAM workbench tools (jobs, tools, operations, dressups, G-code)
- documents: Document management tools
- objects: Object creation and manipulation tools
- partdesign: PartDesign workbench tools
- spreadsheet: Spreadsheet workbench tools for parametric design
- draft: Draft workbench tools (ShapeString for 3D text)
- export: Export functionality tools
- macros: Macro management tools
- view: View and screenshot tools
- validation: Object and document validation tools
- custom: Tools added on the fork's ``custom`` branch (see CUSTOM_CHANGES.md)

Each module is imported and registered separately, so one broken module is
skipped and logged instead of stopping the whole server.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from freecad_mcp.tools._registration import ErrorReportingMCP
from freecad_mcp.tools.custom import CUSTOM_TOOL_MODULES
from freecad_mcp.tools.custom.loader import (
    ToolModule,
    register_tool_modules,
    start_registration_report,
)

__all__ = [
    "TOOL_MODULES",
    "register_all_tools",
]

# Tool modules, registered in this order.
TOOL_MODULES: tuple[ToolModule, ...] = (
    ToolModule("execution", "freecad_mcp.tools.execution", "register_execution_tools"),
    ToolModule("documents", "freecad_mcp.tools.documents", "register_document_tools"),
    ToolModule("objects", "freecad_mcp.tools.objects", "register_object_tools"),
    ToolModule(
        "partdesign", "freecad_mcp.tools.partdesign", "register_partdesign_tools"
    ),
    ToolModule(
        "spreadsheet", "freecad_mcp.tools.spreadsheet", "register_spreadsheet_tools"
    ),
    ToolModule("draft", "freecad_mcp.tools.draft", "register_draft_tools"),
    ToolModule("export", "freecad_mcp.tools.export", "register_export_tools"),
    ToolModule("cam", "freecad_mcp.tools.cam", "register_cam_tools"),
    ToolModule("macros", "freecad_mcp.tools.macros", "register_macro_tools"),
    ToolModule("view", "freecad_mcp.tools.view", "register_view_tools"),
    ToolModule(
        "validation", "freecad_mcp.tools.validation", "register_validation_tools"
    ),
)


def register_all_tools(
    mcp: Any,
    get_bridge_func: Callable[[], Awaitable[Any]],
    *,
    allow_unsafe_code: bool = False,
) -> None:
    """Register all FreeCAD tools with the Robust MCP Server.

    Args:
        mcp: The MCPServer instance (Any to keep registration modules decoupled).
        get_bridge_func: Async function returning the active bridge connection.
        allow_unsafe_code: Whether to expose tools that execute arbitrary code.
    """
    # Let expected errors (ValueError, ConnectionError, ...) reach the client
    # with their message instead of the SDK's generic "Error executing tool".
    mcp = ErrorReportingMCP(mcp)
    report = start_registration_report()
    # Custom tools go last, so an upstream tool keeps its name in any clash.
    register_tool_modules(
        mcp,
        (*TOOL_MODULES, *CUSTOM_TOOL_MODULES),
        get_bridge_func,
        allow_unsafe_code=allow_unsafe_code,
        report=report,
    )
