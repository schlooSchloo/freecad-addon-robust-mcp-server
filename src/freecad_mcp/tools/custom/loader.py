"""Fail-safe loading of tool modules.

Each tool module is imported and registered on its own. If a module cannot be
imported, or raises while registering its tools, it is skipped and logged and
the rest of the server still starts. A module that fails part-way adds none of
its tools, because registrations are collected first and only added to the
server once the whole module has registered without error.

The loader also refuses to register two tools with the same name. The MCP SDK
keeps the first tool registered under a name and drops later ones with only a
warning; the loader makes that visible as an error and records which module
won.

What happened is kept in a RegistrationReport, which the
``get_tool_registration_status`` tool returns to the client.
"""

import importlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Reported error messages are cut to this length; full tracebacks go to the log.
MAX_REPORTED_ERROR_LENGTH = 500


@dataclass(frozen=True)
class ToolModule:
    """A module that registers a group of MCP tools.

    Attributes:
        label: Short name for the group, used in logs and the report
            (e.g. ``"partdesign"`` or ``"custom.status"``).
        module: Import path of the module.
        function: Name of the module's registration function. It is called as
            ``function(mcp, get_bridge)``, plus ``allow_unsafe_code=...`` when
            the function accepts that keyword.
    """

    label: str
    module: str
    function: str


@dataclass
class RegistrationReport:
    """What happened while registering tool modules.

    Attributes:
        registered: Tool names registered, by module label.
        failed_modules: Error summary for each module that was skipped.
        failed_tools: Error summary for each single tool the server rejected,
            keyed ``"<module label>.<tool name>"``.
        duplicates: Tools skipped because an earlier module already registered
            a tool with the same name.
    """

    registered: dict[str, list[str]] = field(default_factory=dict)
    failed_modules: dict[str, str] = field(default_factory=dict)
    failed_tools: dict[str, str] = field(default_factory=dict)
    duplicates: list[dict[str, str]] = field(default_factory=list)

    def owners(self) -> dict[str, str]:
        """Return the module label that registered each tool name.

        Returns:
            Mapping of tool name to module label.
        """
        return {
            name: label for label, names in self.registered.items() for name in names
        }

    def as_dict(self) -> dict[str, Any]:
        """Summarise the report for an MCP client.

        Returns:
            Dictionary with:
                - ok: True if nothing failed and there were no duplicate names
                - tool_count: Number of tools registered
                - modules: Number of tools registered by each module
                - failed_modules: Error summary for each skipped module
                - failed_tools: Error summary for each rejected tool
                - duplicate_tools: Tools skipped because of a name clash
        """
        return {
            "ok": not (self.failed_modules or self.failed_tools or self.duplicates),
            "tool_count": sum(len(names) for names in self.registered.values()),
            "modules": {label: len(names) for label, names in self.registered.items()},
            "failed_modules": dict(self.failed_modules),
            "failed_tools": dict(self.failed_tools),
            "duplicate_tools": list(self.duplicates),
        }


_current_report = RegistrationReport()


def start_registration_report() -> RegistrationReport:
    """Start a new report for a fresh round of registration.

    Returns:
        The new, empty report, which is also what get_registration_report
        returns from now on.
    """
    global _current_report
    _current_report = RegistrationReport()
    return _current_report


def get_registration_report() -> RegistrationReport:
    """Return the report from the most recent round of registration.

    Returns:
        The current RegistrationReport.
    """
    return _current_report


@dataclass
class _PendingTool:
    """A tool registration collected by _StagingMCP, not yet on the server."""

    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    function: Callable[..., Any]

    @property
    def name(self) -> str:
        """The name the server will register the tool under."""
        name = self.kwargs.get("name")
        if name is None and self.args and isinstance(self.args[0], str):
            name = self.args[0]
        return name or self.function.__name__


