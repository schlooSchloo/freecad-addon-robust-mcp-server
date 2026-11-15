"""View and screenshot tools for FreeCAD Robust MCP Server.

This module provides tools for controlling the 3D view and
capturing screenshots. Based on learnings from neka-nat which
has excellent screenshot handling with view type detection.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import ImageContent, TextContent

from freecad_mcp.bridge.base import ViewAngle
from freecad_mcp.tools._registration import file_tool
from freecad_mcp.tools.view_camera import (
    build_camera_code,
    check_image_size,
    positive_number,
    run_camera_code,
    screenshot_angle,
    screenshot_content,
    vector,
)


def register_view_tools(mcp: Any, get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register view-related tools with the Robust MCP Server.

    Args:
        mcp: The MCPServer instance.
        get_bridge: Async function to get the active bridge.
    """

    @mcp.tool()
    async def get_screenshot(
        view_angle: str = "Isometric",
        width: int = 800,
        height: int = 600,
        doc_name: str | None = None,
        fit: bool | None = None,
    ) -> list[ImageContent | TextContent]:
        """Capture a screenshot of a document's 3D view.

        Requires GUI mode. The document's 3D view is captured even when one
        of its other views (a spreadsheet, say) is active. Named angles
        change the camera only for the capture: it is put back afterwards.

        Args:
            view_angle: View angle to set before capture. Options:
                - "Isometric" - 3D isometric view (default)
                - "Front" - Front view (XZ plane)
                - "Back" - Back view
                - "Top" - Top view (XY plane)
                - "Bottom" - Bottom view
                - "Left" - Left view (YZ plane)
                - "Right" - Right view
                - "FitAll" - Keep the current orientation, fit all objects
                - "Current" - Keep the current camera as it is, e.g. after
                  set_camera_position or zoom_in
            width: Image width in pixels (1-8192). Defaults to 800.
            height: Image height in pixels (1-8192). Defaults to 600.
            doc_name: Document to capture. Uses active document if None.
            fit: Whether to fit all objects in the view before capture.
                Defaults to True for every angle except "Current".

        Returns:
            Two content blocks: the PNG image, then a text block with the
            capture details as JSON: success, format, width, height,
            doc_name, view_angle, fit, camera (type, position,
            view_direction, up_direction, look_at, focal_distance, and
            view_height or field_of_view_degrees) and, when the document's
            active view was not a 3D view, note.

        Raises:
            ValueError: Invalid arguments, no such document, no 3D view, no
                GUI, or the capture failed. The client sees the message as
                a tool error.
        """
        angle = screenshot_angle(view_angle)
        check_image_size(width, height)
        fit_view = (angle is not ViewAngle.CURRENT) if fit is None else fit

        bridge = await get_bridge()
        result = await bridge.get_screenshot(
            view_angle=angle,
            width=width,
            height=height,
            doc_name=doc_name,
            fit=fit_view,
        )
        return screenshot_content(result)

    @mcp.tool()
    async def set_view_angle(
        view_angle: str,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Set the 3D view angle.

        Args:
            view_angle: View angle to set. Options:
                - "Isometric" - 3D isometric view
                - "Front" - Front view (XZ plane)
                - "Back" - Back view
                - "Top" - Top view (XY plane)
                - "Bottom" - Bottom view
                - "Left" - Left view (YZ plane)
                - "Right" - Right view
                - "FitAll" - Fit all objects in view
            doc_name: Document to set view for. Uses active document if None.

        Returns:
            Dictionary with result:
                - success: Whether operation was successful
        """
        from freecad_mcp.bridge.base import ViewAngle

        angle_map = {
            "Isometric": ViewAngle.ISOMETRIC,
            "Front": ViewAngle.FRONT,
            "Back": ViewAngle.BACK,
            "Top": ViewAngle.TOP,
            "Bottom": ViewAngle.BOTTOM,
            "Left": ViewAngle.LEFT,
            "Right": ViewAngle.RIGHT,
            "FitAll": ViewAngle.FIT_ALL,
        }

        if view_angle not in angle_map:
            return {
                "success": False,
                "error": f"Invalid view_angle: {view_angle}. Options: {list(angle_map.keys())}",
            }

        bridge = await get_bridge()
        await bridge.set_view(angle_map[view_angle], doc_name)
        return {"success": True}

    @mcp.tool()
    async def list_workbenches() -> list[dict[str, Any]]:
        """List all available FreeCAD workbenches.

        Returns:
            List of dictionaries, each containing:
                - name: Workbench internal name
                - label: Display label
                - is_active: Whether workbench is currently active
        """
        bridge = await get_bridge()
        workbenches = await bridge.get_workbenches()
        return [
            {
                "name": wb.name,
                "label": wb.label,
                "is_active": wb.is_active,
            }
            for wb in workbenches
        ]

    @mcp.tool()
    async def activate_workbench(workbench_name: str) -> dict[str, Any]:
        """Activate a FreeCAD workbench.

        Args:
            workbench_name: Internal name of the workbench to activate.
                Common workbenches:
                - "PartWorkbench" - Part modeling
                - "PartDesignWorkbench" - Parametric part design
                - "SketcherWorkbench" - 2D sketching
                - "DraftWorkbench" - 2D drafting
                - "MeshWorkbench" - Mesh operations
                - "SpreadsheetWorkbench" - Spreadsheet

        Returns:
            Dictionary with result:
                - success: Whether activation was successful
        """
        bridge = await get_bridge()
        await bridge.activate_workbench(workbench_name)
        return {"success": True}

    @mcp.tool()
    async def fit_all(doc_name: str | None = None) -> dict[str, Any]:
        """Fit all objects in the current view.

        Adjusts the camera to show all visible objects in the document.

        Args:
            doc_name: Document to fit view for. Uses active document if None.

        Returns:
            Dictionary with result:
                - success: Whether operation was successful
        """
        from freecad_mcp.bridge.base import ViewAngle

        bridge = await get_bridge()
        await bridge.set_view(ViewAngle.FIT_ALL, doc_name)
        return {"success": True}

    @mcp.tool()
    async def set_object_visibility(
        object_name: str,
        visible: bool,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Set the visibility of a FreeCAD object.

        Args:
            object_name: Name of the object.
            visible: Whether object should be visible.
            doc_name: Document containing the object. Uses active document if None.

        Returns:
            Dictionary with result:
                - success: Whether operation was successful
                - visible: New visibility state
        """
        bridge = await get_bridge()

        code = f"""
if not FreeCAD.GuiUp:
    _result_ = {{"success": False, "error": "GUI not available - visibility cannot be set in headless mode"}}
else:
    doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
    if doc is None:
        _result_ = {{"success": False, "error": "No document found"}}
    else:
        obj = doc.getObject({object_name!r})
        if obj is None:
            _result_ = {{"success": False, "error": "Object not found: " + repr({object_name!r})}}
        elif hasattr(obj, "ViewObject") and obj.ViewObject:
            obj.ViewObject.Visibility = {visible}
            _result_ = {{"success": True, "visible": {visible}}}
        else:
            _result_ = {{"success": False, "error": "Object has no ViewObject"}}
"""
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "success": False,
            "error": result.error_traceback or "Set visibility failed",
        }

    @mcp.tool()
    async def set_display_mode(
        object_name: str,
        mode: str,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Set the display mode of a FreeCAD object.

        Args:
            object_name: Name of the object.
            mode: Display mode. Common options:
                - "Flat Lines" - Solid with edges
                - "Shaded" - Solid without edges
                - "Wireframe" - Wire frame only
                - "Points" - Points only
            doc_name: Document containing the object. Uses active document if None.

        Returns:
            Dictionary with result:
                - success: Whether operation was successful
                - mode: New display mode
        """
        bridge = await get_bridge()

        code = f"""
if not FreeCAD.GuiUp:
    _result_ = {{"success": False, "error": "GUI not available - display mode cannot be set in headless mode"}}
else:
    doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
    if doc is None:
        _result_ = {{"success": False, "error": "No document found"}}
    else:
        obj = doc.getObject({object_name!r})
        if obj is None:
            _result_ = {{"success": False, "error": "Object not found: " + repr({object_name!r})}}
        elif hasattr(obj, "ViewObject") and obj.ViewObject:
            obj.ViewObject.DisplayMode = {mode!r}
            _result_ = {{"success": True, "mode": {mode!r}}}
        else:
            _result_ = {{"success": False, "error": "Object has no ViewObject"}}
"""
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "success": False,
            "error": result.error_traceback or "Set display mode failed",
        }

    @mcp.tool()
    async def set_object_color(
        object_name: str,
        color: list[float],
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Set the color of a FreeCAD object.

        Args:
            object_name: Name of the object.
            color: RGB color as [r, g, b] where each value is 0.0-1.0.
                   Example: [1.0, 0.0, 0.0] for red.
            doc_name: Document containing the object. Uses active document if None.

        Returns:
            Dictionary with result:
                - success: Whether operation was successful
                - color: New color values
        """
        bridge = await get_bridge()

        if len(color) != 3:
            return {
                "success": False,
                "error": "Color must be [r, g, b] with values 0.0-1.0",
            }

        code = f"""
if not FreeCAD.GuiUp:
    _result_ = {{"success": False, "error": "GUI not available - color cannot be set in headless mode"}}
else:
    doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
    if doc is None:
        _result_ = {{"success": False, "error": "No document found"}}
    else:
        obj = doc.getObject({object_name!r})
        if obj is None:
            _result_ = {{"success": False, "error": "Object not found: " + repr({object_name!r})}}
        elif hasattr(obj, "ViewObject") and obj.ViewObject:
            obj.ViewObject.ShapeColor = ({color[0]}, {color[1]}, {color[2]})
            _result_ = {{"success": True, "color": {color}}}
        else:
            _result_ = {{"success": False, "error": "Object has no ViewObject"}}
"""
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "success": False,
            "error": result.error_traceback or "Set color failed",
        }

    @mcp.tool()
    async def zoom_in(
        factor: float = 1.5, doc_name: str | None = None
    ) -> dict[str, Any]:
        """Zoom in the 3D view of a document.

        Requires GUI mode. Orthographic views show an area ``factor`` times
        smaller; perspective views move the camera ``factor`` times closer to
        the point it looks at. Use get_screenshot with view_angle="Current"
        to capture the result.

        Args:
            factor: Zoom factor (>1 zooms in, <1 zooms out). Defaults to 1.5.
            doc_name: Document whose 3D view to zoom. Uses active document if
                None.

        Returns:
            Dictionary with result:
                - success: True
                - doc_name: Document whose view was changed
                - camera: The new camera (type, position, view_direction,
                  up_direction, look_at, focal_distance, and view_height or
                  field_of_view_degrees)
                - note: Present when the document's active view was not a 3D
                  view and its 3D view was used

        Raises:
            ValueError: Invalid factor, no such document or 3D view, or no
                GUI.
        """
        zoom = positive_number("factor", factor)
        bridge = await get_bridge()
        code = build_camera_code(doc_name, f"zoom_camera_text(camera, {zoom!r})")
        return await run_camera_code(bridge, code, "Zoom in")

    @mcp.tool()
    async def zoom_out(
        factor: float = 1.5, doc_name: str | None = None
    ) -> dict[str, Any]:
        """Zoom out the 3D view of a document.

        Requires GUI mode. The opposite of zoom_in with the same factor.

        Args:
            factor: Zoom factor (>1 zooms out, <1 zooms in). Defaults to 1.5.
            doc_name: Document whose 3D view to zoom. Uses active document if
                None.

        Returns:
            Dictionary with result:
                - success: True
                - doc_name: Document whose view was changed
                - camera: The new camera (see zoom_in)
                - note: Present when the document's active view was not a 3D
                  view and its 3D view was used

        Raises:
            ValueError: Invalid factor, no such document or 3D view, or no
                GUI.
        """
        zoom = 1.0 / positive_number("factor", factor)
        bridge = await get_bridge()
        code = build_camera_code(doc_name, f"zoom_camera_text(camera, {zoom!r})")
        return await run_camera_code(bridge, code, "Zoom out")

    @mcp.tool()
    async def set_camera_position(
        position: list[float],
        look_at: list[float] | None = None,
        doc_name: str | None = None,
        up_vector: list[float] | None = None,
        view_height: float | None = None,
    ) -> dict[str, Any]:
        """Place the camera of a document's 3D view and point it at a point.

        Requires GUI mode. To frame a region, look at its centre from a
        point outside it and, for orthographic views, set view_height to a
        little more than the region's size. Capture the result with
        get_screenshot(view_angle="Current").

        Args:
            position: Camera position as [x, y, z] in mm.
            look_at: Point to look at as [x, y, z]. Uses origin if None. It
                becomes the focal point, which zoom_in and zoom_out keep in
                place.
            doc_name: Document whose 3D view to change. Uses active document
                if None.
            up_vector: Direction that should appear upwards in the image, as
                [x, y, z]. Defaults to +Z (or +Y when looking straight up or
                down). Must not be parallel to the viewing direction.
            view_height: Height of the visible area in mm, for orthographic
                views (FreeCAD's default). Ignored by perspective views,
                where the distance to look_at sets the visible size. Keeps
                the current height if None.

        Returns:
            Dictionary with result:
                - success: True
                - doc_name: Document whose view was changed
                - camera: The new camera (type, position, view_direction,
                  up_direction, look_at, focal_distance, and view_height or
                  field_of_view_degrees)
                - note: Present when the document's active view was not a 3D
                  view and its 3D view was used

        Raises:
            ValueError: Invalid vectors (position equal to look_at, up_vector
                parallel to the view), no such document or 3D view, or no
                GUI.
        """
        camera_position = vector("position", position)
        target = vector("look_at", look_at) or (0.0, 0.0, 0.0)
        up = vector("up_vector", up_vector)
        height = (
            None if view_height is None else positive_number("view_height", view_height)
        )
        if camera_position == target:
            raise ValueError("position and look_at must be different points")

        bridge = await get_bridge()
        code = build_camera_code(
            doc_name,
            f"look_at_camera_text(camera, {camera_position!r}, {target!r},"
            f" {up!r}, {height!r})",
        )
        return await run_camera_code(bridge, code, "Set camera position")

    @mcp.tool()
    async def undo(doc_name: str | None = None) -> dict[str, Any]:
        """Undo the last operation.

        Args:
            doc_name: Document to undo in. Uses active document if None.

        Returns:
            Dictionary with result:
                - success: Whether undo was performed
                - can_undo: Whether more undos are available
        """
        bridge = await get_bridge()

        code = f"""
doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
if doc is None:
    _result_ = {{"success": False, "can_undo": False, "error": "No document found"}}
elif doc.UndoCount > 0:
    doc.undo()
    _result_ = {{"success": True, "can_undo": doc.UndoCount > 0}}
else:
    _result_ = {{"success": False, "can_undo": False, "error": "Nothing to undo"}}
"""
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "success": False,
            "can_undo": False,
            "error": result.error_traceback or "Undo failed",
        }

    @mcp.tool()
    async def redo(doc_name: str | None = None) -> dict[str, Any]:
        """Redo the last undone operation.

        Args:
            doc_name: Document to redo in. Uses active document if None.

        Returns:
            Dictionary with result:
                - success: Whether redo was performed
                - can_redo: Whether more redos are available
        """
        bridge = await get_bridge()

        code = f"""
doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
if doc is None:
    _result_ = {{"success": False, "can_redo": False, "error": "No document found"}}
elif doc.RedoCount > 0:
    doc.redo()
    _result_ = {{"success": True, "can_redo": doc.RedoCount > 0}}
else:
    _result_ = {{"success": False, "can_redo": False, "error": "Nothing to redo"}}
"""
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "success": False,
            "can_redo": False,
            "error": result.error_traceback or "Redo failed",
        }

    @mcp.tool()
    async def get_undo_redo_status(doc_name: str | None = None) -> dict[str, Any]:
        """Get the current undo/redo status.

        Args:
            doc_name: Document to check. Uses active document if None.

        Returns:
            Dictionary with status:
                - undo_count: Number of undoable operations
                - redo_count: Number of redoable operations
                - undo_names: List of undo operation names (if available)
        """
        bridge = await get_bridge()

        code = f"""
doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
if doc is None:
    _result_ = {{"error": "No document found", "undo_count": 0, "redo_count": 0, "undo_names": []}}
else:
    _result_ = {{
        "undo_count": doc.UndoCount,
        "redo_count": doc.RedoCount,
        "undo_names": list(doc.UndoNames) if hasattr(doc, "UndoNames") else [],
    }}
"""
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "error": result.error_traceback or "Get undo/redo status failed",
            "undo_count": 0,
            "redo_count": 0,
            "undo_names": [],
        }

    @mcp.tool()
    async def list_parts_library() -> list[dict[str, Any]]:
        """List available parts from the FreeCAD parts library.

        Returns:
            List of parts with:
                - name: Part filename
                - path: Full path to part file
                - category: Part category/folder
        """
        bridge = await get_bridge()

        code = """
import os

parts = []

# Get parts library paths
try:
    # Standard library path
    lib_path = FreeCAD.getResourceDir() + "Mod/Parts_Library"
    if not os.path.exists(lib_path):
        lib_path = os.path.expanduser("~/.FreeCAD/Mod/PartsLibrary")

    if os.path.exists(lib_path):
        for root, dirs, files in os.walk(lib_path):
            category = os.path.relpath(root, lib_path)
            if category == ".":
                category = "Root"

            for f in files:
                if f.endswith((".FCStd", ".step", ".stp", ".iges", ".igs")):
                    parts.append({
                        "name": f,
                        "path": os.path.join(root, f),
                        "category": category,
                    })
except Exception as e:
    pass

_result_ = parts
"""
        result = await bridge.execute_python(code)
        if result.success:
            return result.result
        return []

    @file_tool(mcp)
    async def insert_part_from_library(
        part_path: str,
        name: str | None = None,
        position: list[float] | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Insert a part from the parts library into the document.

        Args:
            part_path: Path to the part file.
            name: Name for the inserted part. Auto-generated if None.
            position: Initial position [x, y, z]. Origin if None.
            doc_name: Target document. Uses active document if None.

        Returns:
            Dictionary with inserted part information:
                - name: Part name
                - label: Part label
                - type_id: Part type
        """
        bridge = await get_bridge()

        pos_str = (
            f"FreeCAD.Vector({position[0]}, {position[1]}, {position[2]})"
            if position
            else "FreeCAD.Vector(0, 0, 0)"
        )

        code = f"""
import os
import Part

doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
if doc is None:
    doc = FreeCAD.newDocument("Unnamed")

part_path = {part_path!r}
if not os.path.exists(part_path):
    raise FileNotFoundError(f"Part file not found: {{part_path}}")

ext = os.path.splitext(part_path)[1].lower()
part_name = {name!r} or os.path.splitext(os.path.basename(part_path))[0]

# Wrap in transaction for undo support
doc.openTransaction("Insert Part from Library")
try:
    new_obj = None
    if ext == ".fcstd":
        # Import FreeCAD document
        src_doc = FreeCAD.openDocument(part_path)
        for obj in src_doc.Objects:
            if hasattr(obj, "Shape"):
                new_obj = doc.addObject("Part::Feature", part_name)
                new_obj.Shape = obj.Shape.copy()
                break
        FreeCAD.closeDocument(src_doc.Name)
    else:
        # Import STEP/IGES
        shape = Part.read(part_path)
        new_obj = doc.addObject("Part::Feature", part_name)
        new_obj.Shape = shape

    if new_obj is None:
        raise ValueError(f"No importable shape found in {{part_path}}")

    # Set position
    new_obj.Placement.Base = {pos_str}

    doc.recompute()
    doc.commitTransaction()

    _result_ = {{
        "name": new_obj.Name,
        "label": new_obj.Label,
        "type_id": new_obj.TypeId,
    }}
except Exception:
    doc.abortTransaction()
    raise
"""
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "success": False,
            "error": result.error_traceback or "Insert part from library failed",
        }

    @mcp.tool()
    async def get_console_log(lines: int = 50) -> dict[str, Any]:
        """Get recent console output from FreeCAD.

        Args:
            lines: Maximum number of lines to return (1-1000). Defaults to 50.

        Returns:
            Dictionary with:
                - messages: List of console messages
                - warnings: List of warning messages
                - errors: List of error messages

        Raises:
            ValueError: The line limit is outside the supported range.
            RuntimeError: Console history is unavailable in this FreeCAD session.
        """
        bridge = await get_bridge()
        console_lines = await bridge.get_console_output(lines)

        return {
            "messages": console_lines,
            "warnings": [line for line in console_lines if "warning" in line.lower()],
            "errors": [line for line in console_lines if "error" in line.lower()],
        }

    @mcp.tool()
    async def recompute(doc_name: str | None = None) -> dict[str, Any]:
        """Force recompute of all objects in a document.

        Every object is marked as changed and the document is recomputed.
        Afterwards, every object FreeCAD marks as failed is listed with
        FreeCAD's own error message, and success is False if any failed.

        Args:
            doc_name: Document to recompute. Uses active document if None.

        Returns:
            Dictionary with result:
                - success: False if any object failed to recompute
                - touch_count: Number of objects marked for recompute
                - invalid_objects: Failed objects (up to 50), each with
                  document, name, label, type_id, state, message (FreeCAD's
                  own error text), dependents (objects built on it, whose
                  results may be out of date) and, for broken links,
                  link_problems naming the missing file, document or object
                - invalid_objects_truncated: True if more objects failed
                  than are listed
                - error: Summary naming the failed objects and their
                  messages, or None on success
        """
        # Imported here to keep this fork's change inside this function.
        from freecad_mcp.bridge.object_health import OBJECT_HEALTH_HELPERS

        bridge = await get_bridge()

        code = (
            OBJECT_HEALTH_HELPERS
            + f"""
doc = FreeCAD.ActiveDocument if {doc_name!r} is None else FreeCAD.getDocument({doc_name!r})
if doc is None:
    _result_ = {{"success": False, "error": "No document found", "touch_count": 0}}
else:
    # Touch all objects to force recompute
    touch_count = 0
    for obj in doc.Objects:
        if hasattr(obj, "touch"):
            obj.touch()
            touch_count += 1

    doc.recompute()

    _result_ = _mcp_recompute_report(FreeCAD.getDependentObjects(doc.Objects))
    _result_["touch_count"] = touch_count
"""
        )
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "success": False,
            "error": result.error_traceback or "Recompute failed",
            "touch_count": 0,
        }
