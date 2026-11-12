"""Tools added on the schlooSchloo fork's ``custom`` branch.

Everything here is kept apart from upstream's tool modules so that merging
upstream changes rarely conflicts. ``freecad_mcp.tools`` registers the modules
listed in CUSTOM_TOOL_MODULES after upstream's own, so if upstream later adds
a tool with the same name, upstream's version is kept and the clash is logged
(``tests/unit/custom/test_tool_name_collisions.py`` also fails).

To add a group of tools:

1. Create a module in this package with a ``register_<group>_tools(mcp,
   get_bridge)`` function. Accept ``allow_unsafe_code`` as a keyword argument
   only if the group has tools that run arbitrary code.
2. Add a ToolModule entry for it to CUSTOM_TOOL_MODULES.
3. Add tests under ``tests/unit/custom/``, and record the change in
   CUSTOM_CHANGES.md.

Keep this file light: it is imported at startup, so it should only list
modules, never import them.
"""

from freecad_mcp.tools.custom.loader import ToolModule

__all__ = ["CUSTOM_TOOL_MODULES"]

CUSTOM_TOOL_MODULES: tuple[ToolModule, ...] = (
    ToolModule(
        "custom.status",
        "freecad_mcp.tools.custom.status",
        "register_status_tools",
    ),
)
