"""Fail if a custom tool shares a name with an upstream tool.

The MCP SDK keeps the first tool registered under a name and drops later ones
with only a warning. Custom tools are registered after upstream's, so a clash
would silently replace our tool with upstream's. This test makes an upstream
merge that introduces a clash fail loudly, so we can decide whether to drop,
rename or keep our version.
"""

from collections import Counter
from typing import Any

import pytest

from freecad_mcp.tools import TOOL_MODULES
from freecad_mcp.tools.custom import CUSTOM_TOOL_MODULES
from freecad_mcp.tools.custom.loader import ToolModule, _stage_module


async def _get_bridge() -> Any:
    return None


def _names(modules: tuple[ToolModule, ...]) -> list[str]:
    """Return every tool name the modules register, unsafe tools included."""
    return [
        tool.name
        for spec in modules
        for tool in _stage_module(spec, _get_bridge, allow_unsafe_code=True)
    ]


def _repeated(names: list[str]) -> list[str]:
    return sorted(name for name, count in Counter(names).items() if count > 1)


def test_custom_modules_are_listed():
    assert CUSTOM_TOOL_MODULES, "CUSTOM_TOOL_MODULES should not be empty"


def test_custom_tools_do_not_clash_with_upstream():
    clashes = sorted(set(_names(TOOL_MODULES)) & set(_names(CUSTOM_TOOL_MODULES)))

    assert not clashes, (
        "Upstream now has tools with the same name as these custom tools: "
        f"{clashes}. Upstream's version wins at runtime. Drop or rename ours, "
        "and note the decision in CUSTOM_CHANGES.md."
    )


@pytest.mark.parametrize(
    "modules", [TOOL_MODULES, CUSTOM_TOOL_MODULES], ids=["upstream", "custom"]
)
def test_no_repeated_names_within_a_group(modules):
    assert _repeated(_names(modules)) == []


def test_module_labels_are_unique():
    labels = [spec.label for spec in (*TOOL_MODULES, *CUSTOM_TOOL_MODULES)]

    assert _repeated(labels) == []


def test_custom_labels_are_prefixed():
    assert all(spec.label.startswith("custom.") for spec in CUSTOM_TOOL_MODULES)
