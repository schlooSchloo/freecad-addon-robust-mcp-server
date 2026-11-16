"""Edit existing sketch geometry with the sketch solver.

Operations here move points of existing geometry, change a circle or arc
radius, change the value of a dimensional constraint, and add a connected
polyline in one call. Each one recomputes the document and reports the
sketch's solver state: degrees of freedom, conflicting and redundant
constraints, and whether the sketch's wires are closed.

The MCP server wraps every call in a transaction, so a call is one undo step,
and an error aborts it without leaving half-made changes.

Constraint indices in results are 0-based, like the indices the sketch tools
take. FreeCAD's own conflicting and redundant lists are 1-based; they are
converted here.
"""

from __future__ import annotations

import math
from typing import Any

# Point positions as FreeCAD's sketcher numbers them.
POINT_POSITIONS = {"start": 1, "end": 2, "center": 3}

# Dimensional constraint types whose value can be changed, and the kind of
# quantity each holds.
DIMENSIONAL_TYPES = {
    "Distance": "length",
    "DistanceX": "length",
    "DistanceY": "length",
    "Radius": "length",
    "Diameter": "length",
    "Angle": "angle",
}

# Segments within this angle of an axis get a horizontal or vertical
# constraint when auto_horizontal_vertical is on.
AXIS_TOLERANCE_DEGREES = 0.5

# Shortest segment add_sketch_polyline accepts, in mm.
MIN_SEGMENT_LENGTH = 1e-6

# Longest list of constraints reported in one category.
MAX_LISTED = 20

# How close a moved point must be to its target to count as reached, in mm.
REACHED_TOLERANCE = 1e-6

_SOLVER_STATUS = {
    0: "ok",
    -1: "solver_failed",
    -2: "redundant",
    -3: "conflicting",
    -4: "over_constrained",
    -5: "malformed",
}


def _round(value: float) -> float:
    """Round a length or angle for reporting (1e-6 resolution)."""
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded


def _xy(vector: Any) -> list[float]:
    """Return a sketch-plane point as a rounded [x, y] list."""
    return [_round(vector.x), _round(vector.y)]


# ---------------------------------------------------------------------------
# Looking things up
# ---------------------------------------------------------------------------


def _get_document(args: dict[str, Any]) -> Any:
    """Return the requested document, or the active one."""
    import FreeCAD

    doc_name = args.get("doc_name")
    if doc_name:
        if doc_name not in FreeCAD.listDocuments():
            raise ValueError(f"Document not found: {doc_name!r}")
        return FreeCAD.getDocument(doc_name)
    if FreeCAD.ActiveDocument is None:
        raise ValueError("No active document")
    return FreeCAD.ActiveDocument


def _get_sketch(args: dict[str, Any]) -> tuple[Any, Any]:
    """Return the document and the sketch named in the arguments."""
    doc = _get_document(args)
    name = args.get("sketch_name")
    if not name:
        raise ValueError("sketch_name is required")
    sketch = doc.getObject(name)
    if sketch is None:
        matches = doc.getObjectsByLabel(name)
        sketch = matches[0] if len(matches) == 1 else None
    if sketch is None:
        raise ValueError(f"Sketch not found: {name!r}")
    if not sketch.isDerivedFrom("Sketcher::SketchObject"):
        raise ValueError(f"{sketch.Name!r} is a {sketch.TypeId}, not a sketch")
    return doc, sketch


