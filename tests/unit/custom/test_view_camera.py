"""Tests for screenshots as images and the pivy-free camera and zoom tools.

Covers:
- get_screenshot returns MCP image content plus JSON details, and failures
  reach the client as tool errors (checked with a real MCPServer round trip
  through the normal registration path);
- capturing the current view without changing it ("Current" / fit);
- capturing the document's 3D view when another of its views is active;
- set_camera_position and zoom acting on the named document's camera
  description, without pivy;
- set_view_angle and fit_all targeting the named document.
"""

import base64
import json
import math
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

from freecad_mcp.bridge import view3d
from freecad_mcp.bridge.base import ExecutionResult, ScreenshotResult, ViewAngle
from freecad_mcp.bridge.embedded import EmbeddedBridge
from freecad_mcp.bridge.screenshot import (
    build_screenshot_code,
    build_set_view_code,
    execution_error_message,
    screenshot_result,
)
from freecad_mcp.bridge.socket import SocketBridge
from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools import register_all_tools
from freecad_mcp.tools.view import register_view_tools
from freecad_mcp.tools.view_camera import screenshot_content

PNG = b"\x89PNG\r\n\x1a\nimage"
PNG_BASE64 = base64.b64encode(PNG).decode("ascii")

ISOMETRIC_CAMERA = """#Inventor V2.1 ascii


OrthographicCamera {
  viewportMapping ADJUST_CAMERA
  position 32.247452 -7.247447 17.247448
  orientation 0.74290609 0.30772209 0.59447283  1.2171158
  nearDistance 0
  farDistance 42.426407
  aspectRatio 1
  focalDistance 21.213203
  height 42.426407

}
"""

PERSPECTIVE_CAMERA = """#Inventor V2.1 ascii


PerspectiveCamera {
  viewportMapping ADJUST_CAMERA
  position 0 -100 0
  orientation 1 0 0  1.5707964
  nearDistance 1
  farDistance 200
  aspectRatio 1
  focalDistance 100
  heightAngle 0.78539819

}
"""


def _execution(result: Any = None, *, success: bool = True, error: str = "") -> Any:
    """Return an ExecutionResult."""
    return ExecutionResult(
        success=success,
        result=result,
        stdout="",
        stderr="",
        execution_time_ms=1.0,
        error_traceback=error or None,
    )


# ---------------------------------------------------------------------------
# Camera description helpers (run inside FreeCAD, tested as plain Python)
# ---------------------------------------------------------------------------


