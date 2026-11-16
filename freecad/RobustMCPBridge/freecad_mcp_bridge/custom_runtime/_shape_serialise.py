"""Shared helpers for reading shapes and describing their geometry.

Used by the read-only geometry operations (``geometry.py``). Everything here
works on in-memory shape copies and never changes the document. The module
name starts with ``_`` so the runtime does not scan it for operations.

Coordinates are rounded to 1e-6 mm, and quantities of unbounded shapes (such
as infinite datum planes) are reported as ``None`` with ``unbounded: True``
rather than as huge numbers.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

# Extents beyond this are treated as unbounded (FreeCAD uses 1e100 for
# infinite datum planes).
UNBOUNDED_LIMIT = 1e50

# Sample points: at most this many per edge, and in total per call.
MAX_SAMPLES_PER_EDGE = 500
MAX_SAMPLE_POINTS = 5000

# The bridge refuses results over a size budget (MAX_RESULT_BUDGET in the
# bridge's server.py: 16 units per value plus string bytes). Listings stop at
# about half of it so a large shape gives a truncated result, not an error.
RESULT_SIZE_LIMIT = 500_000

PLANE_TYPES = (
    "App::Plane",
    "PartDesign::Plane",
    "Part::DatumPlane",
    "Part::Plane",
    "Sketcher::SketchObject",
)


def num(value: float, digits: int = 6) -> float:
    """Round a float for a result, turning -0.0 into 0.0.

    Args:
        value: The number.
        digits: Decimal places to keep.

    Returns:
        The rounded number.
    """
    return round(float(value), digits) + 0.0


def vec(vector: Any) -> list[float]:
    """Return a FreeCAD vector as a rounded ``[x, y, z]`` list."""
    return [num(vector.x), num(vector.y), num(vector.z)]


def find_document(doc_name: str | None) -> Any:
    """Return the named document, or the active one.

    Args:
        doc_name: Document name, or None for the active document.

    Returns:
        The FreeCAD document.

    Raises:
        ValueError: If there is no such document.
    """
    import FreeCAD

    if doc_name:
        doc = FreeCAD.listDocuments().get(doc_name)
        if doc is None:
            names = ", ".join(sorted(FreeCAD.listDocuments())) or "none"
            raise ValueError(f"No document named {doc_name!r}. Open documents: {names}")
        return doc
    doc = FreeCAD.ActiveDocument
    if doc is None:
        raise ValueError("No active document; pass doc_name")
    return doc


def find_object(doc: Any, name: str, role: str = "Object") -> Any:
    """Find an object by name, or by label if the label is unique.

    Args:
        doc: The document.
        name: Object name or label.
        role: Word used in error messages (e.g. "Frame object").

    Returns:
        The document object.

    Raises:
        ValueError: If nothing matches or the label is ambiguous.
    """
    obj = doc.getObject(name)
    if obj is not None:
        return obj
    matches = doc.getObjectsByLabel(name)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"Several objects have the label {name!r}; use the object name: "
            + ", ".join(match.Name for match in matches)
        )
    raise ValueError(f"{role} {name!r} not found in document {doc.Name!r}")


def _identity() -> Any:
    import FreeCAD

    return FreeCAD.Placement()


def parent_placement(obj: Any) -> Any:
    """Return the global placement of the group that contains an object.

    For a PartDesign feature this is its body's placement; for an object in
    an App::Part, the part's. Objects at the top level get the identity.
    """
    getter = getattr(obj, "getParentGeoFeatureGroup", None)
    parent = getter() if getter else None
    if parent is None:
        return _identity()
    if hasattr(parent, "getGlobalPlacement"):
        return parent.getGlobalPlacement()
    return parent_placement(parent).multiply(parent.Placement)


def global_placement(obj: Any) -> Any:
    """Return an object's placement in global coordinates.

    Raises:
        ValueError: If the object has no placement.
    """
    if hasattr(obj, "getGlobalPlacement"):
        return obj.getGlobalPlacement()
    placement = getattr(obj, "Placement", None)
    if placement is None:
        raise ValueError(f"{obj.Name!r} has no placement")
    return parent_placement(obj).multiply(placement)


def global_shape(obj: Any) -> Any:
    """Return a copy of an object's shape in global coordinates.

    Uses the object's own shape (for a PartDesign body, the body's shape; for
    an App::Link, the linked shape with the link's placement), then applies
    the placement of any containing body or part.

    Args:
        obj: The document object.

    Returns:
        A new shape, independent of the document.

    Raises:
        ValueError: If the object has no shape or its shape is empty.
    """
    import Part

    shape: Any = None
    if obj.isDerivedFrom("App::Link") or not hasattr(obj, "Shape"):
        try:
            shape = Part.getShape(obj)
        except Exception as error:  # reported as a caller error
            raise ValueError(f"{obj.Name!r} has no shape: {error}") from error
    else:
        shape = obj.Shape
    if shape is None or shape.isNull():
        detail = ""
        status = getattr(obj, "getStatusString", None)
        if status is not None and "Invalid" in getattr(obj, "State", []):
            detail = f" FreeCAD reports: {status()}"
        raise ValueError(
            f"{obj.Name!r} ({obj.TypeId}) has no shape or an empty shape.{detail}"
        )
    shape = shape.copy()
    outer = parent_placement(obj)
    if not outer.isIdentity():
        shape.Placement = outer.multiply(shape.Placement)
    return shape


def placement_info(placement: Any) -> dict[str, Any]:
    """Describe a placement as position, rotation axis and angle (degrees)."""
    rotation = placement.Rotation
    return {
        "position": vec(placement.Base),
        "rotation_axis": vec(rotation.Axis),
        "rotation_angle_deg": num(math.degrees(rotation.Angle)),
    }


def resolve_frame(doc: Any, frame: str | None) -> tuple[Any, Any]:
    """Work out which coordinate frame results are reported in.

    Args:
        doc: The document.
        frame: Name of an object whose local frame to use, or None (or
            "global") for global coordinates.

    Returns:
        ``(inverse_placement, frame_info)``. The inverse placement is None
        for global coordinates.

    Raises:
        ValueError: If the frame object doesn't exist or has no placement.
    """
    if not frame or frame.lower() == "global":
        return None, "global"
    obj = find_object(doc, frame, "Frame object")
    placement = global_placement(obj)
    info = {"object": obj.Name, "label": obj.Label, **placement_info(placement)}
    info["note"] = (
        f"Coordinates are in the local frame of {obj.Name!r}, whose global "
        "placement is shown here."
    )
    return placement.inverse(), info


def to_frame(shape: Any, inverse: Any) -> Any:
    """Move a shape copy into a frame (no change when inverse is None)."""
    if inverse is None:
        return shape
    moved = shape.copy()
    moved.Placement = inverse.multiply(moved.Placement)
    return moved


def is_unbounded(bound_box: Any) -> bool:
    """True if a bounding box is invalid or extends beyond UNBOUNDED_LIMIT."""
    if not bound_box.isValid():
        return True
    values = (
        bound_box.XMin,
        bound_box.YMin,
        bound_box.ZMin,
        bound_box.XMax,
        bound_box.YMax,
        bound_box.ZMax,
    )
    return any(not math.isfinite(v) or abs(v) > UNBOUNDED_LIMIT for v in values)


def bound_box_info(bound_box: Any) -> dict[str, Any] | None:
    """Describe a bounding box (mm), or None if it is unbounded."""
    if is_unbounded(bound_box):
        return None
    return {
        "min": [num(bound_box.XMin), num(bound_box.YMin), num(bound_box.ZMin)],
        "max": [num(bound_box.XMax), num(bound_box.YMax), num(bound_box.ZMax)],
        "size": [
            num(bound_box.XLength),
            num(bound_box.YLength),
            num(bound_box.ZLength),
        ],
    }


def result_cost(value: Any) -> int:
    """Estimate a value's cost against the bridge's result size budget."""
    cost = 16
    if isinstance(value, str):
        cost += len(value.encode("utf-8"))
    elif isinstance(value, dict):
        for key, item in value.items():
            cost += result_cost(key) + result_cost(item)
    elif isinstance(value, list | tuple):
        for item in value:
            cost += result_cost(item)
    return cost


class SizeBudget:
    """Track how much of the result size limit a listing has used.

    Args:
        limit: Size units available (see RESULT_SIZE_LIMIT).
    """

    def __init__(self, limit: int = RESULT_SIZE_LIMIT) -> None:
        self.remaining = limit
        self.exhausted = False

    def take(self, value: Any) -> bool:
        """Reserve room for value; False (and exhausted) if it doesn't fit."""
        if self.exhausted:
            return False
        cost = result_cost(value)
        if cost > self.remaining:
            self.exhausted = True
            return False
        self.remaining -= cost
        return True


