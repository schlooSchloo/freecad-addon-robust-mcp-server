"""Screenshot, camera and zoom tools against a live FreeCAD GUI.

Covers the fixes for screenshots returned as images, capturing the current
view, capturing when a spreadsheet view is active, and the camera and zoom
tools working without pivy and on the requested document.
"""

import base64
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools._registration import report_expected_errors
from freecad_mcp.tools.view import register_view_tools
from tests.integration.auth_support import integration_credentials
from tests.integration.conftest import requires_gui

pytestmark = [pytest.mark.integration, pytest.mark.gui, requires_gui]


@pytest_asyncio.fixture
async def view_setup() -> AsyncIterator[tuple[XmlRpcBridge, dict[str, Any], str]]:
    """Connect, register the view tools and create a document with a box."""
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    tools: dict[str, Callable[..., Any]] = {}
    server = MagicMock()

    def tool(**_kwargs: Any) -> Callable[[Any], Any]:
        def register(function: Any) -> Any:
            tools[function.__name__] = report_expected_errors(function)
            return function

        return register

    server.tool = tool

    async def get_bridge() -> XmlRpcBridge:
        return bridge

    register_view_tools(server, get_bridge)
    doc_name = f"McpViewCamera_{uuid4().hex[:12]}"
    setup = await bridge.execute_python(
        f"""
doc = FreeCAD.newDocument({doc_name!r})
box = doc.addObject("Part::Box", "Box")
box.Length = 40
doc.recompute()
view = FreeCADGui.getDocument(doc.Name).activeView()
view.setAnimationEnabled(False)
view.viewIsometric()
view.fitAll()
FreeCADGui.updateGui()
view.setAnimationEnabled(True)
_result_ = True
"""
    )
    assert setup.success, setup.error_traceback or setup.stderr
    try:
        yield bridge, tools, doc_name
    finally:
        try:
            await bridge.execute_python(
                f"FreeCAD.closeDocument({doc_name!r})\n_result_ = True"
            )
        finally:
            await bridge.disconnect()


def _image(blocks: list[Any]) -> tuple[Any, dict[str, Any]]:
    """Return the decoded QImage and the details from screenshot content."""
    from PySide6.QtGui import QImage

    assert isinstance(blocks[0], ImageContent)
    assert blocks[0].mime_type == "image/png"
    assert isinstance(blocks[1], TextContent)
    image = QImage.fromData(base64.b64decode(blocks[0].data, validate=True))
    assert not image.isNull()
    return image, json.loads(blocks[1].text)


def _differing_pixels(first: Any, second: Any) -> int:
    """Count sampled pixels that differ between two images of equal size."""
    assert first.size() == second.size()
    return sum(
        first.pixel(x, y) != second.pixel(x, y)
        for x in range(0, first.width(), 4)
        for y in range(0, first.height(), 4)
    )


async def _camera(bridge: XmlRpcBridge, doc_name: str) -> str:
    """Return the camera description of the document's 3D view."""
    result = await bridge.execute_python(
        f"""
gui_doc = FreeCADGui.getDocument({doc_name!r})
_result_ = gui_doc.mdiViewsOfType("Gui::View3DInventor")[0].getCamera()
"""
    )
    assert result.success, result.error_traceback
    return str(result.result)


@pytest.mark.asyncio
async def test_current_view_capture_differs_from_default(view_setup: Any) -> None:
    """A placed camera is captured as is, and the default capture restores it."""
    bridge, tools, doc_name = view_setup
    default_image, default_details = _image(
        await tools["get_screenshot"](doc_name=doc_name, width=320, height=240)
    )
    assert default_details["fit"] is True
    assert default_details["camera"]["type"] in {"Orthographic", "Perspective"}

    placed = await tools["set_camera_position"](
        position=[40, -60, 5], look_at=[40, 5, 5], view_height=20, doc_name=doc_name
    )
    assert placed["camera"]["view_direction"] == pytest.approx([0, 1, 0], abs=1e-5)
    camera_before = await _camera(bridge, doc_name)

    current_image, current_details = _image(
        await tools["get_screenshot"](
            view_angle="Current", doc_name=doc_name, width=320, height=240
        )
    )
    assert current_details["fit"] is False
    assert current_details["camera"]["position"] == pytest.approx([40, -60, 5])
    assert _differing_pixels(default_image, current_image) > 200

    # A default capture changes the camera only for the capture.
    await tools["get_screenshot"](doc_name=doc_name, width=320, height=240)
    assert await _camera(bridge, doc_name) == camera_before


