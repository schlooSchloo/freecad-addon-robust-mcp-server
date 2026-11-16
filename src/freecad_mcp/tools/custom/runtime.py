"""Call the fork's custom runtime inside FreeCAD.

Custom tools keep their FreeCAD-side code in the workbench
(``freecad_mcp_bridge/custom_runtime/``) rather than sending it as text. This
module builds the small program that calls that code through the bridge's
``_p3d_dispatch`` entry point, with the arguments passed as JSON so model
text can never become code.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from freecad_mcp.tools.utils import wrap_with_transaction

# Must match P3D_API_VERSION in the workbench's custom_runtime package.
P3D_API_VERSION = 1

DEFAULT_TIMEOUT_MS = 120_000

MISSING_RUNTIME = (
    "This tool needs the Robust MCP Bridge workbench from the fork's custom "
    "branch, which provides the custom runtime. Update the workbench and "
    "restart FreeCAD. (Custom tools are not available in embedded mode.)"
)

_INCOMPATIBLE_PREFIX = "Incompatible custom tools interface:"


def build_runtime_code(
    operation: str,
    args: dict[str, Any],
    *,
    transaction: str | None = None,
    doc_name: str | None = None,
) -> str:
    """Build the program that runs one custom runtime operation.

    Args:
        operation: Name of the runtime operation.
        args: JSON-safe arguments for the operation.
        transaction: If given, wrap the call in a FreeCAD transaction (an undo
            step) with this name.
        doc_name: Document the transaction belongs to. Defaults to the active
            document.

    Returns:
        Python source to send with ``bridge.execute_python``.
    """
    payload = json.dumps(args)
    check = (
        "import json as _p3d_json\n"
        "if '_p3d_dispatch' not in globals():\n"
        f"    raise RuntimeError({MISSING_RUNTIME!r})\n"
    )
    call = (
        f"_result_ = _p3d_dispatch({P3D_API_VERSION!r}, {operation!r}, "
        f"_p3d_json.loads({payload!r}))\n"
    )
    if transaction:
        doc_expr = (
            f"FreeCAD.getDocument({doc_name!r})"
            if doc_name
            else "FreeCAD.ActiveDocument"
        )
        call = wrap_with_transaction(call, transaction, doc_expr=doc_expr)
    return check + call


def _last_line(detail: str) -> str:
    """Return the last non-empty line of a traceback."""
    lines = [line for line in detail.strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


async def run_operation(
    get_bridge: Callable[[], Awaitable[Any]],
    operation: str,
    args: dict[str, Any],
    *,
    transaction: str | None = None,
    doc_name: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> Any:
    """Run a custom runtime operation and return its result.

    Arguments whose value is None are left out, so the runtime's defaults
    apply.

    Args:
        get_bridge: Async function returning the active bridge.
        operation: Name of the runtime operation.
        args: Arguments for the operation.
        transaction: Optional undo-step name; see build_runtime_code.
        doc_name: Document the transaction belongs to.
        timeout_ms: Execution timeout.

    Returns:
        The operation's result.

    Raises:
        ToolError: If the workbench lacks the custom runtime or its version
            doesn't match this server.
        ValueError: If the operation failed in FreeCAD. The message is
            FreeCAD's final error line.
    """
    bridge = await get_bridge()
    clean = {key: value for key, value in args.items() if value is not None}
    code = build_runtime_code(
        operation, clean, transaction=transaction, doc_name=doc_name
    )
    result = await bridge.execute_python(code, timeout_ms=timeout_ms)
    if result.success:
        return result.result
    detail = result.error_traceback or result.stderr or ""
    last = _last_line(detail) or f"{operation} failed in FreeCAD"
    for prefix in ("RuntimeError: ", "ValueError: "):
        if last.startswith(prefix):
            message = last.removeprefix(prefix)
            if message == MISSING_RUNTIME or message.startswith(_INCOMPATIBLE_PREFIX):
                raise ToolError(message)
            if prefix == "ValueError: ":
                raise ValueError(message)
    raise ValueError(last)
