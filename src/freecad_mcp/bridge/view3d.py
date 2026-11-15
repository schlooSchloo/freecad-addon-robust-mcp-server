"""Find a document's 3D view and read or change its camera without pivy.

The functions here run inside FreeCAD: the screenshot, camera and view-angle
code builders include this module's source in the code they send. They use
only the 3D view's own Python methods (``getCamera``/``setCamera`` with an
Open Inventor camera description), so they work where the coin scene-graph
bindings (pivy) are not usable. Apart from ``resolve_3d_view``, which is given
the FreeCAD modules, the functions are plain Python and can be tested without
FreeCAD.

Keep this module free of ``from __future__`` imports and of imports from this
package: its source is placed after other code before it is sent.
"""

import math
import re
from typing import Any

VIEW3D_TYPE_NAME = "Gui::View3DInventor"
CAMERA_DEFAULTS: dict[str, tuple[float, ...]] = {
    "position": (0.0, 0.0, 1.0),
    "orientation": (0.0, 0.0, 1.0, 0.0),
    "focalDistance": (5.0,),
    "height": (2.0,),
    "heightAngle": (math.pi / 4,),
}


def _view_supports(view: Any, required: tuple[str, ...]) -> bool:
    """Return whether a view has every required method."""
    return view is not None and all(
        callable(getattr(view, name, None)) for name in required
    )


def find_3d_view(gui_doc: Any, required: tuple[str, ...]) -> tuple[Any, str | None]:
    """Return the document's 3D view, preferring its active view.

    When the document's active view is not a 3D view (a spreadsheet or
    TechDraw page, say), the document's other views are searched for one.
    The view is used as it is; it does not need to be the active window.

    Args:
        gui_doc: The FreeCADGui document.
        required: View methods the caller needs.

    Returns:
        The view, and a note for the caller when it is not the active view.

    Raises:
        ValueError: The document has no view with the required methods.
    """
    active = gui_doc.activeView()
    if _view_supports(active, required):
        return active, None
    finder = getattr(gui_doc, "mdiViewsOfType", None)
    candidates = finder(VIEW3D_TYPE_NAME) if callable(finder) else []
    active_kind = type(active).__name__ if active is not None else "no view"
    for view in candidates or []:
        if _view_supports(view, required):
            note = (
                f"The document's active view is not a 3D view ({active_kind}); "
                "its 3D view was used instead."
            )
            return view, note
    name = getattr(getattr(gui_doc, "Document", None), "Name", "?")
    raise ValueError(
        f"Document {name!r} has no supported 3D view (its active view is "
        f"{active_kind}). Open a 3D view of the document first."
    )


def resolve_3d_view(
    freecad: Any, freecad_gui: Any, doc_name: str | None, required: tuple[str, ...]
) -> tuple[Any, Any, str | None]:
    """Return the requested document, its 3D view and an optional note.

    Args:
        freecad: The FreeCAD module.
        freecad_gui: The FreeCADGui module.
        doc_name: Document name, or None for the active document.
        required: View methods the caller needs.

    Returns:
        The document, its 3D view, and a note when the view is not active.

    Raises:
        ValueError: The document or a suitable 3D view does not exist.
        RuntimeError: FreeCAD is running without its GUI.
    """
    if doc_name is None:
        doc = freecad.ActiveDocument
        if doc is None:
            raise ValueError("No active document")
    else:
        try:
            doc = freecad.getDocument(doc_name)
        except (NameError, LookupError, ValueError):
            doc = None
        if doc is None:
            raise ValueError(f"No document named {doc_name!r}")
    if not freecad.GuiUp:
        raise RuntimeError("GUI not available - this needs FreeCAD's GUI")
    gui_doc = freecad_gui.getDocument(doc.Name)
    if gui_doc is None:
        raise ValueError(f"Document {doc.Name!r} has no GUI view")
    view, note = find_3d_view(gui_doc, required)
    return doc, view, note


def _field_pattern(name: str) -> re.Pattern[str]:
    """Match one ``name value...`` line of an Inventor camera description."""
    return re.compile(rf"(?m)^([ \t]*){name}[ \t]+([^\n]*)$")


def _camera_numbers(text: str, name: str) -> tuple[float, ...]:
    """Read a numeric camera field, falling back to the Inventor default."""
    match = _field_pattern(name).search(text)
    if match is None:
        return CAMERA_DEFAULTS[name]
    try:
        values = tuple(float(part) for part in match.group(2).split())
    except ValueError:
        return CAMERA_DEFAULTS[name]
    if len(values) != len(CAMERA_DEFAULTS[name]):
        return CAMERA_DEFAULTS[name]
    return values