class SampleBudget:
    """Sample-point settings and the points left for this call.

    Args:
        count: Points per edge (evenly spaced along the curve), or None.
        tolerance: Largest gap in mm between the curve and the straight
            lines joining the points, or None.
    """

    def __init__(
        self, count: int | None = None, tolerance: float | None = None
    ) -> None:
        self.count = count
        self.tolerance = tolerance
        self.remaining = MAX_SAMPLE_POINTS
        self.omitted = 0

    @property
    def enabled(self) -> bool:
        """True if sample points were asked for."""
        return bool(self.count) or bool(self.tolerance)

    def sample(self, edge: Any) -> tuple[list[list[float]] | None, bool]:
        """Return sample points along an edge and whether they were capped.

        Returns ``(None, False)`` once the call's point budget is spent.
        """
        if self.remaining <= 0:
            self.omitted += 1
            return None, False
        capped = False
        if self.tolerance:
            points = edge.discretize(Deflection=float(self.tolerance))
            if len(points) > MAX_SAMPLES_PER_EDGE:
                points = edge.discretize(Number=MAX_SAMPLES_PER_EDGE)
                capped = True
        else:
            points = edge.discretize(Number=int(self.count or 2))
        if len(points) > self.remaining:
            self.omitted += 1
            return None, False
        self.remaining -= len(points)
        return [vec(point) for point in points], capped