class _StagingMCP:
    """Stand-in server that collects ``tool()`` registrations without applying them.

    Attributes:
        pending: Registrations collected so far, in order.
    """

    def __init__(self) -> None:
        """Create an empty staging area."""
        self.pending: list[_PendingTool] = []

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        """Return a decorator that records a tool registration.

        Args:
            *args: Positional arguments for the server's ``tool()``.
            **kwargs: Keyword arguments for the server's ``tool()``.

        Returns:
            A decorator that records the function and returns it unchanged.
        """

        def decorator(function: Any) -> Any:
            self.pending.append(_PendingTool(args, kwargs, function))
            return function

        return decorator


def _summarise(error: BaseException) -> str:
    """Return a short, single-line description of an exception.

    Args:
        error: The exception to describe.

    Returns:
        ``"<ExceptionType>: <message>"``, cut to MAX_REPORTED_ERROR_LENGTH.
    """
    text = " ".join(str(error).split())
    summary = f"{type(error).__name__}: {text}" if text else type(error).__name__
    if len(summary) > MAX_REPORTED_ERROR_LENGTH:
        summary = summary[:MAX_REPORTED_ERROR_LENGTH] + " ... (truncated)"
    return summary


def _stage_module(
    spec: ToolModule,
    get_bridge: Callable[[], Awaitable[Any]],
    allow_unsafe_code: bool,
) -> list[_PendingTool]:
    """Import a tool module and collect its registrations.

    Args:
        spec: The module to load.
        get_bridge: Async function returning the active bridge connection.
        allow_unsafe_code: Passed on if the registration function accepts it.

    Returns:
        The module's tool registrations, in order.

    Raises:
        Exception: Anything raised while importing the module or calling its
            registration function.
    """
    module = importlib.import_module(spec.module)
    register = getattr(module, spec.function)
    kwargs: dict[str, Any] = {}
    if "allow_unsafe_code" in inspect.signature(register).parameters:
        kwargs["allow_unsafe_code"] = allow_unsafe_code
    staging = _StagingMCP()
    register(staging, get_bridge, **kwargs)
    return staging.pending


def register_tool_modules(
    mcp: Any,
    modules: Iterable[ToolModule],
    get_bridge: Callable[[], Awaitable[Any]],
    *,
    allow_unsafe_code: bool = False,
    report: RegistrationReport | None = None,
) -> RegistrationReport:
    """Register tool modules one at a time, skipping any that fail.

    Modules are registered in the order given. When two tools share a name,
    the first one registered is kept and the later one is skipped and logged
    as an error.

    Args:
        mcp: The server (or server proxy) to register tools on.
        modules: The tool modules to register.
        get_bridge: Async function returning the active bridge connection.
        allow_unsafe_code: Whether to expose tools that execute arbitrary code.
        report: Report to add results to. Defaults to the current report.

    Returns:
        The report the results were added to.
    """
    if report is None:
        report = get_registration_report()
    owners = report.owners()

    for spec in modules:
        try:
            pending = _stage_module(spec, get_bridge, allow_unsafe_code)
        except Exception as error:
            report.failed_modules[spec.label] = _summarise(error)
            logger.exception(
                "Skipped tool module %r (%s); its tools are unavailable",
                spec.label,
                spec.module,
            )
            continue

        names = report.registered.setdefault(spec.label, [])
        for tool in pending:
            name = tool.name
            if name in owners:
                report.duplicates.append(
                    {
                        "tool": name,
                        "kept_from": owners[name],
                        "skipped_from": spec.label,
                    }
                )
                logger.error(
                    "Tool name %r from module %r is already registered by %r; "
                    "skipped the %r version",
                    name,
                    spec.label,
                    owners[name],
                    spec.label,
                )
                continue
            try:
                mcp.tool(*tool.args, **tool.kwargs)(tool.function)
            except Exception as error:
                report.failed_tools[f"{spec.label}.{name}"] = _summarise(error)
                logger.exception(
                    "Skipped tool %r from module %r; the server rejected it",
                    name,
                    spec.label,
                )
                continue
            owners[name] = spec.label
            names.append(name)

    return report