class TestCameraText:
    """Reading and changing Inventor camera descriptions."""

    def test_camera_info_isometric(self):
        info = view3d.camera_info(ISOMETRIC_CAMERA)

        assert info["type"] == "Orthographic"
        third = 1 / math.sqrt(3)
        assert info["view_direction"] == pytest.approx([-third, third, -third], 1e-5)
        assert info["up_direction"] == pytest.approx(
            [-0.408248, 0.408248, 0.816497], 1e-5
        )
        assert info["look_at"] == pytest.approx([20, 5, 5], abs=1e-4)
        assert info["view_height"] == pytest.approx(42.426407)

    def test_camera_info_perspective_reports_field_of_view(self):
        info = view3d.camera_info(PERSPECTIVE_CAMERA)

        assert info["type"] == "Perspective"
        assert info["view_direction"] == pytest.approx([0, 1, 0], abs=1e-6)
        assert info["up_direction"] == pytest.approx([0, 0, 1], abs=1e-6)
        assert info["field_of_view_degrees"] == pytest.approx(45)
        assert "view_height" not in info

    def test_orientation_round_trip(self):
        info = view3d.camera_info(ISOMETRIC_CAMERA)

        orientation = view3d.look_at_orientation(
            info["view_direction"], info["up_direction"]
        )

        assert orientation == pytest.approx(
            (0.74290609, 0.30772209, 0.59447283, 1.2171158), abs=1e-5
        )

    @pytest.mark.parametrize(
        "direction",
        [(1, 0, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1), (-1, 2, 3), (0.2, -0.1, -5)],
    )
    def test_look_at_points_the_camera(self, direction):
        position = (10.0, 20.0, 30.0)
        target = tuple(p + d for p, d in zip(position, direction, strict=True))

        text = view3d.look_at_camera_text(ISOMETRIC_CAMERA, position, target)
        info = view3d.camera_info(text)

        length = math.sqrt(sum(d * d for d in direction))
        assert info["position"] == pytest.approx(position)
        assert info["view_direction"] == pytest.approx(
            [d / length for d in direction], abs=1e-6
        )
        assert info["look_at"] == pytest.approx(target, abs=1e-5)
        assert info["focal_distance"] == pytest.approx(length)
        # The up direction is perpendicular to the view and points up where possible.
        dot = sum(
            a * b
            for a, b in zip(info["up_direction"], info["view_direction"], strict=True)
        )
        assert dot == pytest.approx(0, abs=1e-6)

    def test_look_at_uses_up_vector_and_view_height(self):
        text = view3d.look_at_camera_text(
            ISOMETRIC_CAMERA, (0, 0, 50), (0, 0, 0), up=(1, 0, 0), view_height=12.5
        )
        info = view3d.camera_info(text)

        assert info["up_direction"] == pytest.approx([1, 0, 0], abs=1e-6)
        assert info["view_height"] == pytest.approx(12.5)
        # Other fields are kept.
        assert "aspectRatio 1" in text
        assert text.count("height ") == 1

    def test_view_height_is_ignored_for_perspective(self):
        text = view3d.look_at_camera_text(
            PERSPECTIVE_CAMERA, (0, -10, 0), (0, 0, 0), view_height=5
        )

        assert "heightAngle 0.78539819" in text
        assert not re.search(r"(?m)^\s*height\s", text)

    @pytest.mark.parametrize(
        ("position", "target", "up", "message"),
        [
            ((1, 1, 1), (1, 1, 1), None, "different points"),
            ((0, 0, 10), (0, 0, 0), (0, 0, 5), "parallel"),
            ((0, 0, 10), (0, 0, 0), (0, 0, 0), "zero length"),
        ],
    )
    def test_look_at_rejects_unusable_vectors(self, position, target, up, message):
        with pytest.raises(ValueError, match=message):
            view3d.look_at_camera_text(ISOMETRIC_CAMERA, position, target, up)

    def test_zoom_orthographic_changes_height(self):
        zoomed = view3d.zoom_camera_text(ISOMETRIC_CAMERA, 2)

        info = view3d.camera_info(zoomed)
        assert info["view_height"] == pytest.approx(42.426407 / 2)
        assert info["position"] == pytest.approx([32.247452, -7.247447, 17.247448])

    def test_zoom_perspective_moves_towards_focal_point(self):
        zoomed = view3d.zoom_camera_text(PERSPECTIVE_CAMERA, 4)

        info = view3d.camera_info(zoomed)
        assert info["focal_distance"] == pytest.approx(25)
        assert info["position"] == pytest.approx([0, -25, 0], abs=1e-5)
        assert info["look_at"] == pytest.approx([0, 0, 0], abs=1e-5)

    @pytest.mark.parametrize("factor", [0, -1, math.inf, math.nan])
    def test_zoom_rejects_bad_factor(self, factor):
        with pytest.raises(ValueError, match="positive"):
            view3d.zoom_camera_text(ISOMETRIC_CAMERA, factor)

    def test_missing_field_is_added(self):
        text = "#Inventor V2.1 ascii\nOrthographicCamera {\n  height 10\n}\n"

        updated = view3d.update_camera_text(text, {"position": (1.0, 2.0, 3.0)})

        assert "position 1 2 3" in updated
        assert view3d.camera_info(updated)["position"] == [1, 2, 3]
        assert view3d.camera_info(text)["position"] == [0, 0, 1]  # Inventor default

    def test_unreadable_camera_is_rejected(self):
        with pytest.raises(ValueError, match="could not be read"):
            view3d.update_camera_text("not a camera", {"height": (1.0,)})


# ---------------------------------------------------------------------------
# Finding the document's 3D view
# ---------------------------------------------------------------------------


class Sheet:
    """A spreadsheet view: no camera or image methods."""

    def fitAll(self) -> None:
        """Spreadsheet views can fit their columns."""


def _gui_document(active: Any, views: list[Any], name: str = "Target") -> MagicMock:
    """Return a GUI document whose active view and 3D views are given."""
    gui_doc = MagicMock()
    gui_doc.Document.Name = name
    gui_doc.activeView.return_value = active
    gui_doc.mdiViewsOfType.return_value = views
    return gui_doc


