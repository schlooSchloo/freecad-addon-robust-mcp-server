"""Helpers shared by the mesh operations (not scanned for operations)."""

from __future__ import annotations

import math
from typing import Any


def round_value(value: float, digits: int = 6) -> float:
    """Round to ``digits`` decimal places, avoiding ``-0.0``.

    Args:
        value: Number to round.
        digits: Decimal places (6 decimals is 1e-6 mm for lengths).

    Returns:
        The rounded number.
    """
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def bounds_of(bound_box: Any) -> dict[str, Any] | None:
    """Describe a FreeCAD bounding box as a plain dictionary.

    Args:
        bound_box: A ``FreeCAD.BoundBox``.

    Returns:
        Dictionary with ``min``, ``max`` and ``size`` (mm), or None if the box
        is not valid (nothing to bound).
    """
    if not bound_box.isValid():
        return None
    return {
        "min": [
            round_value(v) for v in (bound_box.XMin, bound_box.YMin, bound_box.ZMin)
        ],
        "max": [
            round_value(v) for v in (bound_box.XMax, bound_box.YMax, bound_box.ZMax)
        ],
        "size": [
            round_value(v)
            for v in (bound_box.XLength, bound_box.YLength, bound_box.ZLength)
        ],
    }


def get_document(doc_name: str | None) -> Any:
    """Return the named document, or the active one.

    Args:
        doc_name: Document name, or None for the active document.

    Returns:
        The FreeCAD document.

    Raises:
        ValueError: If there is no such document or no active document.
    """
    import FreeCAD

    if doc_name:
        doc = FreeCAD.listDocuments().get(doc_name)
        if doc is None:
            raise ValueError(f"Document {doc_name!r} not found")
        return doc
    doc = FreeCAD.ActiveDocument
    if doc is None:
        raise ValueError("No active document")
    return doc


def get_object(doc: Any, name: Any) -> Any:
    """Find an object by internal name, then by a unique label.

    Args:
        doc: FreeCAD document.
        name: Object name or label.

    Returns:
        The object.

    Raises:
        ValueError: If the name is missing, unknown or an ambiguous label.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("An object name is required")
    obj = doc.getObject(name)
    if obj is not None:
        return obj
    matches = doc.getObjectsByLabel(name)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"Label {name!r} matches {len(matches)} objects; use the object name"
        )
    raise ValueError(f"Object {name!r} not found in document {doc.Name!r}")


def global_shape(obj: Any) -> Any:
    """Return a copy of an object's shape placed in global coordinates.

    Args:
        obj: Object with a ``Shape`` (Part feature, body, link to one).

    Returns:
        A ``Part.Shape`` copy; the document is not touched.

    Raises:
        ValueError: If the object has no usable shape.
    """
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError(f"Object {obj.Name!r} has no shape")
    shape = shape.copy()
    if hasattr(obj, "getGlobalPlacement"):
        shape.Placement = obj.getGlobalPlacement()
    return shape


def check_deflection(linear: Any, angular_degrees: Any) -> tuple[float, float]:
    """Validate meshing settings.

    Args:
        linear: Linear deflection (mm, or a fraction when relative).
        angular_degrees: Angular deflection in degrees.

    Returns:
        Both values as floats.

    Raises:
        ValueError: If either is not a positive finite number, or the angle
            exceeds 90 degrees.
    """
    values = []
    for label, value in (
        ("linear_deflection", linear),
        ("angular_deflection", angular_degrees),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be a number")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be greater than 0")
        values.append(float(value))
    if values[1] > 90:
        raise ValueError("angular_deflection must be 90 degrees or less")
    return values[0], values[1]


def tessellate(
    shape: Any, linear: float, angular_degrees: float, relative: bool
) -> Any:
    """Tessellate a shape into an in-memory ``Mesh.Mesh``.

    Args:
        shape: A ``Part.Shape`` with faces, already in global coordinates.
        linear: Linear deflection (mm, or a fraction of edge size if relative).
        angular_degrees: Angular deflection in degrees.
        relative: Whether the linear deflection is relative.

    Returns:
        A mesh that is not part of any document.

    Raises:
        ValueError: If the shape has no faces or meshing gives no facets.
    """
    import MeshPart

    if not shape.Faces:
        raise ValueError("The shape has no faces to mesh")
    mesh = MeshPart.meshFromShape(
        Shape=shape,
        LinearDeflection=linear,
        AngularDeflection=math.radians(angular_degrees),
        Relative=bool(relative),
    )
    if mesh.CountFacets == 0:
        raise ValueError("Meshing produced no facets; try a smaller deflection")
    return mesh
