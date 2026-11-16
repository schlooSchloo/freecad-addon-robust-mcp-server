"""Read-only report on links from a document to other files."""

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import ToolAnnotations

from freecad_mcp.tools.custom.runtime import run_operation


def register_dependencies_tools(
    mcp: Any, get_bridge: Callable[[], Awaitable[Any]]
) -> None:
    """Register the document dependency tool.

    Args:
        mcp: The MCPServer instance.
        get_bridge: Async function to get the active bridge.
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def get_document_dependencies(
        doc_name: str | None = None,
        max_links: int = 200,
    ) -> dict[str, Any]:
        """List every link from a document to another file and whether it works.

        Covers App::Link, SubShapeBinder and any other cross-file link
        property. Read-only: it only checks that paths exist, and never
        opens, reads, copies or saves files. Use it before moving or
        "Save As" into a new folder: relative paths break unless the linked
        files are copied alongside.

        Args:
            doc_name: Document to inspect. Uses the active document if None.
            max_links: Most links to list (default 200); `truncated` is set
                if there were more.

        Returns:
            Dictionary with `document`, `document_file`, `summary`
            (`all_resolve`, counts, `broken` with reasons, `not_open`,
            `linked_files`) and `links`. Each link has `object`, `property`,
            `stored_path`, `resolved_path`, `target_object`,
            `target_document`, `file_exists`, `object_present`, `status`
            (resolved, document_not_open, missing_file, missing_object) and
            `reason` when it does not resolve.
        """
        return await run_operation(
            get_bridge,
            "get_document_dependencies",
            {"doc_name": doc_name, "max_links": max_links},
        )
