"""Unit tests for starting the server before FreeCAD is running (lazy_connect)."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import freecad_mcp.server as server_module
from freecad_mcp.config import FreecadMode, ServerConfig


def _config(*, lazy_connect: bool) -> MagicMock:
    """Build an XML-RPC server configuration stand-in."""
    config = MagicMock()
    config.mode = FreecadMode.XMLRPC
    config.socket_host = "localhost"
    config.xmlrpc_port = 9875
    config.timeout_ms = 30000
    config.lazy_connect = lazy_connect
    return config


def _bridge(connect_errors: int) -> AsyncMock:
    """Build a bridge whose first ``connect_errors`` connects are refused."""
    bridge = AsyncMock()
    bridge.connect = AsyncMock(
        side_effect=[ConnectionError("refused")] * connect_errors + [None] * 5
    )
    bridge.get_freecad_version = AsyncMock(
        return_value={"version": "1.1.4", "gui_available": True}
    )
    return bridge


@contextmanager
def _patched(config: MagicMock, bridge: AsyncMock) -> Iterator[None]:
    """Patch configuration, credentials and the XML-RPC bridge class."""
    with (
        patch.object(server_module, "get_config", return_value=config),
        patch.object(
            server_module,
            "credentials_from_config",
            return_value=("paired", MagicMock()),
        ),
        patch("freecad_mcp.bridge.xmlrpc.XmlRpcBridge", return_value=bridge),
    ):
        yield


@pytest.fixture(autouse=True)
def _restore_server_state() -> Iterator[None]:
    """Restore the module-level bridge state after each test."""
    saved = (
        server_module._bridge,
        server_module._bridge_pending_connect,
        server_module._connect_lock,
    )
    server_module._connect_lock = None
    yield
    (
        server_module._bridge,
        server_module._bridge_pending_connect,
        server_module._connect_lock,
    ) = saved


def test_lazy_connect_defaults_to_off() -> None:
    """Existing setups keep exiting when FreeCAD is missing unless opted in."""
    assert ServerConfig.model_fields["lazy_connect"].default is False


async def test_without_lazy_connect_startup_still_fails() -> None:
    """Without lazy_connect a missing FreeCAD still stops startup."""
    bridge = _bridge(connect_errors=1)
    with (
        _patched(_config(lazy_connect=False), bridge),
        pytest.raises(ConnectionError),
    ):
        async with server_module.lifespan(MagicMock()):
            pytest.fail("lifespan should not start")


async def test_lazy_connect_starts_without_freecad() -> None:
    """With lazy_connect the server starts and reports FreeCAD as unreachable."""
    bridge = _bridge(connect_errors=2)
    with _patched(_config(lazy_connect=True), bridge):
        async with server_module.lifespan(MagicMock()):
            with pytest.raises(ConnectionError, match="FreeCAD is not reachable"):
                await server_module.get_bridge()
            assert bridge.connect.await_count == 2

        # Never connected, so shutdown must not try to disconnect.
        bridge.disconnect.assert_not_awaited()


async def test_lazy_connect_connects_once_freecad_is_running() -> None:
    """A later tool call connects once FreeCAD has been started."""
    bridge = _bridge(connect_errors=1)
    with _patched(_config(lazy_connect=True), bridge):
        async with server_module.lifespan(MagicMock()):
            assert await server_module.get_bridge() is bridge
            assert await server_module.get_bridge() is bridge
            assert bridge.connect.await_count == 2
            bridge.get_freecad_version.assert_awaited_once()

        bridge.disconnect.assert_awaited_once()


async def test_lazy_connect_with_freecad_running_connects_at_startup() -> None:
    """If FreeCAD is already running, the server connects at startup as before."""
    bridge = _bridge(connect_errors=0)
    with _patched(_config(lazy_connect=True), bridge):
        async with server_module.lifespan(MagicMock()):
            assert server_module._bridge_pending_connect is False
            assert await server_module.get_bridge() is bridge
            assert bridge.connect.await_count == 1


async def test_concurrent_tool_calls_connect_once() -> None:
    """Simultaneous first tool calls share one connection attempt."""
    bridge = _bridge(connect_errors=1)

    async def slow_connect() -> None:
        await asyncio.sleep(0.01)

    with _patched(_config(lazy_connect=True), bridge):
        async with server_module.lifespan(MagicMock()):
            bridge.connect.side_effect = slow_connect
            results = await asyncio.gather(
                *(server_module.get_bridge() for _ in range(5))
            )
            assert all(result is bridge for result in results)
            assert bridge.connect.await_count == 2