class TestFind3dView:
    """find_3d_view and resolve_3d_view."""

    def test_active_3d_view_is_used(self):
        view = MagicMock()
        found, note = view3d.find_3d_view(_gui_document(view, []), ("getCamera",))

        assert found is view
        assert note is None

    def test_3d_view_found_when_spreadsheet_is_active(self):
        view = MagicMock()
        gui_doc = _gui_document(Sheet(), [view])

        found, note = view3d.find_3d_view(gui_doc, ("saveImage", "getCamera"))

        assert found is view
        assert "Sheet" in note
        gui_doc.mdiViewsOfType.assert_called_once_with("Gui::View3DInventor")

    def test_no_3d_view_raises(self):
        with pytest.raises(ValueError, match=r"no supported 3D view.*Sheet"):
            view3d.find_3d_view(_gui_document(Sheet(), []), ("getCamera",))

    def test_old_gui_document_without_view_list(self):
        gui_doc = SimpleNamespace(activeView=Sheet, Document=None)

        with pytest.raises(ValueError, match="no supported 3D view"):
            view3d.find_3d_view(gui_doc, ("getCamera",))

    def test_unknown_document_raises(self):
        def get_document(name: str) -> Any:
            raise NameError(f"Unknown document '{name}'")

        freecad = SimpleNamespace(GuiUp=True, getDocument=get_document)

        with pytest.raises(ValueError, match="No document named 'Missing'"):
            view3d.resolve_3d_view(freecad, MagicMock(), "Missing", ())

    def test_headless_raises(self):
        freecad = SimpleNamespace(GuiUp=False, ActiveDocument=SimpleNamespace())

        with pytest.raises(RuntimeError, match="GUI not available"):
            view3d.resolve_3d_view(freecad, MagicMock(), None, ())

    def test_named_document_is_used_not_active_one(self):
        target = SimpleNamespace(Name="Target")
        freecad = SimpleNamespace(
            GuiUp=True,
            ActiveDocument=SimpleNamespace(Name="Other"),
            getDocument=MagicMock(return_value=target),
        )
        gui = MagicMock()
        view = MagicMock()
        gui.getDocument.return_value = _gui_document(view, [])

        doc, found, _ = view3d.resolve_3d_view(freecad, gui, "Target", ())

        assert doc is target
        assert found is view
        gui.getDocument.assert_called_once_with("Target")


# ---------------------------------------------------------------------------
# Generated screenshot and set-view code, run against fake FreeCAD modules
# ---------------------------------------------------------------------------


class FakeView:
    """A 3D view with a camera description and image export."""

    def __init__(self, camera: str = ISOMETRIC_CAMERA) -> None:
        self.camera = camera
        self.calls: list[str] = []

    def getCamera(self) -> str:
        return self.camera

    def setCamera(self, camera: str) -> None:
        self.calls.append("setCamera")
        self.camera = camera

    def fitAll(self) -> None:
        self.calls.append("fitAll")

    def viewFront(self) -> None:
        self.calls.append("viewFront")

    def viewIsometric(self) -> None:
        self.calls.append("viewIsometric")

    def saveImage(self, path: str, *_args: Any) -> None:
        self.calls.append("saveImage")
        with open(path, "wb") as image_file:
            image_file.write(PNG)


def _freecad_environment(
    view: Any, active: Any | None = None
) -> tuple[dict[str, Any], MagicMock]:
    """Return exec globals with fake FreeCAD modules around a view."""
    document = SimpleNamespace(Name="Target")
    freecad = SimpleNamespace(
        GuiUp=True,
        ActiveDocument=SimpleNamespace(Name="Other"),
        getDocument=MagicMock(return_value=document),
    )
    gui = MagicMock()
    gui.getDocument.return_value = _gui_document(
        view if active is None else active, [view]
    )
    return {"FreeCAD": freecad, "FreeCADGui": gui}, gui


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    """Keep generated temporary files in the test's directory."""
    import os

    original = os.path.expanduser
    monkeypatch.setattr(
        os.path,
        "expanduser",
        lambda path: str(tmp_path) if path == "~" else original(path),
    )
    return tmp_path


