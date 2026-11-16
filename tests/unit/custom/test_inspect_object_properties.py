"""Tests for inspect_object's property filter and truncation flag."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from freecad_mcp.bridge.base import ObjectInfo
from freecad_mcp.bridge.object_inspection import build_object_inspection_code
from freecad_mcp.tools.objects import register_object_tools


class _CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *_args: Any, **_kwargs: Any) -> Any:
        def decorator(function: Any) -> Any:
            self.tools[function.__name__] = function
            return function

        return decorator


def _inspect_tool(bridge: Any) -> Any:
    mcp = _CapturingMCP()

    async def get_bridge() -> Any:
        return bridge

    register_object_tools(mcp, get_bridge)
    return mcp.tools["inspect_object"]


@pytest.mark.parametrize("truncated", [False, True])
async def test_passes_filter_and_reports_truncation(truncated: bool) -> None:
    bridge = AsyncMock()
    bridge.get_object.return_value = ObjectInfo(
        name="Pad",
        label="Pad",
        type_id="PartDesign::Pad",
        properties={"Length": 5.0},
        properties_truncated=truncated,
    )

    result = await _inspect_tool(bridge)(
        "Pad", doc_name="Doc", property_names=["Length"]
    )

    bridge.get_object.assert_awaited_once_with(
        "Pad",
        "Doc",
        include_properties=True,
        include_shape=True,
        property_names=["Length"],
    )
    assert result["properties"] == {"Length": 5.0}
    assert result["properties_truncated"] is truncated


async def test_no_truncation_key_without_properties() -> None:
    bridge = AsyncMock()
    bridge.get_object.return_value = ObjectInfo(
        name="Box", label="Box", type_id="Part::Box"
    )

    result = await _inspect_tool(bridge)("Box", include_properties=False)

    assert "properties" not in result
    assert "properties_truncated" not in result


def test_filter_reaches_generated_code() -> None:
    code = build_object_inspection_code(
        "Pad", "Doc", property_names=["Length", "Placement"]
    )

    assert "Length" in code
    assert "Placement" in code
    compile(code, "<inspect>", "exec")
