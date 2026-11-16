"""Unit tests for the binder, projection and attached datum tools.

The server side is checked against a mock bridge: which runtime operation it
calls, with which arguments and undo-step name, and what it refuses before
contacting FreeCAD. The FreeCAD-free helpers of the runtime module are tested
directly.
"""

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools.custom.references import register_references_tools

RUNTIME_FILE = (
    Path(__file__).resolve().parents[3]
    / "freecad/RobustMCPBridge/freecad_mcp_bridge/custom_runtime/references.py"
)


def _load_runtime() -> Any:
    """Import the workbench's references module without FreeCAD."""
    spec = importlib.util.spec_from_file_location("_test_references", RUNTIME_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Calls:
    """Mock bridge that runs the sent program with a stand-in dispatcher."""

    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.codes: list[str] = []
        self.bridge = AsyncMock()
        self.bridge.execute_python = AsyncMock(side_effect=self._execute)

    async def _execute(self, code: str, **_kwargs: Any) -> ExecutionResult:
        self.codes.append(code)

        def dispatch(_version: int, operation: str, args: dict[str, Any]) -> Any:
            self.calls.append((operation, args))
            return self.result

        document = MagicMock()
        freecad = MagicMock(ActiveDocument=document)
        freecad.getDocument.return_value = document
        namespace: dict[str, Any] = {"_p3d_dispatch": dispatch, "FreeCAD": freecad}
        exec(compile(code, "<test>", "exec"), namespace)  # noqa: S102
        return ExecutionResult(
            success=True,
            result=namespace.get("_result_"),
            stdout="",
            stderr="",
            execution_time_ms=1.0,
        )

    async def get_bridge(self) -> Any:
        return self.bridge


@pytest.fixture
def setup() -> tuple[dict[str, Callable[..., Any]], _Calls]:
    """Register the tools against a recording mock bridge."""
    tools: dict[str, Callable[..., Any]] = {}
    server = MagicMock()

    def tool(**_kwargs: Any) -> Callable[[Any], Any]:
        def register(function: Any) -> Any:
            tools[function.__name__] = function
            return function

        return register

    server.tool = tool
    calls = _Calls(result={"name": "X"})
    register_references_tools(server, calls.get_bridge)
    return tools, calls


class TestServerTools:
    """What each tool sends, and what it refuses before contacting FreeCAD."""

    async def test_binder_arguments_and_undo_name(self, setup):
        tools, calls = setup

        result = await tools["create_subshape_binder"](
            "CylBody",
            ["BoxPad.Face6", "Other#Pad.Edge1"],
            bind_mode="frozen",
            doc_name="Doc",
        )

        assert result == {"name": "X"}
        operation, args = calls.calls[0]
        assert operation == "create_subshape_binder"
        assert args == {
            "body": "CylBody",
            "sources": ["BoxPad.Face6", "Other#Pad.Edge1"],
            "bind_mode": "frozen",
            "relative": True,
            "make_face": True,
            "doc_name": "Doc",
        }
        assert "Create binder in CylBody" in calls.codes[0]

    async def test_projection_arguments_and_undo_name(self, setup):
        tools, calls = setup

        await tools["project_into_sketch"](
            "Sketch", "Pad", ["Edge3", "Face2"], defining=True
        )

        operation, args = calls.calls[0]
        assert operation == "project_into_sketch"
        assert args == {
            "sketch": "Sketch",
            "source": "Pad",
            "elements": ["Edge3", "Face2"],
            "defining": True,
            "intersection": False,
            "reuse_binder": True,
            "max_geometry": 50,
        }
        assert "Project Pad into Sketch" in calls.codes[0]

    async def test_datum_arguments_and_undo_name(self, setup):
        tools, calls = setup

        await tools["create_attached_datum"](
            "Body",
            ["Pad.Edge1"],
            mode="NormalToEdge",
            kind="line",
            offset=[0, 0, 1],
            rotation=[90, 0, 0],
            path_parameter=1.0,
        )

        operation, args = calls.calls[0]
        assert operation == "create_attached_datum"
        assert args == {
            "body": "Body",
            "attach_to": ["Pad.Edge1"],
            "mode": "NormalToEdge",
            "kind": "line",
            "offset": [0, 0, 1],
            "rotation": [90, 0, 0],
            "path_parameter": 1.0,
            "reverse": False,
        }
        assert "Create datum line in Body" in calls.codes[0]

    @pytest.mark.parametrize(
        "tool, kwargs, message",
        [
            ("create_subshape_binder", {"body_name": "B", "sources": []}, "source"),
            (
                "project_into_sketch",
                {"sketch_name": "S", "source_object": "P", "elements": []},
                "element",
            ),
            (
                "project_into_sketch",
                {
                    "sketch_name": "S",
                    "source_object": "P",
                    "elements": ["Edge1"],
                    "max_geometry": 0,
                },
                "max_geometry",
            ),
            ("create_attached_datum", {"body_name": "B", "attach_to": []}, "reference"),
            (
                "create_attached_datum",
                {"body_name": "B", "attach_to": ["XY_Plane"], "offset": [1, 2]},
                "offset",
            ),
            (
                "create_attached_datum",
                {"body_name": "B", "attach_to": ["XY_Plane"], "rotation": [1]},
                "rotation",
            ),
        ],
    )
    async def test_refused_before_contacting_freecad(
        self, setup, tool, kwargs, message
    ):
        tools, calls = setup

        with pytest.raises(ValueError, match=message):
            await tools[tool](**kwargs)
        calls.bridge.execute_python.assert_not_called()

    async def test_freecad_error_is_passed_through(self, setup):
        tools, calls = setup
        calls.bridge.execute_python = AsyncMock(
            return_value=ExecutionResult(
                success=False,
                result=None,
                stdout="",
                stderr="",
                execution_time_ms=1.0,
                error_traceback="Traceback...\nValueError: Mode 'X' does not fit",
            )
        )

        with pytest.raises(ValueError, match=r"^Mode 'X' does not fit$"):
            await tools["create_attached_datum"]("Body", ["Pad.Face1"], mode="X")


class TestRuntimeHelpers:
    """FreeCAD-free parts of the workbench module."""

    def test_imports_without_freecad(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "FreeCAD", None)
        module = _load_runtime()

        assert set(module.OPERATIONS) == {
            "create_subshape_binder",
            "project_into_sketch",
            "create_attached_datum",
        }

    @pytest.mark.parametrize("element", ["Face1", "Edge12", "Vertex3"])
    def test_element_names_accepted(self, element):
        _load_runtime()._check_element(element)

    @pytest.mark.parametrize("element", ["Face0", "edge1", "Edge", "Solid1", "E1"])
    def test_element_names_refused(self, element):
        with pytest.raises(ValueError, match="Bad sub-element"):
            _load_runtime()._check_element(element)

    def test_triple(self):
        module = _load_runtime()

        assert module._triple(None, "offset") == [0.0, 0.0, 0.0]
        assert module._triple([1, 2, 3], "offset") == [1.0, 2.0, 3.0]
        with pytest.raises(ValueError, match="offset must be"):
            module._triple([1, 2], "offset")

    def test_rounding_clears_negative_zero(self):
        module = _load_runtime()

        assert module._num(-1e-9) == 0.0
        assert str(module._num(-1e-9)) == "0.0"
        assert module._num(0.8660254037) == 0.866025