@pytest.mark.asyncio
async def test_zoom_changes_the_requested_document(view_setup: Any) -> None:
    """zoom_in and zoom_out change the named document's camera and undo each other."""
    _, tools, doc_name = view_setup
    await tools["set_camera_position"](
        position=[20, -60, 5], look_at=[20, 5, 5], view_height=40, doc_name=doc_name
    )
    zoomed = await tools["zoom_in"](factor=2, doc_name=doc_name)
    camera = zoomed["camera"]
    if camera["type"] == "Orthographic":
        assert camera["view_height"] == pytest.approx(20)
    else:
        assert camera["focal_distance"] == pytest.approx(32.5)
    restored = await tools["zoom_out"](factor=2, doc_name=doc_name)
    assert restored["camera"]["focal_distance"] == pytest.approx(65)
    assert restored["camera"].get("view_height", 40) == pytest.approx(40)


@pytest.mark.asyncio
async def test_capture_with_spreadsheet_view_active(view_setup: Any) -> None:
    """The document's 3D view is captured when its spreadsheet view is active."""
    bridge, tools, doc_name = view_setup
    opened = await bridge.execute_python(
        f"""
doc = FreeCAD.getDocument({doc_name!r})
sheet = doc.addObject("Spreadsheet::Sheet", "Sheet")
doc.recompute()
sheet.ViewObject.doubleClicked()
FreeCADGui.updateGui()
_result_ = type(FreeCADGui.getDocument(doc.Name).activeView()).__name__
"""
    )
    assert opened.success, opened.error_traceback
    assert "Sheet" in opened.result

    image, details = _image(
        await tools["get_screenshot"](doc_name=doc_name, width=200, height=150)
    )
    assert "note" in details
    colours = {
        image.pixel(x, y)
        for x in range(0, image.width(), 4)
        for y in range(0, image.height(), 4)
    }
    assert len(colours) > 2, "The capture did not show the model"


@pytest.mark.asyncio
async def test_missing_3d_view_is_a_tool_error(view_setup: Any) -> None:
    """Without any 3D view, capture and zoom fail with a tool error."""
    bridge, tools, _ = view_setup
    doc_name = f"McpNoView_{uuid4().hex[:12]}"
    closed = await bridge.execute_python(
        f"""
import time
from PySide import QtCore, QtWidgets
doc = FreeCAD.newDocument({doc_name!r})
# Keep a spreadsheet view open: closing a document's last view closes it.
sheet = doc.addObject("Spreadsheet::Sheet", "Sheet")
doc.recompute()
sheet.ViewObject.doubleClicked()
FreeCADGui.updateGui()
gui_doc = FreeCADGui.getDocument(doc.Name)
area = FreeCADGui.getMainWindow().findChild(QtWidgets.QMdiArea)
for window in area.subWindowList():
    # Window titles use the label, which may differ from the name.
    if window.windowTitle().startswith(doc.Label + " :"):
        window.close()
# Closed windows are deleted later; let Qt finish that.
for _ in range(20):
    FreeCADGui.updateGui()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    if not gui_doc.mdiViewsOfType("Gui::View3DInventor"):
        break
    time.sleep(0.05)
_result_ = len(gui_doc.mdiViewsOfType("Gui::View3DInventor"))
"""
    )
    try:
        assert closed.success, closed.error_traceback
        if closed.result != 0:
            pytest.skip("Could not close the document's 3D view in this GUI")
        with pytest.raises(ToolError, match="no supported 3D view"):
            await tools["get_screenshot"](doc_name=doc_name)
        with pytest.raises(ToolError, match="no supported 3D view"):
            await tools["zoom_in"](doc_name=doc_name)
    finally:
        await bridge.execute_python(
            f"FreeCAD.closeDocument({doc_name!r})\n_result_ = True"
        )
