"""The custom runtime is installed with the workbench and reachable."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools.custom.runtime import P3D_API_VERSION, run_operation
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration


@pytest.fixture
async def bridge() -> AsyncIterator[XmlRpcBridge]:
    """A connected bridge to the test FreeCAD."""
    connection = XmlRpcBridge(**integration_credentials())
    await connection.connect()
    try:
        yield connection
    finally:
        await connection.disconnect()


@pytest.mark.asyncio
async def test_runtime_info(bridge: XmlRpcBridge) -> None:
    """The installed workbench reports a matching version and no load errors."""

    async def get_bridge() -> Any:
        return bridge

    info = await run_operation(get_bridge, "runtime_info", {})

    assert info["api_version"] == P3D_API_VERSION
    assert "runtime_info" in info["operations"]
    assert info["load_errors"] == {}
    assert info["freecad_version"].startswith(("1.0", "1.1"))


@pytest.mark.asyncio
async def test_unknown_operation_is_a_clear_error(bridge: XmlRpcBridge) -> None:
    """Unknown operations come back as a ValueError naming the operation."""

    async def get_bridge() -> Any:
        return bridge

    with pytest.raises(ValueError, match="Unknown custom operation: 'no_such_op'"):
        await run_operation(get_bridge, "no_such_op", {})