def _geometry_index(sketch: Any, args: dict[str, Any]) -> int:
    """Return a validated index of the sketch's own geometry."""
    index = args.get("geometry_index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise ValueError("geometry_index must be an integer")
    count = len(sketch.Geometry)
    if index < 0:
        raise ValueError(
            "Only the sketch's own geometry (index 0 or more) can be edited; "
            "negative indices are axes and external geometry"
        )
    if index >= count:
        raise ValueError(
            f"Geometry index {index} is out of range: {sketch.Name!r} has "
            f"{count} geometry element(s)"
        )
    return index


def _geometry_kind(geometry: Any) -> str:
    """Return a short type name for a sketch geometry, e.g. 'LineSegment'."""
    return type(geometry).__name__


def _constraint_summary(sketch: Any, index: int) -> dict[str, Any]:
    """Describe one constraint compactly."""
    constraint = sketch.Constraints[index]
    summary: dict[str, Any] = {"index": index, "type": constraint.Type}
    if constraint.Name:
        summary["name"] = constraint.Name
    return summary


def _constraints_on_geometry(sketch: Any, index: int) -> list[dict[str, Any]]:
    """Return the constraints that reference a geometry index."""
    found = []
    for number, constraint in enumerate(sketch.Constraints):
        if index in (constraint.First, constraint.Second, constraint.Third):
            found.append(_constraint_summary(sketch, number))
    return found


# ---------------------------------------------------------------------------
# Sketch state
# ---------------------------------------------------------------------------


def _solver_list(sketch: Any, attribute: str) -> list[int]:
    """Read one of FreeCAD's 1-based constraint lists as 0-based indices."""
    values = getattr(sketch, attribute, None) or []
    count = len(sketch.Constraints)
    return [value - 1 for value in values if 0 < value <= count]


def _wire_state(sketch: Any) -> dict[str, Any]:
    """Report how the sketch's (non-construction) edges join into wires."""
    shape = sketch.Shape
    if shape.isNull() or not shape.Edges:
        return {"wire_count": 0, "closed_wires": 0, "all_wires_closed": None}
    wires = shape.Wires
    closed = sum(1 for wire in wires if wire.isClosed())
    in_wires = sum(len(wire.Edges) for wire in wires)
    state: dict[str, Any] = {
        "wire_count": len(wires),
        "closed_wires": closed,
        "all_wires_closed": bool(wires) and closed == len(wires),
    }
    loose = len(shape.Edges) - in_wires
    if loose > 0:
        state["edges_not_in_wires"] = loose
        state["all_wires_closed"] = False
    return state


def _recompute_errors(doc: Any, max_listed: int = 10) -> dict[str, Any]:
    """List objects left in an error state after recompute."""
    errors = []
    for obj in doc.Objects:
        state = getattr(obj, "State", [])
        if "Invalid" in state or "Error" in state:
            message = ""
            if hasattr(obj, "getStatusString"):
                message = str(obj.getStatusString()).strip()
            errors.append({"name": obj.Name, "message": message})
    return {
        "objects_in_error": errors[:max_listed],
        "objects_in_error_truncated": len(errors) > max_listed,
    }


def sketch_state(doc: Any, sketch: Any) -> dict[str, Any]:
    """Solve, recompute and describe a sketch.

    Args:
        doc: The sketch's document.
        sketch: The sketch.

    Returns:
        Counts, degrees of freedom, solver status, conflicting, redundant,
        partially redundant and malformed constraints (0-based, capped at
        MAX_LISTED each), wire closure, and any objects left in error by the
        recompute (for example a pad broken by the change).
    """
    code = sketch.solve()
    doc.recompute()
    state: dict[str, Any] = {
        "geometry_count": len(sketch.Geometry),
        "constraint_count": len(sketch.Constraints),
        "dof": getattr(sketch, "DoF", None),
        "fully_constrained": getattr(sketch, "FullyConstrained", None),
        "solver_status": _SOLVER_STATUS.get(code, f"code {code}"),
    }
    truncated = False
    for key, attribute in (
        ("conflicting", "ConflictingConstraints"),
        ("redundant", "RedundantConstraints"),
        ("partially_redundant", "PartiallyRedundantConstraints"),
        ("malformed", "MalformedConstraints"),
    ):
        indices = _solver_list(sketch, attribute)
        truncated = truncated or len(indices) > MAX_LISTED
        state[key] = [_constraint_summary(sketch, i) for i in indices[:MAX_LISTED]]
    state["lists_truncated"] = truncated
    state.update(_wire_state(sketch))
    state.update(_recompute_errors(doc))
    return state


# ---------------------------------------------------------------------------
# Moving points
# ---------------------------------------------------------------------------


def _point(sketch: Any, index: int, position: int, point: str) -> Any:
    """Return a geometry's point, or explain that it has no such point."""
    geometry = sketch.Geometry[index]
    kind = _geometry_kind(geometry)
    # Circles and ellipses only have a centre; lines and points have none.
    has_center = hasattr(geometry, "Center")
    has_ends = hasattr(geometry, "StartPoint") or kind == "Point"
    if (position == 3 and not has_center) or (position in (1, 2) and not has_ends):
        options = [
            name
            for name, number in POINT_POSITIONS.items()
            if (number == 3 and has_center) or (number in (1, 2) and has_ends)
        ]
        if kind == "Point":
            options = ["start"]
        raise ValueError(
            f"Geometry {index} ({kind}) has no {point} point; use "
            + " or ".join(repr(option) for option in options)
        )
    if kind == "Point" and position == 2:
        raise ValueError(f"Geometry {index} (Point) has no end point; use 'start'")
    return sketch.getPoint(index, position)


def _move(sketch: Any, index: int, position: int, target: Any) -> str | None:
    """Move a point with the solver; return FreeCAD's error, if any."""
    move = getattr(sketch, "moveGeometry", None) or sketch.movePoint
    try:
        move(index, position, target, False)
    except ValueError as error:
        return str(error)
    return None


def move_point(args: dict[str, Any]) -> dict[str, Any]:
    """Move a point of existing sketch geometry, respecting constraints.

    The solver drags the point towards the target, moving connected geometry
    as the constraints allow (like dragging in the sketcher). A point held by
    constraints stays where it is or moves only partly; the result says so.

    Args:
        args: ``sketch_name``, ``geometry_index``, ``point`` ("start", "end"
            or "center"), ``position`` ([x, y] in mm, sketch coordinates, or
            an offset when ``relative`` is true), optional ``relative`` and
            ``doc_name``.

    Returns:
        The point's position before and after, whether it moved and reached
        the target, the constraints on that geometry when it didn't, and the
        sketch state.
    """
    import FreeCAD

    doc, sketch = _get_sketch(args)
    index = _geometry_index(sketch, args)
    point = args.get("point", "start")
    if point not in POINT_POSITIONS:
        raise ValueError("point must be 'start', 'end' or 'center'")
    position = POINT_POSITIONS[point]
    raw = args.get("position")
    if (
        not isinstance(raw, list | tuple)
        or len(raw) != 2
        or not all(isinstance(v, int | float) for v in raw)
    ):
        raise ValueError("position must be [x, y] in mm")
    before = _point(sketch, index, position, point)
    if args.get("relative"):
        target = FreeCAD.Vector(before.x + raw[0], before.y + raw[1], 0)
    else:
        target = FreeCAD.Vector(float(raw[0]), float(raw[1]), 0)

    error = _move(sketch, index, position, target)
    state = sketch_state(doc, sketch)
    after = sketch.getPoint(index, position)
    miss = math.hypot(after.x - target.x, after.y - target.y)
    moved = math.hypot(after.x - before.x, after.y - before.y) > REACHED_TOLERANCE
    result: dict[str, Any] = {
        "sketch": sketch.Name,
        "geometry_index": index,
        "geometry_type": _geometry_kind(sketch.Geometry[index]),
        "point": point,
        "before": _xy(before),
        "requested": _xy(target),
        "after": _xy(after),
        "moved": moved,
        "reached": miss <= REACHED_TOLERANCE,
        "distance_from_requested": _round(miss),
    }
    if miss > REACHED_TOLERANCE:
        held = _constraints_on_geometry(sketch, index)
        where = " and left it where it was" if not moved else "; it moved part way"
        if state["solver_status"] == "ok":
            reason = (
                ". Constraints hold it: change or delete one of them, or change "
                "a dimension with set_sketch_constraint_value."
            )
        else:
            reason = (
                f". The sketch doesn't solve ({state['solver_status']}): fix "
                "the constraints listed in sketch_state first."
            )
        result["message"] = "The solver could not put the point there" + where + reason
        if error:
            result["solver_message"] = error
        result["constraints_on_geometry"] = held[:MAX_LISTED]
        result["constraints_on_geometry_truncated"] = len(held) > MAX_LISTED
    result["sketch_state"] = state
    return result


# ---------------------------------------------------------------------------
# Radius
# ---------------------------------------------------------------------------


def set_radius(args: dict[str, Any]) -> dict[str, Any]:
    """Set a circle or arc radius when no constraint sets it.

    Adds a temporary radius constraint, solves, then removes it, so other
    constraints (tangency, coincidence, equality) are respected and connected
    geometry follows.

    Args:
        args: ``sketch_name``, ``geometry_index``, ``radius`` (mm, above 0),
            optional ``doc_name``.

    Returns:
        The radius and centre before and after, and the sketch state.

    Raises:
        ValueError: If the geometry isn't a circle or arc, a radius or
            diameter constraint already sets it (the message names it), or
            other constraints prevent the change.
    """
    import Sketcher

    doc, sketch = _get_sketch(args)
    index = _geometry_index(sketch, args)
    radius = args.get("radius")
    if not isinstance(radius, int | float) or isinstance(radius, bool) or radius <= 0:
        raise ValueError("radius must be a number above 0 (mm)")
    geometry = sketch.Geometry[index]
    kind = _geometry_kind(geometry)
    if kind not in ("Circle", "ArcOfCircle"):
        raise ValueError(
            f"Geometry {index} is a {kind}; set_sketch_radius works on circles "
            "and arcs of circles"
        )
    for number, constraint in enumerate(sketch.Constraints):
        if constraint.Type in ("Radius", "Diameter") and constraint.First == index:
            label = f"{number}" + (f" ({constraint.Name!r})" if constraint.Name else "")
            raise ValueError(
                f"Geometry {index}'s size is set by {constraint.Type} constraint "
                f"{label}; change its value with set_sketch_constraint_value "
                "instead"
            )
    before_radius = geometry.Radius
    before_center = geometry.Center

    temporary = sketch.addConstraint(
        Sketcher.Constraint("Radius", index, float(radius))
    )
    code = sketch.solve()
    if code != 0:
        conflicting = [
            _constraint_summary(sketch, i)
            for i in _solver_list(sketch, "ConflictingConstraints")
            if i != temporary
        ]
        redundant = [
            _constraint_summary(sketch, i)
            for i in _solver_list(sketch, "RedundantConstraints")
            if i != temporary
        ]
        raise ValueError(
            f"Other constraints fix geometry {index}'s radius, so it can't be "
            f"set to {radius} mm (solver: "
            f"{_SOLVER_STATUS.get(code, code)}; conflicting with: "
            f"{conflicting or redundant}). Change those constraints instead."
        )
    sketch.delConstraint(temporary)
    state = sketch_state(doc, sketch)
    after = sketch.Geometry[index]
    return {
        "sketch": sketch.Name,
        "geometry_index": index,
        "geometry_type": kind,
        "radius_before": _round(before_radius),
        "radius": _round(after.Radius),
        "center_before": _xy(before_center),
        "center": _xy(after.Center),
        "sketch_state": state,
    }


# ---------------------------------------------------------------------------
# Constraint values
# ---------------------------------------------------------------------------


def _find_constraint(sketch: Any, reference: Any) -> int:
    """Return a constraint index from an index or a name."""
    count = len(sketch.Constraints)
    if isinstance(reference, str) and reference.strip().isdigit():
        reference = int(reference.strip())
    if isinstance(reference, int) and not isinstance(reference, bool):
        if not 0 <= reference < count:
            raise ValueError(
                f"Constraint index {reference} is out of range: {sketch.Name!r} "
                f"has {count} constraint(s)"
            )
        return reference
    if isinstance(reference, str) and reference:
        for number, constraint in enumerate(sketch.Constraints):
            if constraint.Name == reference:
                return number
        names = [c.Name for c in sketch.Constraints if c.Name]
        raise ValueError(
            f"No constraint named {reference!r} in {sketch.Name!r}. Named "
            f"constraints: {names[:MAX_LISTED] or 'none'}"
        )
    raise ValueError("constraint must be a 0-based index or a constraint name")


def _expression_for(sketch: Any, index: int) -> str | None:
    """Return the expression that drives a constraint, if any."""
    name = sketch.Constraints[index].Name
    paths = {f"Constraints[{index}]"}
    if name:
        paths.add(f"Constraints.{name}")
    for path, expression in getattr(sketch, "ExpressionEngine", []):
        if path.lstrip(".") in paths:
            return str(expression)
    return None


def _value_in_units(value: Any, kind: str) -> float:
    """Convert a value (number or string with units) to mm or degrees."""
    import FreeCAD

    unit_name = "mm" if kind == "length" else "deg"
    if isinstance(value, bool):
        raise ValueError("value must be a number or a string with units")
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            quantity = FreeCAD.Units.Quantity(value.strip())
        except (ValueError, RuntimeError, OSError) as error:
            raise ValueError(f"Can't read {value!r} as a quantity: {error}") from None
        if quantity.Unit == FreeCAD.Units.Unit():
            number = float(quantity.Value)
        else:
            reference = FreeCAD.Units.Quantity(f"1 {unit_name}")
            if quantity.Unit != reference.Unit:
                wanted = (
                    "a length (e.g. '12 mm')"
                    if kind == "length"
                    else ("an angle (e.g. '30 deg')")
                )
                raise ValueError(f"{value!r} is not {wanted}")
            number = float(quantity.getValueAs(unit_name))
    else:
        raise ValueError("value must be a number or a string with units")
    if not math.isfinite(number):
        raise ValueError("value must be finite")
    return number


def _datum(sketch: Any, index: int, kind: str) -> float:
    """Read a constraint's value in mm or degrees."""
    quantity = sketch.getDatum(index)
    return float(quantity.getValueAs("mm" if kind == "length" else "deg"))


def _datum_failure(message: str) -> str:
    """Explain why FreeCAD refused a new constraint value.

    FreeCAD reverts the value when the sketch can't be solved with it, and
    reports that as "Invalid constraint index", which is misleading.
    """
    if "conflicting" in message or "redundant" in message:
        return message + "; fix the sketch's constraints first."
    if message.startswith("Invalid constraint index"):
        return (
            "the sketch can't be solved with that value (it doesn't fit the "
            "other constraints)."
        )
    return message + "."


def set_constraint_value(args: dict[str, Any]) -> dict[str, Any]:
    """Change the value of a dimensional constraint and solve.

    Args:
        args: ``sketch_name``, ``constraint`` (0-based index or name),
            ``value`` (number in mm or degrees, or a string with units such
            as "12 mm", "0.5 in", "30 deg"), optional ``doc_name``.

    Returns:
        The constraint, its value before and after, and the sketch state.

    Raises:
        ValueError: If the constraint isn't dimensional, is a reference
            (non-driving) constraint, is driven by an expression, or the
            solver can't satisfy the new value (nothing is changed).
    """
    doc, sketch = _get_sketch(args)
    index = _find_constraint(sketch, args.get("constraint"))
    constraint = sketch.Constraints[index]
    label = f"{index}" + (f" ({constraint.Name!r})" if constraint.Name else "")
    kind = DIMENSIONAL_TYPES.get(constraint.Type)
    if kind is None:
        raise ValueError(
            f"Constraint {label} is a {constraint.Type} constraint, which has "
            "no value. Dimensional types: " + ", ".join(DIMENSIONAL_TYPES)
        )
    if not constraint.Driving:
        raise ValueError(
            f"Constraint {label} is a reference (measuring) constraint; it "
            "can't be set. Make it driving first."
        )
    expression = _expression_for(sketch, index)
    if expression is not None:
        raise ValueError(
            f"Constraint {label} is set by the expression {expression!r}; "
            "change what the expression refers to (e.g. the spreadsheet cell) "
            "instead"
        )
    number = _value_in_units(args.get("value"), kind)
    before = _datum(sketch, index, kind)
    datum = math.radians(number) if kind == "angle" else number
    try:
        sketch.setDatum(index, datum)
    except ValueError as error:
        unit = "mm" if kind == "length" else "deg"
        raise ValueError(
            f"Couldn't set constraint {label} to {_round(number)} {unit}: "
            f"{_datum_failure(str(error))} Nothing was "
            "changed."
        ) from None
    state = sketch_state(doc, sketch)
    return {
        "sketch": sketch.Name,
        "constraint": _constraint_summary(sketch, index),
        "unit": "mm" if kind == "length" else "deg",
        "value_before": _round(before),
        "value": _round(_datum(sketch, index, kind)),
        "sketch_state": state,
    }


# ---------------------------------------------------------------------------
# Polylines
# ---------------------------------------------------------------------------


def _clean_points(points: Any) -> list[tuple[float, float]]:
    """Validate polyline points and return them as (x, y) tuples."""
    if not isinstance(points, list | tuple):
        raise ValueError("points must be a list of [x, y] pairs in mm")
    clean: list[tuple[float, float]] = []
    for number, point in enumerate(points):
        if (
            not isinstance(point, list | tuple)
            or len(point) != 2
            or not all(
                isinstance(v, int | float) and not isinstance(v, bool) for v in point
            )
            or not all(math.isfinite(v) for v in point)
        ):
            raise ValueError(f"Point {number} must be [x, y] in mm, got {point!r}")
        clean.append((float(point[0]), float(point[1])))
    return clean


def _axis_aligned(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> tuple[list[int], list[int]]:
    """Return the numbers of near-horizontal and near-vertical segments."""
    tolerance = math.radians(AXIS_TOLERANCE_DEGREES)
    horizontal: list[int] = []
    vertical: list[int] = []
    for number, (start, end) in enumerate(segments):
        angle = math.atan2(abs(end[1] - start[1]), abs(end[0] - start[0]))
        if angle <= tolerance:
            horizontal.append(number)
        elif angle >= math.pi / 2 - tolerance:
            vertical.append(number)
    return horizontal, vertical


def plan_polyline(
    points: Any,
    *,
    closed: bool,
    coincident: bool,
    auto_horizontal_vertical: bool,
) -> dict[str, Any]:
    """Work out the segments and constraints of a polyline (no FreeCAD).

    Args:
        points: List of [x, y] points in mm.
        closed: Join the last point back to the first.
        coincident: Add coincident constraints at the joints.
        auto_horizontal_vertical: Constrain segments within
            AXIS_TOLERANCE_DEGREES of an axis to be horizontal or vertical.

    Returns:
        ``segments`` ([(x0, y0), (x1, y1)] pairs), ``joints`` (pairs of
        segment numbers joined end to start), ``horizontal`` and ``vertical``
        (segment numbers), and ``closed`` (True also when the last point
        repeats the first, which is then dropped).

    Raises:
        ValueError: For malformed points, too few points, or a segment shorter
            than MIN_SEGMENT_LENGTH.
    """
    clean = _clean_points(points)
    if len(clean) > 2 and math.dist(clean[0], clean[-1]) <= MIN_SEGMENT_LENGTH:
        clean.pop()
        closed = True
    if len(clean) < 2:
        raise ValueError("A polyline needs at least 2 points")
    if closed and len(clean) < 3:
        raise ValueError("A closed polyline needs at least 3 distinct points")
    count = len(clean) if closed else len(clean) - 1
    segments = [(clean[i], clean[(i + 1) % len(clean)]) for i in range(count)]
    for number, (start, end) in enumerate(segments):
        if math.dist(start, end) <= MIN_SEGMENT_LENGTH:
            raise ValueError(
                f"Segment {number} from {list(start)} to {list(end)} has no length; "
                "remove the repeated point"
            )
    joints: list[tuple[int, int]] = []
    if coincident:
        joints = [(i, i + 1) for i in range(count - 1)]
        if closed:
            joints.append((count - 1, 0))
    horizontal, vertical = (
        _axis_aligned(segments) if auto_horizontal_vertical else ([], [])
    )
    return {
        "segments": segments,
        "joints": joints,
        "horizontal": horizontal,
        "vertical": vertical,
        "closed": closed,
    }


def add_polyline(args: dict[str, Any]) -> dict[str, Any]:
    """Add connected line segments to a sketch in one step.

    Args:
        args: ``sketch_name``, ``points`` ([[x, y], ...] in mm), optional
            ``closed`` (default True), ``constrain`` ("coincident" or
            "none"), ``auto_horizontal_vertical`` (default True),
            ``construction`` (default False) and ``doc_name``.

    Returns:
        The new geometry indices, the new constraint indices by type, the
        vertex positions after solving, and the sketch state.
    """
    import FreeCAD
    import Part
    import Sketcher

    doc, sketch = _get_sketch(args)
    constrain = args.get("constrain", "coincident")
    if constrain not in ("coincident", "none"):
        raise ValueError("constrain must be 'coincident' or 'none'")
    plan = plan_polyline(
        args.get("points"),
        closed=bool(args.get("closed", True)),
        coincident=constrain == "coincident",
        auto_horizontal_vertical=bool(args.get("auto_horizontal_vertical", True)),
    )
    construction = bool(args.get("construction", False))
    lines = [
        Part.LineSegment(
            FreeCAD.Vector(start[0], start[1], 0), FreeCAD.Vector(end[0], end[1], 0)
        )
        for start, end in plan["segments"]
    ]
    first = len(sketch.Geometry)
    added = sketch.addGeometry(lines, construction)
    indices = list(added) if isinstance(added, list | tuple) else [added]
    if len(indices) != len(lines):
        indices = list(range(first, first + len(lines)))

    groups: dict[str, list[Any]] = {
        "coincident": [
            Sketcher.Constraint("Coincident", indices[a], 2, indices[b], 1)
            for a, b in plan["joints"]
        ],
        "horizontal": [
            Sketcher.Constraint("Horizontal", indices[n]) for n in plan["horizontal"]
        ],
        "vertical": [
            Sketcher.Constraint("Vertical", indices[n]) for n in plan["vertical"]
        ],
    }
    constraint_indices: dict[str, list[int]] = {}
    for key, constraints in groups.items():
        start = len(sketch.Constraints)
        if constraints:
            sketch.addConstraint(constraints)
        constraint_indices[key] = list(range(start, len(sketch.Constraints)))

    state = sketch_state(doc, sketch)
    vertices = [_xy(sketch.getPoint(i, 1)) for i in indices]
    if not plan["closed"]:
        vertices.append(_xy(sketch.getPoint(indices[-1], 2)))
    return {
        "sketch": sketch.Name,
        "geometry_indices": indices,
        "closed": plan["closed"],
        "construction": construction,
        "constraint_indices": constraint_indices,
        "vertices": vertices,
        "sketch_state": state,
    }


OPERATIONS = {
    "sketch_move_point": move_point,
    "sketch_set_radius": set_radius,
    "sketch_set_constraint_value": set_constraint_value,
    "sketch_add_polyline": add_polyline,
}
