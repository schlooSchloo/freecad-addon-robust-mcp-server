"""Tool that reports which tool modules loaded at startup.

When a tool module fails to load, its tools are missing and nothing else in
the conversation says why. This tool lets the client check.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import ToolAnnotations

from freecad_mcp.tools.custom.loader import get_registration_report


def register_status_tools(mcp: Any, _get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register the tool registration status tool.

    Args:
        mcp: The MCPServer instance.
        _get_bridge: Async function to get the active bridge (unused; this tool
            does not talk to FreeCAD).
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def get_tool_registration_status() -> dict[str, Any]:
        """Report which tool modules loaded when the server started.

        Use this when an expected tool is missing. A module that failed to
        load is skipped so the rest of the server still works; this shows
        which modules were skipped and why. Does not contact FreeCAD.

        Returns:
            Dictionary with:
                - ok: True if every module loaded and no tool names clashed
                - tool_count: Number of tools registered
                - modules: Number of tools registered by each module
                - failed_modules: Error summary for each module that was skipped
                - failed_tools: Error summary for each tool the server rejected
                - duplicate_tools: Tools skipped because another module already
                  registered the same name
        """
        return get_registration_report().as_dict()