class ElementIndex:
    """Find the name (``Edge3``) of a sub-shape within a shape."""

    def __init__(self, items: list[Any], prefix: str) -> None:
        self.prefix = prefix
        self.items = items
        self.by_hash: dict[int, list[int]] = {}
        self.hashed = all(hasattr(item, "hashCode") for item in items[:1])
        if self.hashed:
            for index, item in enumerate(items):
                self.by_hash.setdefault(item.hashCode(), []).append(index)

    def name(self, sub_shape: Any) -> str | None:
        """Return the element name of sub_shape, or None if not found."""
        if self.hashed:
            candidates = self.by_hash.get(sub_shape.hashCode(), [])
        else:
            candidates = list(range(len(self.items)))
        for index in candidates:
            if self.items[index].isSame(sub_shape):
                return f"{self.prefix}{index + 1}"
        return None


def _curve_details(edge: Any, curve: Any, info: dict[str, Any]) -> None:
    """Add type-specific curve details to an edge description."""
    import Part

    if isinstance(curve, Part.Line):
        info["direction"] = vec(curve.Direction)
    elif isinstance(curve, Part.Circle):
        closed = bool(edge.isClosed())
        info["curve_type"] = "Circle" if closed else "ArcOfCircle"
        info["center"] = vec(curve.Center)
        info["radius"] = num(curve.Radius)
        info["axis"] = vec(curve.Axis)
        if not closed:
            span = edge.LastParameter - edge.FirstParameter
            info["angle_deg"] = num(math.degrees(span))
    elif isinstance(curve, Part.Ellipse):
        closed = bool(edge.isClosed())
        info["curve_type"] = "Ellipse" if closed else "ArcOfEllipse"
        info["center"] = vec(curve.Center)
        info["major_radius"] = num(curve.MajorRadius)
        info["minor_radius"] = num(curve.MinorRadius)
        info["axis"] = vec(curve.Axis)
        major = getattr(curve, "XAxis", None)
        if major is not None:
            info["major_axis"] = vec(major)
    elif isinstance(curve, (Part.BSplineCurve, Part.BezierCurve)):
        info["degree"] = int(curve.Degree)
        info["pole_count"] = int(curve.NbPoles)
        info["rational"] = bool(curve.isRational())
        if isinstance(curve, Part.BSplineCurve):
            info["knot_count"] = int(curve.NbKnots)
            info["periodic"] = bool(curve.isPeriodic())


