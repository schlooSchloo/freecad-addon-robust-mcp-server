"""Generate the shared FreeCAD GUI screenshot and view operations for all bridges."""

import inspect
from typing import TYPE_CHECKING

from freecad_mcp.bridge import private_scratch, view3d

if TYPE_CHECKING:
    from freecad_mcp.bridge.base import ExecutionResult, ScreenshotResult, ViewAngle

# View methods for each standard angle; "FitAll" and "Current" keep the
# orientation as it is.
VIEW_ANGLE_METHODS = {
    "Isometric": "viewIsometric",
    "Front": "viewFront",
    "Back": "viewRear",
    "Top": "viewTop",
    "Bottom": "viewBottom",
    "Left": "viewLeft",
    "Right": "viewRight",
}
CAPTURE_METHODS = ("saveImage", "getCamera", "setCamera", "fitAll")


def view3d_source() -> str:
    """Return the source of the FreeCAD-side view helpers for generated code."""
    return inspect.getsource(view3d)


def execution_error_message(result: "ExecutionResult", default: str) -> str:
    """Return a readable error message for a failed bridge execution.

    A FreeCAD traceback is reduced to its last line (the exception type and
    message), which is what callers need to see.

    Args:
        result: The failed execution result.
        default: Message to use when the result carries no error text.

    Returns:
        The error message.
    """
    text = (result.error_traceback or result.stderr or "").strip()
    if not text:
        return default
    if text.startswith("Traceback"):
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-1].strip()
    return text


def build_screenshot_code(
    doc_name: str | None,
    view_angle: str,
    width: int,
    height: int,
    fit: bool = True,
) -> str:
    """Build a capture that restores the camera and removes its temporary file.

    The document's 3D view is used even when another view of the document
    (a spreadsheet, say) is active. A missing document or 3D view raises an
    error inside FreeCAD, so the bridge reports a failed execution.

    Args:
        doc_name: Document to capture, or None for the active document.
        view_angle: Requested camera orientation. "FitAll" and "Current"
            keep the current orientation.
        width: Image width in pixels.
        height: Image height in pixels.
        fit: Whether to fit all objects in the view before capturing.

    Returns:
        Python source to execute in FreeCAD's GUI thread.
    """
    scratch_code = inspect.getsource(private_scratch)
    return f"""
{scratch_code}
{view3d_source()}
import base64
import os

doc, view, view_note = resolve_3d_view(FreeCAD, FreeCADGui, {doc_name!r}, {CAPTURE_METHODS!r})
angle = {view_angle!r}
method = {VIEW_ANGLE_METHODS!r}.get(angle)
if method is not None and not callable(getattr(view, method, None)):
    raise ValueError("Cannot capture screenshot: the 3D view does not support the requested angle")

# Save before any orientation or framing changes, including on failed captures.
animation_getter = getattr(view, "isAnimationEnabled", None)
animation_setter = getattr(view, "setAnimationEnabled", None)
animation = animation_getter() if callable(animation_getter) and callable(animation_setter) else None
camera = None
captured_camera = None
try:
    if animation is not None:
        animation_setter(False)
    camera = view.getCamera()
    if method is not None:
        getattr(view, method)()
    FreeCADGui.updateGui()
    if {bool(fit)!r}:
        view.fitAll()
        FreeCADGui.updateGui()
    try:
        captured_camera = camera_info(view.getCamera())
    except Exception:
        captured_camera = None
    with private_scratch_directory(".freecad-mcp-screenshot-") as private_dir:
        temp_path = os.path.join(private_dir, "screenshot.png")
        view.saveImage(temp_path, {width}, {height}, "Current")
        with open(temp_path, "rb") as image_file:
            image_data = base64.b64encode(image_file.read()).decode("ascii")
finally:
    try:
        if camera is not None:
            view.setCamera(camera)
            FreeCADGui.updateGui()
    finally:
        if animation is not None:
            animation_setter(animation)

_result_ = {{"success": True, "data": image_data, "format": "png", "width": {width}, "height": {height}, "doc_name": doc.Name, "camera": captured_camera, "note": view_note}}
"""


def screenshot_result(
    result: "ExecutionResult",
    view_angle: "ViewAngle | None",
    width: int,
    height: int,
    fit: bool = True,
) -> "ScreenshotResult":
    """Convert the execution result of build_screenshot_code to a ScreenshotResult.

    Args:
        result: The bridge's execution result.
        view_angle: The requested view angle.
        width: Requested image width in pixels.
        height: Requested image height in pixels.
        fit: Whether the capture fitted all objects.

    Returns:
        The screenshot result, with the error message on failure.
    """
    from freecad_mcp.bridge.base import ScreenshotResult

    if result.success and isinstance(result.result, dict) and result.result:
        data = result.result
        return ScreenshotResult(
            success=True,
            data=data["data"],
            format=data["format"],
            width=data["width"],
            height=data["height"],
            view_angle=view_angle,
            fit=fit,
            doc_name=data.get("doc_name"),
            camera=data.get("camera"),
            note=data.get("note"),
        )
    return ScreenshotResult(
        success=False,
        error=execution_error_message(result, "Failed to capture screenshot"),
        width=width,
        height=height,
        view_angle=view_angle,
        fit=fit,
    )


def build_set_view_code(doc_name: str | None, view_angle: str) -> str:
    """Build code that sets a standard view angle (or fits all) in a document.

    The document's own 3D view is used, not whichever document is active.
    In headless FreeCAD the code does nothing, as before.

    Args:
        doc_name: Document name, or None for the active document.
        view_angle: A ViewAngle value; "FitAll" fits all objects and
            "Current" changes nothing.

    Returns:
        Python source to execute in FreeCAD.
    """
    method = VIEW_ANGLE_METHODS.get(view_angle)
    if view_angle == "FitAll":
        method = "fitAll"
    # getCamera tells a 3D view apart from e.g. a spreadsheet, which has fitAll.
    required = (method, "getCamera") if method else ("getCamera",)
    return f"""
{view3d_source()}
if not FreeCAD.GuiUp:
    _result_ = True  # No view to change in headless mode.
else:
    doc, view, view_note = resolve_3d_view(FreeCAD, FreeCADGui, {doc_name!r}, {required!r})
    method = {method!r}
    if method is not None:
        animation_getter = getattr(view, "isAnimationEnabled", None)
        animation_setter = getattr(view, "setAnimationEnabled", None)
        animation = animation_getter() if callable(animation_getter) and callable(animation_setter) else None
        try:
            if animation is not None:
                animation_setter(False)
            getattr(view, method)()
            FreeCADGui.updateGui()
        finally:
            if animation is not None:
                animation_setter(animation)
    _result_ = True
"""