class TestScreenshotCode:
    """build_screenshot_code inside (fake) FreeCAD."""

    def test_spreadsheet_active_captures_3d_view(self, private_home):
        view = FakeView()
        environment, gui = _freecad_environment(view, active=Sheet())

        exec(build_screenshot_code("Target", "Front", 64, 48), environment)  # noqa: S102

        result = environment["_result_"]
        assert base64.b64decode(result["data"]) == PNG
        assert result["doc_name"] == "Target"
        assert "Sheet" in result["note"]
        assert result["camera"]["type"] == "Orthographic"
        assert view.calls == ["viewFront", "fitAll", "saveImage", "setCamera"]
        gui.getDocument.assert_called_once_with("Target")
        assert not list(private_home.iterdir())

    def test_current_view_without_fit_leaves_camera_alone(self, private_home):
        view = FakeView()
        environment, _ = _freecad_environment(view)

        exec(  # noqa: S102
            build_screenshot_code("Target", "Current", 64, 48, fit=False), environment
        )

        assert "fitAll" not in view.calls
        assert "viewIsometric" not in view.calls
        assert view.camera == ISOMETRIC_CAMERA
        assert environment["_result_"]["camera"]["position"] == pytest.approx(
            [32.247452, -7.247447, 17.247448]
        )

    def test_fit_without_angle_change(self, private_home):
        view = FakeView()
        environment, _ = _freecad_environment(view)

        exec(  # noqa: S102
            build_screenshot_code("Target", "Current", 64, 48, fit=True), environment
        )

        assert view.calls == ["fitAll", "saveImage", "setCamera"]

    def test_no_3d_view_raises(self, private_home):
        environment, _ = _freecad_environment(FakeView(), active=Sheet())
        environment[
            "FreeCADGui"
        ].getDocument.return_value.mdiViewsOfType.return_value = []

        with pytest.raises(ValueError, match="no supported 3D view"):
            exec(build_screenshot_code("Target", "Front", 64, 48), environment)  # noqa: S102

    def test_default_code_is_unchanged_by_fit_default(self):
        assert build_screenshot_code(None, "Isometric", 8, 8) == build_screenshot_code(
            None, "Isometric", 8, 8, True
        )


class TestSetViewCode:
    """build_set_view_code inside (fake) FreeCAD."""

    def test_targets_named_document_and_disables_animation(self):
        view = FakeView()
        view.isAnimationEnabled = MagicMock(return_value=True)
        view.setAnimationEnabled = MagicMock()
        environment, gui = _freecad_environment(view, active=Sheet())

        exec(build_set_view_code("Target", "Front"), environment)  # noqa: S102

        assert view.calls == ["viewFront"]
        gui.getDocument.assert_called_once_with("Target")
        assert [c.args[0] for c in view.setAnimationEnabled.call_args_list] == [
            False,
            True,
        ]

    def test_fit_all_needs_a_3d_view(self):
        environment, _ = _freecad_environment(FakeView(), active=Sheet())
        environment[
            "FreeCADGui"
        ].getDocument.return_value.mdiViewsOfType.return_value = []

        with pytest.raises(ValueError, match="no supported 3D view"):
            exec(build_set_view_code("Target", "FitAll"), environment)  # noqa: S102

    def test_headless_does_nothing(self):
        environment = {"FreeCAD": SimpleNamespace(GuiUp=False), "FreeCADGui": None}

        exec(build_set_view_code("Target", "Top"), environment)  # noqa: S102

        assert environment["_result_"] is True


@pytest.mark.parametrize("bridge_class", [XmlRpcBridge, SocketBridge, EmbeddedBridge])
class TestBridges:
    """The three bridges share the screenshot and set-view code."""

    @pytest.mark.asyncio
    async def test_screenshot_passes_fit_and_details(self, bridge_class):
        bridge = bridge_class()
        bridge.is_gui_available = AsyncMock(return_value=True)
        details = {
            "data": PNG_BASE64,
            "format": "png",
            "width": 64,
            "height": 48,
            "doc_name": "Target",
            "camera": {"type": "Orthographic"},
            "note": "used 3D view",
        }
        bridge.execute_python = AsyncMock(return_value=_execution(details))

        result = await bridge.get_screenshot(
            view_angle=ViewAngle.CURRENT, width=64, height=48, doc_name="T", fit=False
        )

        assert bridge.execute_python.await_args.args[0] == build_screenshot_code(
            "T", "Current", 64, 48, False
        )
        assert result.success and result.fit is False
        assert result.camera == {"type": "Orthographic"}
        assert result.note == "used 3D view"

    @pytest.mark.asyncio
    async def test_set_view_failure_raises(self, bridge_class):
        bridge = bridge_class()
        bridge.is_gui_available = AsyncMock(return_value=True)
        traceback = (
            'Traceback (most recent call last):\n  File "<mcp>"\nValueError: No 3D'
        )
        bridge.execute_python = AsyncMock(
            return_value=_execution(success=False, error=traceback)
        )

        with pytest.raises(ValueError, match=r"^ValueError: No 3D$"):
            await bridge.set_view(ViewAngle.TOP, "Target")

        assert bridge.execute_python.await_args.args[0] == build_set_view_code(
            "Target", "Top"
        )