def edge_info(
    edge: Any,
    budget: SampleBudget | None = None,
    name: str | None = None,
    vertex_index: ElementIndex | None = None,
) -> dict[str, Any]:
    """Describe one edge.

    Args:
        edge: The edge.
        budget: Sample-point settings; None for no samples.
        name: Element name to include (``Edge3``).
        vertex_index: If given, the edge's vertex names are listed.

    Returns:
        Curve type, length (mm), closed flag, start and end points (in curve
        parameter order), type-specific details and optional samples.
    """
    info: dict[str, Any] = {}
    if name:
        info["name"] = name
    if getattr(edge, "Degenerated", False):
        info.update({"curve_type": "Degenerated", "degenerated": True, "length": 0.0})
        if edge.Vertexes:
            info["start"] = info["end"] = vec(edge.Vertexes[0].Point)
        return info
    try:
        curve = edge.Curve
        info["curve_type"] = type(curve).__name__
    except Exception:  # some edges have no 3D curve
        curve = None
        info["curve_type"] = None
    if is_unbounded(edge.BoundBox):
        info.update({"unbounded": True, "length": None})
        if curve is not None:
            _curve_details(edge, curve, info)
        return info
    info["length"] = num(edge.Length)
    info["closed"] = bool(edge.isClosed())
    start = edge.valueAt(edge.FirstParameter)
    end = edge.valueAt(edge.LastParameter)
    info["start"] = vec(start)
    info["end"] = vec(end)
    if vertex_index is not None and edge.Vertexes:
        ordered = sorted(edge.Vertexes, key=lambda v: v.Point.distanceToPoint(start))
        names = [vertex_index.name(v) for v in ordered]
        info["vertices"] = [n for n in names if n]
    if curve is not None:
        _curve_details(edge, curve, info)
    if budget is not None and budget.enabled:
        points, capped = budget.sample(edge)
        if points is None:
            info["samples"] = None
        else:
            info["samples"] = points
            if capped:
                info["samples_capped"] = True
    return info


def _distance(a: list[float], b: list[float]) -> float:
    return math.dist(a, b)


def _flip(info: dict[str, Any]) -> None:
    """Reverse an edge description's direction of travel."""
    info["start"], info["end"] = info["end"], info["start"]
    if "direction" in info:
        info["direction"] = [num(-value) for value in info["direction"]]
    if "axis" in info:
        info["axis"] = [num(-value) for value in info["axis"]]
    if info.get("samples"):
        info["samples"] = list(reversed(info["samples"]))


def chain_edges(
    edges: list[dict[str, Any]], tolerance: float = 1e-4
) -> list[dict[str, Any]]:
    """Orient edge descriptions so each starts where the previous one ends.

    Edges come in connection order but each keeps its own curve direction.
    Flipping start and end (and line direction, arc axis and samples) where
    needed makes the list read as one path. Edges that don't connect are
    left as they are.
    """
    usable = [e for e in edges if "start" in e and "end" in e]
    if len(usable) != len(edges) or len(edges) < 2:
        return edges
    first, second = edges[0], edges[1]
    joins_end = min(
        _distance(first["end"], second["start"]), _distance(first["end"], second["end"])
    )
    joins_start = min(
        _distance(first["start"], second["start"]),
        _distance(first["start"], second["end"]),
    )
    if joins_start < tolerance <= joins_end:
        _flip(first)
    for previous, current in itertools.pairwise(edges):
        if (
            _distance(current["start"], previous["end"]) > tolerance
            and _distance(current["end"], previous["end"]) <= tolerance
        ):
            _flip(current)
    return edges


def wire_outline(
    wires: list[Any],
    max_edges: int,
    budget: SampleBudget | None,
    size: SizeBudget | None = None,
) -> dict[str, Any]:
    """Describe planar wires, as a section outline.

    Args:
        wires: The wires.
        max_edges: Most edges to describe in total.
        budget: Sample-point settings.
        size: Result size budget; listing stops when it runs out.

    Returns:
        Wire list (closed flag, length, enclosed area for closed wires, edges
        in connection order), counts, edges returned and a truncated flag.
    """
    import Part

    total = sum(len(wire.Edges) for wire in wires)
    remaining = max_edges
    wire_list = []
    for index, wire in enumerate(wires):
        closed = bool(wire.isClosed())
        entry: dict[str, Any] = {
            "index": index,
            "closed": closed,
            "length": num(wire.Length),
            "edge_count": len(wire.Edges),
            "enclosed_area": None,
        }
        if closed:
            try:
                entry["enclosed_area"] = num(Part.Face(wire).Area)
            except Exception:  # not planar, or self-intersecting
                entry["enclosed_area"] = None
        edges = []
        for edge in getattr(wire, "OrderedEdges", None) or wire.Edges:
            if remaining <= 0:
                break
            info = edge_info(edge, budget)
            if size is not None and not size.take(info):
                remaining = 0
                break
            edges.append(info)
            remaining -= 1
        entry["edges"] = chain_edges(edges)
        wire_list.append(entry)
    returned = max_edges - remaining
    return {
        "wire_count": len(wires),
        "edge_count": total,
        "edges_returned": returned,
        "truncated": returned < total,
        "wires": wire_list,
    }
