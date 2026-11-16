"""Report on the custom runtime itself.

Used by the MCP server to check that the installed workbench has the custom
runtime and which operations it provides.
"""

from __future__ import annotations

from typing import Any


def runtime_info(_args: dict[str, Any]) -> dict[str, Any]:
    """Describe the installed custom runtime.

    Args:
        _args: Unused.

    Returns:
        Dictionary with the API version, available operations, modules that
        failed to load, and the FreeCAD version and GUI state.
    """
    from . import P3D_API_VERSION, available_operations, load_errors

    info: dict[str, Any] = {
        "api_version": P3D_API_VERSION,
        "operations": available_operations(),
        "load_errors": load_errors(),
    }
    try:
        import FreeCAD

        info["freecad_version"] = ".".join(str(part) for part in FreeCAD.Version()[:3])
        info["gui_up"] = bool(FreeCAD.GuiUp)
    except ImportError:
        info["freecad_version"] = None
        info["gui_up"] = False
    return info


OPERATIONS = {"runtime_info": runtime_info}
