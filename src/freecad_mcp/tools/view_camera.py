"""Helpers for the screenshot, camera and zoom tools in ``tools/view.py``.

Screenshots are returned as MCP image content so clients display them, with a
text block holding the capture details as JSON. Camera and zoom changes use
the 3D view's own camera description (see ``bridge/view3d.py``), so they need
no pivy, and always act on the 3D view of the requested document.
"""

import base64
import json
import math
from typing import Any

from mcp.types import ImageContent, TextContent

from freecad_mcp.bridge.base import ScreenshotResult, ViewAngle
from freecad_mcp.bridge.screenshot import execution_error_message, view3d_source

SCREENSHOT_ANGLES: dict[str, ViewAngle] = {angle.value: angle for angle in ViewAngle}
MAX_IMAGE_SIZE = 8192
CAMERA_METHODS = ("getCamera", "setCamera")


def screenshot_angle(view_angle: str) -> ViewAngle:
    """Return the ViewAngle for a screenshot angle name.

    Args:
        view_angle: Angle name, e.g. "Isometric" or "Current".

    Returns:
        The matching ViewAngle.

    Raises:
        ValueError: The name is not a known angle.
    """
    if view_angle not in SCREENSHOT_ANGLES:
        raise ValueError(
            f"Invalid view_angle: {view_angle}. Options: {list(SCREENSHOT_ANGLES)}"
        )
    return SCREENSHOT_ANGLES[view_angle]


def check_image_size(width: int, height: int) -> None:
    """Reject image sizes FreeCAD cannot render.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.

    Raises:
        ValueError: A size is not between 1 and MAX_IMAGE_SIZE.
    """
    for name, value in (("width", width), ("height", height)):
        if not 1 <= value <= MAX_IMAGE_SIZE:
            raise ValueError(f"{name} must be between 1 and {MAX_IMAGE_SIZE} pixels")


def screenshot_content(result: ScreenshotResult) -> list[ImageContent | TextContent]:
    """Turn a screenshot result into MCP content blocks.

    Args:
        result: A screenshot result from the bridge.

    Returns:
        The image, then a text block with the capture details as JSON
        (format, size, document, view angle, fit, camera, note).

    Raises:
        ValueError: The capture failed; the message says why.
    """
    if not result.success or not result.data:
        raise ValueError(
            f"Screenshot failed: {result.error or 'no image data returned'}"
        )
    image_format = (result.format or "png").lower()
    # Validate here so a corrupt payload is reported, not shown as a broken image.
    try:
        base64.b64decode(result.data, validate=True)
    except ValueError as error:
        raise ValueError("Screenshot failed: image data is not valid base64") from error
    details: dict[str, Any] = {
        "success": True,
        "format": image_format,
        "width": result.width,
        "height": result.height,
        "doc_name": result.doc_name,
        "view_angle": result.view_angle.value if result.view_angle else None,
        "fit": result.fit,
        "camera": result.camera,
    }
    if result.note:
        details["note"] = result.note
    return [
        ImageContent(type="image", data=result.data, mime_type=f"image/{image_format}"),
        TextContent(type="text", text=json.dumps(details)),
    ]


def vector(name: str, value: list[float] | None) -> tuple[float, float, float] | None:
    """Check an [x, y, z] argument and return it as floats.

    Args:
        name: Argument name, for the error message.
        value: The argument value, or None.

    Returns:
        The vector as a tuple, or None when not given.

    Raises:
        ValueError: The value is not three finite numbers.
    """
    if value is None:
        return None
    if len(value) != 3:
        raise ValueError(f"{name} must be [x, y, z]")
    numbers = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in numbers):
        raise ValueError(f"{name} must contain finite numbers")
    return numbers[0], numbers[1], numbers[2]


def positive_number(name: str, value: float) -> float:
    """Check that a number is finite and greater than zero.

    Args:
        name: Argument name, for the error message.
        value: The value to check.

    Returns:
        The value as a float.

    Raises:
        ValueError: The value is not a positive finite number.
    """
    number = float(value)
    if not (math.isfinite(number) and number > 0):
        raise ValueError(f"{name} must be a positive number")
    return number


def build_camera_code(doc_name: str | None, new_camera_expression: str) -> str:
    """Build code that replaces the camera of a document's 3D view.

    Args:
        doc_name: Document name, or None for the active document.
        new_camera_expression: Python expression, evaluated in FreeCAD with
            ``camera`` bound to the current camera description, that gives
            the new description (built from ``bridge/view3d.py`` helpers and
            values formatted with ``!r``).

    Returns:
        Python source to execute in FreeCAD. Its result has ``success``,
        ``doc_name``, ``camera`` (see view3d.camera_info) and, when the 3D
        view is not the document's active view, ``note``.
    """
    return f"""
{view3d_source()}
doc, view, view_note = resolve_3d_view(FreeCAD, FreeCADGui, {doc_name!r}, {CAMERA_METHODS!r})
camera = view.getCamera()
view.setCamera({new_camera_expression})
FreeCADGui.updateGui()
_result_ = {{"success": True, "doc_name": doc.Name, "camera": camera_info(view.getCamera())}}
if view_note:
    _result_["note"] = view_note
"""


async def run_camera_code(bridge: Any, code: str, action: str) -> dict[str, Any]:
    """Run camera code and return its result, raising on failure.

    Args:
        bridge: The FreeCAD bridge.
        code: Code from build_camera_code.
        action: What was attempted, for the error message.

    Returns:
        The result dictionary from FreeCAD.

    Raises:
        ValueError: FreeCAD reported an error.
    """
    result = await bridge.execute_python(code)
    if result.success and isinstance(result.result, dict) and result.result:
        return result.result
    message = execution_error_message(result, "no result returned")
    raise ValueError(f"{action} failed: {message}")
