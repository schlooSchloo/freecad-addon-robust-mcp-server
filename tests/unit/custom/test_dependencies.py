"""Unit tests for the get_document_dependencies tool (server side)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools.custom.dependencies import register_dependencies_tools


@pytest.fixture
def tool_and_bridge() -> tuple[Any, MagicMock]:
    captured: dict[str, Any] = {}
    mcp = MagicMock()
    mcp.tool.return_value = lambda fn: captured.setdefault(fn.__name__, fn)
    bridge = MagicMock()
    bridge.execute_python = AsyncMock(
        return_value=ExecutionResult(
            success=True,
            result={"links": []},
            stdout="",
            stderr="",
            execution_time_ms=1.0,
        )
    )

    async def get_bridge() -> MagicMock:
        return bridge

    register_dependencies_tools(mcp, get_bridge)
    return captured["get_document_dependencies"], bridge


def test_registered_read_only() -> None:
    mcp = MagicMock()
    register_dependencies_tools(mcp, AsyncMock())
    annotations = mcp.tool.call_args.kwargs["annotations"]
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False


async def test_sends_arguments_as_json(tool_and_bridge: tuple[Any, MagicMock]) -> None:
    tool, bridge = tool_and_bridge
    result = await tool(doc_name="MyDoc", max_links=5)
    assert result == {"links": []}
    code = bridge.execute_python.call_args.args[0]
    compile(code, "<test>", "exec")
    assert "get_document_dependencies" in code
    assert json.dumps({"doc_name": "MyDoc", "max_links": 5}) in code


async def test_none_document_is_left_out(
    tool_and_bridge: tuple[Any, MagicMock],
) -> None:
    tool, bridge = tool_and_bridge
    await tool()
    assert "doc_name" not in bridge.execute_python.call_args.args[0].split("loads(")[1]
