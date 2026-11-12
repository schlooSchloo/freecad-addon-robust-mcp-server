"""Check that every tool sends valid Python to FreeCAD.

Most tools build FreeCAD code as text and send it with
``bridge.execute_python``. Linters and type checkers cannot see inside that
text, so a typo in it only shows up when FreeCAD runs it. This test calls
every registered tool against a mock bridge, with sample arguments made from
the tool's signature, and compiles each piece of code the tool sends.

Each tool runs twice: once with only its required arguments, and once with
every optional argument filled in and booleans flipped from their defaults, so
more branches of the code builder are reached.

Tools that use the bridge's higher-level methods (``create_object``,
``get_objects``, ...) instead of sending code pass as long as they call the
bridge. The code those bridge methods build is not covered here.

If a new tool fails because it rejected a sample value before calling the
bridge, add arguments that it accepts to ARGUMENT_OVERRIDES. If it never
contacts FreeCAD, add it to NO_BRIDGE_TOOLS.

Compiling only catches syntax errors, not names that are undefined at run
time. SyntaxWarnings are ignored: many tools build lines such as
``'Doc' is None``, which Python warns about but evaluates correctly (see
CUSTOM_CHANGES.md).
"""

import enum
import inspect
import types
import typing
import warnings
from typing import Any
from unittest.mock import AsyncMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools import TOOL_MODULES
from freecad_mcp.tools.custom import CUSTOM_TOOL_MODULES
from freecad_mcp.tools.custom.loader import _PendingTool, _stage_module

# Arguments for tools that validate a value before building any code.
ARGUMENT_OVERRIDES: dict[str, dict[str, Any]] = {
    "boolean_operation": {"operation": "fuse"},
    "draft_text_on_surface": {"position": [0.0, 0.0]},
    "set_view_angle": {"view_angle": "Isometric"},
}

# Tools that answer without contacting FreeCAD.
NO_BRIDGE_TOOLS: frozenset[str] = frozenset({"get_tool_registration_status"})

SAMPLE_STRING = "Sample"
SAMPLE_VALUES: dict[Any, Any] = {
    str: SAMPLE_STRING,
    int: 1,
    float: 1.0,
    bool: False,
    dict: {},
    list: [],
}


def _sample(annotation: Any) -> Any:
    """Return a plausible value for a parameter annotation."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        return _sample(next(a for a in args if a is not type(None)))
    if origin is typing.Literal:
        return args[0]
    if origin is not None:
        return _sample_container(origin, args)
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return next(iter(annotation))
    return SAMPLE_VALUES.get(annotation, SAMPLE_STRING)


def _sample_container(origin: Any, args: tuple[Any, ...]) -> Any:
    """Return a plausible value for a generic container annotation."""
    if origin is tuple:
        return tuple(_sample(a) for a in args if a is not Ellipsis)
    if origin is dict:
        return {}
    # list, Sequence and other iterables: three items.
    return [_sample(args[0] if args else str) for _ in range(3)]


def _arguments(tool: _PendingTool, *, fill_optional: bool) -> dict[str, Any]:
    """Build keyword arguments for a tool from its signature."""
    hints = typing.get_type_hints(tool.function)
    arguments: dict[str, Any] = {}
    for name, parameter in inspect.signature(tool.function).parameters.items():
        annotation = hints.get(name, str)
        if parameter.default is inspect.Parameter.empty:
            arguments[name] = _sample(annotation)
        elif fill_optional:
            if parameter.default is None:
                arguments[name] = _sample(annotation)
            elif isinstance(parameter.default, bool):
                arguments[name] = not parameter.default
    arguments.update(ARGUMENT_OVERRIDES.get(tool.name, {}))
    return arguments


# The bridge the tools get. Each test sets its own mock here.
_current_bridge: dict[str, Any] = {}


async def _get_bridge() -> Any:
    return _current_bridge["bridge"]


def _all_tools() -> list[_PendingTool]:
    return [
        tool
        for spec in (*TOOL_MODULES, *CUSTOM_TOOL_MODULES)
        for tool in _stage_module(spec, _get_bridge, allow_unsafe_code=True)
    ]


ALL_TOOLS = _all_tools()


def _make_bridge() -> tuple[AsyncMock, list[str]]:
    """Return a mock bridge and the list it records sent code into."""
    sent: list[str] = []

    async def execute_python(code: str, *_args: Any, **_kwargs: Any) -> Any:
        sent.append(code)
        return ExecutionResult(
            success=True,
            result={},
            stdout="",
            stderr="",
            execution_time_ms=1.0,
        )

    bridge = AsyncMock()
    bridge.execute_python = AsyncMock(side_effect=execute_python)
    return bridge, sent


def _compile_error(code: str, tool_name: str) -> str | None:
    """Return the syntax error in code, or None if it compiles."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            compile(code, f"<{tool_name}>", "exec")
        except SyntaxError as error:
            return f"{error.msg} (line {error.lineno}): {error.text!r}"
    return None


@pytest.mark.parametrize("fill_optional", [False, True], ids=["required", "all"])
@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda tool: tool.name)
async def test_tool_sends_valid_python(tool: _PendingTool, fill_optional: bool):
    bridge, sent = _make_bridge()
    _current_bridge["bridge"] = bridge
    arguments = _arguments(tool, fill_optional=fill_optional)

    raised: BaseException | None = None
    try:
        await tool.function(**arguments)
    except Exception as error:
        # Expected: the mock's empty results rarely match what the tool reads
        # back. Only the code sent before that matters here.
        raised = error

    if tool.name in NO_BRIDGE_TOOLS:
        assert not bridge.mock_calls, f"{tool.name} is listed as not using FreeCAD"
        return

    assert bridge.mock_calls, (
        f"{tool.name} never called the bridge with arguments {arguments!r}"
        f" (raised {raised!r}). Add accepted arguments to ARGUMENT_OVERRIDES,"
        " or list the tool in NO_BRIDGE_TOOLS if it never contacts FreeCAD."
    )
    errors = [
        error for code in sent if (error := _compile_error(code, tool.name)) is not None
    ]
    assert not errors, f"{tool.name} sent invalid Python: {errors}"


def test_every_registered_tool_is_checked():
    names = [tool.name for tool in ALL_TOOLS]

    assert len(names) == len(set(names))
    assert len(names) > 150


def test_overrides_and_exclusions_name_real_tools():
    names = {tool.name for tool in ALL_TOOLS}

    assert set(ARGUMENT_OVERRIDES) <= names
    assert names >= NO_BRIDGE_TOOLS


def test_compile_check_catches_broken_code():
    assert _compile_error("if True\n    pass\n", "example") is not None
    assert _compile_error("_result_ = {'ok': True}\n", "example") is None
