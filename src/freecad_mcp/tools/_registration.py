"""Helpers for registering MCP tools.

Includes the gate for tools that expose arbitrary code execution, and a
wrapper that lets the server's own error messages reach the MCP client.

The MCP SDK treats any exception other than ``ToolError`` as a crash and
replaces its message with ``Error executing tool <name>``. The tools in this
package report expected failures (a missing object, an invalid argument,
FreeCAD not running) with ordinary exceptions such as ``ValueError``, so
without the wrapper the client never sees why a call failed.
"""

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

# Exception types the tools raise on purpose, with a message meant for the
# caller. Anything else (TypeError, AttributeError, RuntimeError, ...) is
# still treated as a crash and its details stay hidden, as the SDK intends.
EXPECTED_TOOL_ERRORS: tuple[type[BaseException], ...] = (
    ValueError,
    ConnectionError,
    TimeoutError,
    FileNotFoundError,
    FileExistsError,
    PermissionError,
    IsADirectoryError,
    NotADirectoryError,
)

# Bridge errors can carry a full FreeCAD traceback; keep replies readable.
MAX_ERROR_MESSAGE_LENGTH = 4000


def _tool_error_message(error: BaseException) -> str:
    """Return the message to report for an expected tool error.

    Args:
        error: The exception raised by the tool.

    Returns:
        The exception text (or its type name when empty), truncated to
        MAX_ERROR_MESSAGE_LENGTH characters.
    """
    message = str(error).strip() or type(error).__name__
    if len(message) > MAX_ERROR_MESSAGE_LENGTH:
        message = message[:MAX_ERROR_MESSAGE_LENGTH] + " ... (truncated)"
    return message


def report_expected_errors(
    function: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Wrap an async tool so expected errors reach the client with their message.

    The wrapped function keeps the original name, docstring and signature, so
    the SDK builds the same tool schema from it.

    Args:
        function: The async tool function to wrap.

    Returns:
        An async function that re-raises EXPECTED_TOOL_ERRORS as ToolError.
    """
    if not inspect.iscoroutinefunction(function):
        return function

    @functools.wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await function(*args, **kwargs)
        except ToolError:
            raise
        except EXPECTED_TOOL_ERRORS as error:
            raise ToolError(_tool_error_message(error)) from error

    return wrapper


class ErrorReportingMCP:
    """Proxy for an MCP server whose ``tool()`` decorator reports expected errors.

    Registration modules call ``mcp.tool()`` as usual; tools registered through
    this proxy are wrapped with report_expected_errors first. All other
    attributes are passed through to the real server.

    Attributes:
        server: The wrapped MCP server.
    """

    def __init__(self, server: Any) -> None:
        """Wrap an MCP server.

        Args:
            server: The MCP server to register tools on.
        """
        self.server = server

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        """Return a decorator that wraps a tool and registers it.

        Args:
            *args: Positional arguments for the server's ``tool()``.
            **kwargs: Keyword arguments for the server's ``tool()``.

        Returns:
            A decorator that registers the wrapped tool.
        """
        register = self.server.tool(*args, **kwargs)

        def decorator(function: Any) -> Any:
            return register(report_expected_errors(function))

        return decorator

    def __getattr__(self, name: str) -> Any:
        """Pass other attribute lookups to the wrapped server."""
        return getattr(self.server, name)


def unsafe_tool(mcp: Any, enabled: bool) -> Callable[[Any], Any]:
    """Register a code-execution tool only when the unsafe profile is enabled."""
    if enabled:
        return mcp.tool()
    return lambda function: function
