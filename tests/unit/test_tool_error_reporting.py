"""Unit tests for passing expected tool errors through to the MCP client.

The MCP SDK hides the message of any exception other than ToolError. These
tests check that the server's expected errors keep their message, that real
crashes stay hidden, and that wrapping does not change the tool definitions.
"""

import inspect
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

import freecad_mcp.server as server_module
from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools._registration import (
    MAX_ERROR_MESSAGE_LENGTH,
    ErrorReportingMCP,
    report_expected_errors,
)


@pytest.fixture
def bridge() -> Iterator[AsyncMock]:
    """Install a stand-in bridge on the server for the duration of a test."""
    saved = server_module._bridge, server_module._bridge_pending_connect
    stand_in = AsyncMock()
    server_module._bridge = stand_in
    server_module._bridge_pending_connect = False
    yield stand_in
    server_module._bridge, server_module._bridge_pending_connect = saved


class TestThroughServer:
    """Errors raised by registered tools, called through the real server."""

    async def test_value_error_message_reaches_client(self, bridge: AsyncMock) -> None:
        """A ValueError from a tool becomes a ToolError carrying its message."""
        bridge.execute_python = AsyncMock(
            return_value=ExecutionResult(
                success=False,
                result=None,
                stdout="",
                stderr="",
                execution_time_ms=1.0,
                error_type="ValueError",
                error_traceback="Alias not found: 'Nope'",
            )
        )
        with pytest.raises(ToolError, match="Alias not found: 'Nope'") as caught:
            await server_module.mcp.call_tool(
                "spreadsheet_bind_property",
                {
                    "spreadsheet_name": "Params",
                    "alias": "Nope",
                    "target_object": "Pad",
                    "target_property": "Length",
                },
            )
        assert not isinstance(caught.value, UnexpectedToolError)

    async def test_connection_error_message_reaches_client(
        self, bridge: AsyncMock
    ) -> None:
        """A ConnectionError (FreeCAD not running) keeps its message."""
        bridge.get_freecad_version = AsyncMock(
            side_effect=ConnectionError("FreeCAD is not reachable")
        )
        with pytest.raises(ToolError, match="FreeCAD is not reachable"):
            await server_module.mcp.call_tool("get_freecad_version", {})

    async def test_unexpected_errors_stay_hidden(self, bridge: AsyncMock) -> None:
        """A crash such as TypeError is still reported without details."""
        bridge.get_freecad_version = AsyncMock(side_effect=TypeError("internal"))
        with pytest.raises(UnexpectedToolError) as caught:
            await server_module.mcp.call_tool("get_freecad_version", {})
        assert "internal" not in str(caught.value)


class TestReportExpectedErrors:
    """The report_expected_errors wrapper on its own."""

    async def test_returns_result_unchanged(self) -> None:
        """Successful calls pass their result through."""

        async def tool(value: int) -> int:
            return value * 2

        assert await report_expected_errors(tool)(value=21) == 42

    async def test_long_messages_are_truncated(self) -> None:
        """Very long messages, such as full tracebacks, are shortened."""

        async def tool() -> None:
            raise ValueError("x" * (MAX_ERROR_MESSAGE_LENGTH + 500))

        with pytest.raises(ToolError) as caught:
            await report_expected_errors(tool)()
        assert str(caught.value).endswith("(truncated)")
        assert len(str(caught.value)) < MAX_ERROR_MESSAGE_LENGTH + 50

    async def test_empty_message_uses_type_name(self) -> None:
        """An exception without text reports its type name."""

        async def tool() -> None:
            raise TimeoutError

        with pytest.raises(ToolError, match="TimeoutError"):
            await report_expected_errors(tool)()

    async def test_tool_error_passes_through(self) -> None:
        """A ToolError raised by the tool is not wrapped again."""
        original = ToolError("already reported")

        async def tool() -> None:
            raise original

        with pytest.raises(ToolError) as caught:
            await report_expected_errors(tool)()
        assert caught.value is original

    def test_keeps_name_docstring_and_signature(self) -> None:
        """The wrapper presents the same metadata the SDK reads for schemas."""

        async def tool(name: str, count: int = 1) -> dict[str, int]:
            """Describe the tool."""
            return {name: count}

        wrapped = report_expected_errors(tool)
        assert wrapped.__name__ == "tool"
        assert wrapped.__doc__ == "Describe the tool."
        assert inspect.signature(wrapped) == inspect.signature(tool)
        assert inspect.iscoroutinefunction(wrapped)

    def test_leaves_sync_functions_alone(self) -> None:
        """Non-async functions are returned unchanged."""

        def tool() -> int:
            return 1

        assert report_expected_errors(tool) is tool


def test_proxy_registers_wrapped_tools_and_passes_attributes() -> None:
    """ErrorReportingMCP wraps tools and forwards other attributes."""
    server = MagicMock()
    registered = []
    server.tool = MagicMock(return_value=registered.append)
    proxy = ErrorReportingMCP(server)

    async def tool() -> None:
        raise ValueError("visible")

    proxy.tool()(tool)

    assert len(registered) == 1
    assert registered[0] is not tool
    assert registered[0].__wrapped__ is tool
    assert proxy.name is server.name
