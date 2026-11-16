"""FreeCAD-side code for the fork's custom tools.

The MCP server's custom tools call ``dispatch`` through the
``_p3d_dispatch`` name that the bridge puts into every execution
environment, passing an API version, an operation name and a JSON-safe
argument dictionary. Keeping the code here, instead of sending it as text with
each call, means it can be linted, type-checked and unit tested like any other
module.

Each module in this package that defines an ``OPERATIONS`` dictionary
(operation name to function) is picked up automatically, so groups of tools
can be added without editing a shared list. Functions take the argument
dictionary and return a JSON-safe value. They should raise ``ValueError`` for
problems the caller can fix (a missing object, a bad argument): the message
reaches the MCP client.

Modules here must only import FreeCAD inside functions, so this package can be
imported and tested without FreeCAD.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from typing import Any

# Bump when an operation's arguments or results change incompatibly. The MCP
# server sends the version it expects; a mismatch gives a clear error telling
# the user which side to update.
P3D_API_VERSION = 1

Operation = Callable[[dict[str, Any]], Any]

_operations: dict[str, Operation] | None = None
_load_errors: dict[str, str] = {}


def _load_operations() -> dict[str, Operation]:
    """Import every module in this package and collect its operations.

    A module that fails to import is skipped and recorded in ``_load_errors``,
    so one broken group doesn't disable the others.

    Returns:
        Mapping of operation name to function.

    Raises:
        RuntimeError: If two modules define the same operation name.
    """
    global _operations
    if _operations is not None:
        return _operations
    found: dict[str, Operation] = {}
    owners: dict[str, str] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as error:  # recorded and reported
            _load_errors[info.name] = f"{type(error).__name__}: {error}"
            continue
        for name, function in getattr(module, "OPERATIONS", {}).items():
            if name in found:
                raise RuntimeError(
                    f"Custom runtime operation {name!r} is defined in both "
                    f"{owners[name]!r} and {info.name!r}"
                )
            found[name] = function
            owners[name] = info.name
    _operations = found
    return found


def available_operations() -> list[str]:
    """Return the names of all loaded operations, sorted.

    Returns:
        Operation names.
    """
    return sorted(_load_operations())


def load_errors() -> dict[str, str]:
    """Return import errors for modules that were skipped.

    Returns:
        Error summary by module name.
    """
    _load_operations()
    return dict(_load_errors)


def dispatch(version: int, operation: str, args: dict[str, Any]) -> Any:
    """Run a custom operation for a compatible MCP server.

    Args:
        version: The API version the MCP server was built for.
        operation: Name of the operation to run.
        args: JSON-safe arguments for the operation.

    Returns:
        The operation's JSON-safe result.

    Raises:
        RuntimeError: If the server and workbench versions don't match.
        ValueError: If the operation is unknown or the arguments are not an
            object, or as raised by the operation itself.
    """
    if version != P3D_API_VERSION:
        side = "workbench" if version > P3D_API_VERSION else "MCP server"
        raise RuntimeError(
            "Incompatible custom tools interface: the Robust MCP Bridge "
            f"workbench provides version {P3D_API_VERSION} and the MCP server "
            f"expects version {version}. Update the {side} from the same "
            "branch, then restart FreeCAD."
        )
    if not isinstance(args, dict):
        raise ValueError("Custom operation arguments must be an object")
    operations = _load_operations()
    if operation not in operations:
        detail = ""
        if _load_errors:
            detail = " Modules that failed to load: " + "; ".join(
                f"{name} ({error})" for name, error in sorted(_load_errors.items())
            )
        raise ValueError(f"Unknown custom operation: {operation!r}.{detail}")
    return operations[operation](args)