def test_failed_screenshot_result_keeps_last_traceback_line():
    traceback = "Traceback (most recent call last):\n  ...\nValueError: No document\n"

    result = screenshot_result(
        _execution(success=False, error=traceback), ViewAngle.TOP, 10, 20
    )

    assert not result.success
    assert result.error == "ValueError: No document"
    assert execution_error_message(_execution(success=False), "default") == "default"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@pytest.fixture
def view_tools():
    """Register the view tools against a mock bridge."""
    tools: dict[str, Any] = {}
    server = MagicMock()

    def tool(**_kwargs: Any):
        def register(function: Any) -> Any:
            tools[function.__name__] = function
            return function

        return register

    server.tool = tool
    bridge = AsyncMock()

    async def get_bridge() -> Any:
        return bridge

    register_view_tools(server, get_bridge)
    return tools, bridge


def _screenshot(**overrides: Any) -> ScreenshotResult:
    """Return a successful screenshot result."""
    values: dict[str, Any] = {
        "success": True,
        "data": PNG_BASE64,
        "format": "png",
        "width": 64,
        "height": 48,
        "view_angle": ViewAngle.ISOMETRIC,
        "doc_name": "Target",
        "camera": {"type": "Orthographic", "view_height": 10.0},
    }
    values.update(overrides)
    return ScreenshotResult(**values)


