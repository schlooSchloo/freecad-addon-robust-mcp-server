"""Tools added on the schlooSchloo fork's ``custom`` branch.

Everything here is kept apart from upstream's tool modules so that merging
upstream changes rarely conflicts. ``freecad_mcp.tools`` registers the modules
listed in CUSTOM_TOOL_MODULES after upstream's own, so if upstream later adds
a tool with the same name, upstream's version is kept and the clash is logged
(``tests/unit/custom/test_tool_name_collisions.py`` also fails).

Tool modules are found automatically, so adding a group never edits a shared
list (which would make separate pull requests conflict). To add a group:

1. Create ``<group>.py`` in this package with a ``register_<group>_tools(mcp,
   get_bridge)`` function. Accept ``allow_unsafe_code`` as a keyword argument
   only if the group has tools that run arbitrary code.
2. Add tests under ``tests/unit/custom/``, and record the change in
   CUSTOM_CHANGES.md.

Helper modules that register no tools must start with an underscore, or be
listed in _HELPER_MODULES.

Keep this file light: it is imported at startup, so it lists module names
without importing them. The fail-safe loader imports each module later, and
skips and reports any that fail.
"""

import pkgutil

from freecad_mcp.tools.custom.loader import ToolModule

__all__ = ["CUSTOM_TOOL_MODULES"]

# Modules in this package that hold shared code, not tools.
_HELPER_MODULES = frozenset({"loader", "runtime", "shape_snippets"})


def _discover_tool_modules() -> tuple[ToolModule, ...]:
    """List this package's tool modules, sorted by name, without importing them.

    Returns:
        One ToolModule per module, labelled ``custom.<module>`` and registered
        by its ``register_<module>_tools`` function.
    """
    names = sorted(
        info.name
        for info in pkgutil.iter_modules(__path__)
        if not info.name.startswith("_") and info.name not in _HELPER_MODULES
    )
    return tuple(
        ToolModule(f"custom.{name}", f"{__name__}.{name}", f"register_{name}_tools")
        for name in names
    )


CUSTOM_TOOL_MODULES: tuple[ToolModule, ...] = _discover_tool_modules()