def camera_kind(text: str) -> str:
    """Return "Perspective", "Orthographic" or "Unknown" for a camera text."""
    if re.search(r"\bPerspectiveCamera\b", text):
        return "Perspective"
    if re.search(r"\bOrthographicCamera\b", text):
        return "Orthographic"
    return "Unknown"


def _rotate(
    axis_angle: tuple[float, ...], vector: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Rotate a vector by an axis-angle rotation (Rodrigues' formula)."""
    ax, ay, az, angle = axis_angle
    length = math.sqrt(ax * ax + ay * ay + az * az)
    if length < 1e-12 or abs(angle) < 1e-12:
        return vector
    kx, ky, kz = ax / length, ay / length, az / length
    vx, vy, vz = vector
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    dot = kx * vx + ky * vy + kz * vz
    cross = (ky * vz - kz * vy, kz * vx - kx * vz, kx * vy - ky * vx)
    return (
        vx * cos_a + cross[0] * sin_a + kx * dot * (1 - cos_a),
        vy * cos_a + cross[1] * sin_a + ky * dot * (1 - cos_a),
        vz * cos_a + cross[2] * sin_a + kz * dot * (1 - cos_a),
    )


def _rounded(values: tuple[float, ...] | list[float]) -> list[float]:
    """Round coordinates for reporting."""
    return [round(value, 6) + 0.0 for value in values]


def camera_info(text: str) -> dict[str, Any]:
    """Describe an Inventor camera in world terms.

    Args:
        text: The camera description from ``view.getCamera()``.

    Returns:
        Camera type, position, viewing direction, up direction, the point
        looked at (position plus direction times focal distance), the focal
        distance, and the visible height (orthographic, in model units) or
        field of view (perspective, in degrees).
    """
    kind = camera_kind(text)
    position = _camera_numbers(text, "position")
    orientation = _camera_numbers(text, "orientation")
    focal = _camera_numbers(text, "focalDistance")[0]
    direction = _rotate(orientation, (0.0, 0.0, -1.0))
    up = _rotate(orientation, (0.0, 1.0, 0.0))
    look_at = tuple(p + d * focal for p, d in zip(position, direction, strict=True))
    info: dict[str, Any] = {
        "type": kind,
        "position": _rounded(position),
        "view_direction": _rounded(direction),
        "up_direction": _rounded(up),
        "look_at": _rounded(look_at),
        "focal_distance": round(focal, 6),
    }
    if kind == "Perspective":
        angle = _camera_numbers(text, "heightAngle")[0]
        info["field_of_view_degrees"] = round(math.degrees(angle), 6)
    else:
        info["view_height"] = round(_camera_numbers(text, "height")[0], 6)
    return info


def _format_numbers(values: tuple[float, ...] | list[float]) -> str:
    """Format numbers for an Inventor field."""
    return " ".join(f"{float(value):.9g}" for value in values)


def update_camera_text(text: str, fields: dict[str, tuple[float, ...]]) -> str:
    """Replace (or add) fields in an Inventor camera description.

    Args:
        text: The camera description from ``view.getCamera()``.
        fields: Field names (``position``, ``orientation``, ``focalDistance``,
            ``height``, ...) and their new values.

    Returns:
        The updated description, for ``view.setCamera()``.

    Raises:
        ValueError: The text is not an Inventor camera description.
    """
    if camera_kind(text) == "Unknown" or "}" not in text:
        raise ValueError("The view's camera description could not be read")
    for name, values in fields.items():
        pattern = _field_pattern(name)
        value_text = _format_numbers(values)
        match = pattern.search(text)
        if match is not None:
            line = f"{match.group(1)}{name} {value_text}"
            text = text[: match.start()] + line + text[match.end() :]
        else:
            end = text.rindex("}")
            text = f"{text[:end]}  {name} {value_text}\n{text[end:]}"
    return text


def _normalized(vector: tuple[float, ...] | list[float]) -> tuple[float, float, float]:
    """Return a unit vector, or raise for a zero-length one."""
    x, y, z = (float(value) for value in vector)
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-12:
        raise ValueError("Direction vectors must not have zero length")
    return (x / length, y / length, z / length)


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Return the cross product of two vectors."""
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def look_at_orientation(
    direction: tuple[float, ...] | list[float],
    up: tuple[float, ...] | list[float] | None = None,
) -> tuple[float, float, float, float]:
    """Return the camera orientation (axis x, y, z and angle) for a view.

    The camera looks along its local -Z axis with local +Y up.

    Args:
        direction: Viewing direction (need not be unit length).
        up: Approximate up direction. Defaults to +Z, or +Y when looking
            straight up or down.

    Returns:
        Axis-angle rotation as used by Inventor's ``orientation`` field.

    Raises:
        ValueError: A vector has zero length, or ``up`` is parallel to the
            viewing direction.
    """
    forward = _normalized(direction)
    if up is None:
        up_hint = (0.0, 1.0, 0.0) if abs(forward[2]) > 0.999 else (0.0, 0.0, 1.0)
    else:
        up_hint = _normalized(up)
    right_raw = _cross(forward, up_hint)
    if math.sqrt(sum(value * value for value in right_raw)) < 1e-6:
        raise ValueError("up_vector must not be parallel to the viewing direction")
    right = _normalized(right_raw)
    true_up = _cross(right, forward)
    back = (-forward[0], -forward[1], -forward[2])
    # Rotation matrix with the camera's local axes as columns.
    m00, m01, m02 = right[0], true_up[0], back[0]
    m10, m11, m12 = right[1], true_up[1], back[1]
    m20, m21, m22 = right[2], true_up[2], back[2]
    trace = m00 + m11 + m22
    if trace > 0:
        s = 0.5 / math.sqrt(trace + 1.0)
        w, x, y, z = 0.25 / s, (m21 - m12) * s, (m02 - m20) * s, (m10 - m01) * s
    elif m00 > m11 and m00 > m22:
        s = 2.0 * math.sqrt(1.0 + m00 - m11 - m22)
        w, x, y, z = (m21 - m12) / s, 0.25 * s, (m01 + m10) / s, (m02 + m20) / s
    elif m11 > m22:
        s = 2.0 * math.sqrt(1.0 + m11 - m00 - m22)
        w, x, y, z = (m02 - m20) / s, (m01 + m10) / s, 0.25 * s, (m12 + m21) / s
    else:
        s = 2.0 * math.sqrt(1.0 + m22 - m00 - m11)
        w, x, y, z = (m10 - m01) / s, (m02 + m20) / s, (m12 + m21) / s, 0.25 * s
    if w < 0:
        w, x, y, z = -w, -x, -y, -z
    angle = 2.0 * math.acos(min(1.0, w))
    sin_half = math.sqrt(max(0.0, 1.0 - w * w))
    if sin_half < 1e-9:
        return (0.0, 0.0, 1.0, 0.0)
    return (x / sin_half, y / sin_half, z / sin_half, angle)


def look_at_camera_text(
    text: str,
    position: tuple[float, ...] | list[float],
    look_at: tuple[float, ...] | list[float],
    up: tuple[float, ...] | list[float] | None = None,
    view_height: float | None = None,
) -> str:
    """Return a camera description placed at ``position`` looking at ``look_at``.

    Args:
        text: The current camera description (its type and other fields are
            kept).
        position: Camera position.
        look_at: Point to look at; it becomes the focal point.
        up: Approximate up direction (see look_at_orientation).
        view_height: Visible height in model units for orthographic cameras.
            Ignored for perspective cameras, whose visible size follows from
            the distance.

    Returns:
        The updated camera description.

    Raises:
        ValueError: The points coincide, or the vectors are unusable.
    """
    offset = tuple(
        float(target) - float(origin)
        for target, origin in zip(look_at, position, strict=True)
    )
    distance = math.sqrt(sum(value * value for value in offset))
    if distance < 1e-9:
        raise ValueError("position and look_at must be different points")
    fields: dict[str, tuple[float, ...]] = {
        "position": tuple(float(value) for value in position),
        "orientation": look_at_orientation(offset, up),
        "focalDistance": (distance,),
    }
    if view_height is not None and camera_kind(text) == "Orthographic":
        fields["height"] = (float(view_height),)
    return update_camera_text(text, fields)


def zoom_camera_text(text: str, factor: float) -> str:
    """Return a camera description zoomed in by ``factor`` (below 1 zooms out).

    Orthographic cameras show a ``factor`` times smaller height. Perspective
    cameras move ``factor`` times closer to their focal point.

    Args:
        text: The current camera description.
        factor: Zoom factor, greater than zero.

    Returns:
        The updated camera description.

    Raises:
        ValueError: The factor is not positive or the camera type is unknown.
    """
    if not factor > 0 or not math.isfinite(factor):
        raise ValueError("Zoom factor must be a positive number")
    kind = camera_kind(text)
    if kind == "Orthographic":
        height = _camera_numbers(text, "height")[0]
        return update_camera_text(text, {"height": (height / factor,)})
    if kind == "Perspective":
        position = _camera_numbers(text, "position")
        orientation = _camera_numbers(text, "orientation")
        focal = _camera_numbers(text, "focalDistance")[0]
        direction = _rotate(orientation, (0.0, 0.0, -1.0))
        new_focal = focal / factor
        new_position = tuple(
            p + d * (focal - new_focal)
            for p, d in zip(position, direction, strict=True)
        )
        return update_camera_text(
            text, {"position": new_position, "focalDistance": (new_focal,)}
        )
    raise ValueError("The view's camera description could not be read")