class TestScreenshotTool:
    """get_screenshot at tool level."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("angle", "fit", "expected"),
        [
            ("Isometric", None, True),
            ("FitAll", None, True),
            ("Current", None, False),
            ("Current", True, True),
            ("Top", False, False),
        ],
    )
    async def test_fit_default_depends_on_angle(self, view_tools, angle, fit, expected):
        tools, bridge = view_tools
        bridge.get_screenshot = AsyncMock(return_value=_screenshot())

        await tools["get_screenshot"](view_angle=angle, fit=fit, doc_name="Target")

        kwargs = bridge.get_screenshot.await_args.kwargs
        assert kwargs["fit"] is expected
        assert kwargs["view_angle"] is ViewAngle(angle)
        assert kwargs["doc_name"] == "Target"

    @pytest.mark.asyncio
    async def test_returns_image_and_details(self, view_tools):
        tools, bridge = view_tools
        bridge.get_screenshot = AsyncMock(return_value=_screenshot(note="note text"))

        image, text = await tools["get_screenshot"](doc_name="Target")

        assert isinstance(image, ImageContent)
        assert image.mime_type == "image/png"
        assert base64.b64decode(image.data) == PNG
        details = json.loads(text.text)
        assert details == {
            "success": True,
            "format": "png",
            "width": 64,
            "height": 48,
            "doc_name": "Target",
            "view_angle": "Isometric",
            "fit": True,
            "camera": {"type": "Orthographic", "view_height": 10.0},
            "note": "note text",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"view_angle": "Sideways"}, "Invalid view_angle"),
            ({"width": 0}, "width"),
            ({"height": 100000}, "height"),
        ],
    )
    async def test_invalid_arguments_raise(self, view_tools, kwargs, message):
        tools, bridge = view_tools

        with pytest.raises(ValueError, match=message):
            await tools["get_screenshot"](**kwargs)

        bridge.get_screenshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_capture_raises(self, view_tools):
        tools, bridge = view_tools
        bridge.get_screenshot = AsyncMock(
            return_value=ScreenshotResult(success=False, error="ValueError: no view")
        )

        with pytest.raises(ValueError, match="Screenshot failed: ValueError: no view"):
            await tools["get_screenshot"]()

    def test_corrupt_image_data_raises(self):
        with pytest.raises(ValueError, match="not valid base64"):
            screenshot_content(_screenshot(data="not base64!"))


class TestCameraTools:
    """set_camera_position, zoom_in and zoom_out send pivy-free code."""

    @staticmethod
    def _run_sent_code(bridge: AsyncMock, view: FakeView) -> dict[str, Any]:
        """Execute the code the tool sent against a fake FreeCAD."""
        code = bridge.execute_python.await_args.args[0]
        assert "import pivy" not in code and "getCameraNode" not in code
        environment, gui = _freecad_environment(view)
        exec(code, environment)  # noqa: S102
        gui.getDocument.assert_called_once_with("Target")
        return environment["_result_"]

    @pytest.mark.asyncio
    async def test_set_camera_position(self, view_tools):
        tools, bridge = view_tools
        bridge.execute_python = AsyncMock(return_value=_execution({"success": True}))

        result = await tools["set_camera_position"](
            position=[0, -50, 5], look_at=[0, 0, 5], view_height=8, doc_name="Target"
        )

        assert result == {"success": True}
        view = FakeView()
        sent = self._run_sent_code(bridge, view)
        assert sent["doc_name"] == "Target"
        assert sent["camera"]["position"] == pytest.approx([0, -50, 5])
        assert sent["camera"]["view_direction"] == pytest.approx([0, 1, 0], abs=1e-6)
        assert sent["camera"]["up_direction"] == pytest.approx([0, 0, 1], abs=1e-6)
        assert sent["camera"]["view_height"] == pytest.approx(8)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "factor", "height"),
        [("zoom_in", 2.0, 42.426407 / 2), ("zoom_out", 2.0, 42.426407 * 2)],
    )
    async def test_zoom(self, view_tools, tool, factor, height):
        tools, bridge = view_tools
        bridge.execute_python = AsyncMock(return_value=_execution({"success": True}))

        await tools[tool](factor=factor, doc_name="Target")

        sent = self._run_sent_code(bridge, FakeView())
        assert sent["camera"]["view_height"] == pytest.approx(height)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "kwargs", "message"),
        [
            ("zoom_in", {"factor": 0}, "positive"),
            ("zoom_out", {"factor": -2}, "positive"),
            ("set_camera_position", {"position": [1, 2]}, r"\[x, y, z\]"),
            ("set_camera_position", {"position": [0, 0, 0]}, "different points"),
            (
                "set_camera_position",
                {"position": [1, 0, 0], "view_height": 0},
                "view_height",
            ),
        ],
    )
    async def test_invalid_arguments_raise(self, view_tools, tool, kwargs, message):
        tools, bridge = view_tools

        with pytest.raises(ValueError, match=message):
            await tools[tool](**kwargs)

        bridge.execute_python.assert_not_called()

    @pytest.mark.asyncio
    async def test_freecad_error_raises(self, view_tools):
        tools, bridge = view_tools
        bridge.execute_python = AsyncMock(
            return_value=_execution(
                success=False,
                error="Traceback (most recent call last):\nValueError: no 3D view",
            )
        )

        with pytest.raises(ValueError, match="Zoom in failed: ValueError: no 3D view"):
            await tools["zoom_in"](doc_name="Target")


# ---------------------------------------------------------------------------
# What an MCP client receives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_receives_image_content_block():
    """Through the real registration path, the client gets an image block."""
    bridge = AsyncMock()
    bridge.get_screenshot = AsyncMock(return_value=_screenshot())

    async def get_bridge() -> Any:
        return bridge

    server = MCPServer("test")
    register_all_tools(server, get_bridge)

    async with Client(server) as client:
        listing = await client.list_tools()
        tool = next(item for item in listing.tools if item.name == "get_screenshot")
        assert tool.output_schema is None
        assert "fit" in tool.input_schema["properties"]

        result = await client.call_tool(
            "get_screenshot", {"doc_name": "Target", "view_angle": "Current"}
        )
        assert not result.is_error
        assert result.structured_content is None
        image, text = result.content
        assert isinstance(image, ImageContent)
        assert image.mime_type == "image/png"
        assert base64.b64decode(image.data) == PNG
        assert isinstance(text, TextContent)
        assert json.loads(text.text)["camera"]["view_height"] == 10.0

        bridge.get_screenshot = AsyncMock(
            return_value=ScreenshotResult(
                success=False, error="ValueError: Document 'Target' has no 3D view"
            )
        )
        failed = await client.call_tool("get_screenshot", {"doc_name": "Target"})
        assert failed.is_error
        assert "has no 3D view" in failed.content[0].text

        bridge.execute_python = AsyncMock(
            return_value=_execution({"success": True, "camera": {}})
        )
        zoomed = await client.call_tool("zoom_in", {"doc_name": "Target"})
        assert not zoomed.is_error
        assert zoomed.structured_content == {"success": True, "camera": {}}
