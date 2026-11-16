"""Read-only clearance checks: overlap, clash sweeps and sphere centres.

Everything here works on in-memory copies of shapes, moved with
``Shape.transformed`` (a location change, not a geometry copy). No document
object is added, changed or recomputed.

Poses follow ``pose = T(offset) * T(pivot) * R(axis, angle) * T(-pivot) * P0``,
where ``P0`` is the object's current global placement. Each pose is built from
the starting shape, never by adding rotations, so errors don't build up.
"""

from __future__ import annotations

import math
import time
from typing import Any

# A sweep stops itself after this long and returns what it found, so the
# bridge call (given a 600 s timeout by the server) doesn't time out first.
SWEEP_TIME_LIMIT_S = 540.0

# The bisection that refines a bracket is capped so a bad precision value
# can't loop for long.
_MAX_REFINE_STEPS = 30

NOTE_ZERO_OVERLAP = (
    "Zero overlap does not prove clearance: touching shapes also have zero "
    "overlap. Use the distance for the gap."
)
NOTE_THRESHOLD = (
    "The volume threshold only filters numerical noise; it is not a print "
    "clearance allowance. A contact counts only once its overlap exceeds it, "
    "so a first clash can lag the first touch slightly."
)
NOTE_STEPPED = (
    "Angles were tested at discrete steps, not as a continuous sweep: a "
    "feature thinner than the coarse step can be passed over. Limits are "
    "brackets (last clear, first clash), not exact mechanical limits."
)
NOTE_COMBINED = (
    "Limits about one axis do not define a safe region for combined "
    "rotations; test combined poses separately."
)


# --- small helpers -------------------------------------------------------


def _r(value: float, digits: int = 6) -> float:
    """Round a float for output, turning -0.0 into 0.0."""
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def _vec_out(vector: Any) -> list[float]:
    """Return a FreeCAD vector as a rounded [x, y, z] list."""
    return [_r(vector.x), _r(vector.y), _r(vector.z)]


def _bbox_out(box: Any) -> dict[str, list[float]]:
    """Return a bounding box as rounded min and max corners."""
    return {
        "min": [_r(box.XMin), _r(box.YMin), _r(box.ZMin)],
        "max": [_r(box.XMax), _r(box.YMax), _r(box.ZMax)],
    }


def _vector(value: Any, label: str) -> Any:
    """Convert an [x, y, z] list to a FreeCAD vector.

    Raises:
        ValueError: If the value is not three numbers.
    """
    import FreeCAD

    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError(f"{label} must be [x, y, z]")
    try:
        return FreeCAD.Vector(*(float(item) for item in value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be three numbers") from error


def _document(args: dict[str, Any]) -> Any:
    """Return the named document, or the active one.

    Raises:
        ValueError: If there is no such document.
    """
    import FreeCAD

    name = args.get("doc_name")
    if name:
        try:
            return FreeCAD.getDocument(name)
        except NameError as error:
            raise ValueError(f"Document not found: {name}") from error
    if FreeCAD.ActiveDocument is None:
        raise ValueError("No active document")
    return FreeCAD.ActiveDocument


def _object(doc: Any, name: Any) -> Any:
    """Return a document object by name or label.

    Raises:
        ValueError: If the object doesn't exist.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("Object names must be non-empty strings")
    obj = doc.getObject(name)
    if obj is None:
        matches = doc.getObjectsByLabel(name)
        if len(matches) == 1:
            obj = matches[0]
    if obj is None:
        raise ValueError(f"Object not found in {doc.Name}: {name}")
    return obj


def _global_shape(obj: Any) -> Any:
    """Return the object's shape in global (document) coordinates.

    ``Part.getShape`` gives the shape with the object's own placement (and,
    for an App::Link, the link's). The placements of containers (a Body, an
    App::Part) are applied on top, the same way FreeCAD shows the object.

    Raises:
        ValueError: If the object has no usable shape.
    """
    import Part

    try:
        shape = Part.getShape(obj)
    except Exception as error:  # FreeCAD raises several types here
        raise ValueError(f"Could not read the shape of {obj.Name}: {error}") from error
    if shape is None or shape.isNull():
        raise ValueError(f"Object {obj.Name} has no shape")
    extra = _container_placement(obj)
    if extra is not None and not extra.isIdentity():
        shape = shape.transformed(extra.toMatrix())
    return shape


def _container_placement(obj: Any) -> Any:
    """Return the combined placement of the containers holding an object.

    Walks the parent Body / App::Part chain (as ``getGlobalPlacement`` does,
    which App::Link objects lack). Returns None for a top-level object.
    """
    if not hasattr(obj, "getParentGeoFeatureGroup"):
        return None
    total = None
    parent = obj.getParentGeoFeatureGroup()
    for _depth in range(64):  # containers never nest this deep; stops cycles
        if parent is None:
            break
        if hasattr(parent, "Placement"):
            total = (
                parent.Placement if total is None else parent.Placement.multiply(total)
            )
        parent = parent.getParentGeoFeatureGroup()
    return total


def _select_elements(shape: Any, elements: Any, label: str) -> Any:
    """Return the named sub-elements of a shape (one, or a compound).

    Raises:
        ValueError: If a name is malformed or doesn't exist.
    """
    import Part

    if not elements:
        return shape
    if isinstance(elements, str):
        elements = [elements]
    picked = []
    for name in elements:
        if not isinstance(name, str) or not name[:1].isalpha():
            raise ValueError(f"{label}: element names look like 'Face3' or 'Edge2'")
        try:
            picked.append(shape.getElement(name))
        except Exception as error:
            raise ValueError(f"{label}: no element {name}") from error
    return picked[0] if len(picked) == 1 else Part.makeCompound(picked)


def _pose_placement(pose: Any, label: str) -> Any:
    """Build the motion placement for a pose dictionary.

    Keys (all optional): axis [x, y, z], angle (degrees), pivot [x, y, z],
    offset [x, y, z]. Result: T(offset) * T(pivot) * R * T(-pivot).

    Raises:
        ValueError: If the pose is malformed.
    """
    import FreeCAD

    if pose is None:
        return None
    if not isinstance(pose, dict):
        raise ValueError(f"{label} must be an object with axis, angle, pivot, offset")
    unknown = set(pose) - {"axis", "angle", "pivot", "offset"}
    if unknown:
        raise ValueError(f"{label}: unknown keys {sorted(unknown)}")
    angle = float(pose.get("angle", 0.0) or 0.0)
    axis = _vector(pose.get("axis", [0, 0, 1]), f"{label}.axis")
    if axis.Length < 1e-12:
        raise ValueError(f"{label}.axis must not be zero")
    pivot = _vector(pose.get("pivot", [0, 0, 0]), f"{label}.pivot")
    offset = _vector(pose.get("offset", [0, 0, 0]), f"{label}.offset")
    return FreeCAD.Placement(offset, FreeCAD.Rotation(axis, angle), pivot)


def _solids(shape: Any) -> Any:
    """Return the solids of a shape as one shape, or None if it has none."""
    import Part

    solids = shape.Solids
    if not solids:
        return None
    return solids[0] if len(solids) == 1 else Part.makeCompound(solids)


def _volume(shape: Any) -> float:
    """Return the total volume of a shape's solids (0 if none)."""
    return float(sum(solid.Volume for solid in shape.Solids))


def _centre(shape: Any) -> Any:
    """Return the volume-weighted centre of a shape's solids."""
    import FreeCAD

    total = 0.0
    weighted = FreeCAD.Vector(0, 0, 0)
    for solid in shape.Solids:
        volume = solid.Volume
        weighted += solid.CenterOfMass * volume
        total += volume
    if total <= 0:
        return shape.BoundBox.Center
    return weighted * (1.0 / total)


def _region(shape: Any) -> dict[str, Any]:
    """Describe an overlap region: centre and bounding box."""
    return {"centre": _vec_out(_centre(shape)), "bound_box": _bbox_out(shape.BoundBox)}


def _boxes_meet(a: Any, b: Any, margin: float) -> bool:
    """Return True if two bounding boxes, enlarged by margin, intersect."""
    box = a.BoundBox
    box.enlarge(margin)
    return bool(box.intersect(b.BoundBox))


def _common(a: Any, b: Any) -> tuple[float, Any]:
    """Return the overlap volume and the common shape of two solids.

    Raises:
        RuntimeError: If the boolean fails or gives an impossible volume.
    """
    try:
        common = a.common(b)
    except Exception as error:
        raise RuntimeError(f"boolean common failed: {error}") from error
    if common.isNull():
        return 0.0, None
    volume = _volume(common)
    limit = min(_volume(a), _volume(b))
    if volume < -1e-9 or volume > limit * (1 + 1e-6) + 1e-6:
        raise RuntimeError(
            f"boolean common gave an impossible volume ({volume:.6g} mm³)"
        )
    return volume, common


def _distance(a: Any, b: Any) -> tuple[float, Any, Any, int]:
    """Return the minimum distance, a closest point pair and the pair count.

    Raises:
        RuntimeError: If the distance computation fails.
    """
    try:
        distance, pairs, _info = a.distToShape(b)
    except Exception as error:
        raise RuntimeError(f"distance computation failed: {error}") from error
    first = pairs[0] if pairs else (None, None)
    return float(distance), first[0], first[1], len(pairs)


# --- check_overlap ---------------------------------------------------------


def _prepare(doc: Any, args: dict[str, Any], side: str) -> tuple[Any, dict[str, Any]]:
    """Resolve one side of check_overlap: object, pose and elements."""
    obj = _object(doc, args.get(f"object_{side}"))
    shape = _global_shape(obj)
    motion = _pose_placement(args.get(f"pose_{side}"), f"pose_{side}")
    if motion is not None:
        shape = shape.transformed(motion.toMatrix())
    shape = _select_elements(shape, args.get(f"elements_{side}"), f"elements_{side}")
    info: dict[str, Any] = {"object": obj.Name, "valid": bool(shape.isValid())}
    if args.get(f"elements_{side}"):
        info["elements"] = args[f"elements_{side}"]
    if args.get(f"pose_{side}"):
        info["pose"] = args[f"pose_{side}"]
    solids = _solids(shape)
    info["volume"] = _r(_volume(shape)) if solids is not None else None
    return (solids if solids is not None else shape), info


def check_overlap(args: dict[str, Any]) -> dict[str, Any]:
    """Check two objects for overlap, contact or a gap. Read-only.

    Args:
        args: object_a, object_b; optional elements_a/elements_b (lists of
            names such as "Face3"), pose_a/pose_b (axis, angle, pivot,
            offset), volume_threshold (mm³), touch_tolerance (mm), doc_name.

    Returns:
        Status (overlapping, touching, separate or failed), overlap volume and
        region, or distance and closest points, plus notes.
    """
    doc = _document(args)
    threshold = float(args.get("volume_threshold", 0.001))
    tolerance = float(args.get("touch_tolerance", 0.001))
    if threshold < 0 or tolerance < 0:
        raise ValueError("volume_threshold and touch_tolerance must not be negative")
    shape_a, info_a = _prepare(doc, args, "a")
    shape_b, info_b = _prepare(doc, args, "b")
    result: dict[str, Any] = {
        "status": None,
        "a": info_a,
        "b": info_b,
        "volume_threshold": threshold,
        "touch_tolerance": tolerance,
        "notes": [NOTE_ZERO_OVERLAP, NOTE_THRESHOLD],
        "warnings": [],
    }
    for side, info in (("A", info_a), ("B", info_b)):
        if not info["valid"]:
            result["warnings"].append(
                f"Shape {side} ({info['object']}) is not valid; results may be wrong."
            )
    solid_inputs = info_a["volume"] is not None and info_b["volume"] is not None
    try:
        if solid_inputs:
            volume = 0.0
            common = None
            if _boxes_meet(shape_a, shape_b, tolerance):
                volume, common = _common(shape_a, shape_b)
            result["overlap_volume"] = _r(volume)
            if volume > threshold and common is not None:
                result["status"] = "overlapping"
                result["overlap_region"] = _region(common)
                return result
        else:
            result["overlap_volume"] = None
            result["overlap_volume_reason"] = (
                "Not applicable: at least one input has no solids, so only the "
                "distance is measured (0 means the elements touch or cross)."
            )
        distance, on_a, on_b, count = _distance(shape_a, shape_b)
    except RuntimeError as error:
        result["status"] = "failed"
        result["reason"] = str(error)
        return result
    result["distance"] = _r(distance)
    if on_a is not None:
        result["closest_points"] = {"on_a": _vec_out(on_a), "on_b": _vec_out(on_b)}
        result["closest_pair_count"] = count
    result["status"] = "touching" if distance <= tolerance else "separate"
    if solid_inputs and result["overlap_volume"] > 0:
        result["notes"].append(
            "A small overlap below the volume threshold was found and treated "
            "as numerical noise."
        )
    return result


# --- sweep_clash -----------------------------------------------------------


class _Sweep:
    """State and sample evaluation for one clash sweep."""

    def __init__(self, args: dict[str, Any]) -> None:
        import FreeCAD

        doc = _document(args)
        moving = args.get("moving") or []
        fixed = args.get("fixed") or []
        if not moving or not fixed:
            raise ValueError("Give at least one moving and one fixed object")
        self.moving: dict[str, Any] = {}
        self.fixed: dict[str, Any] = {}
        for names, target in ((moving, self.moving), (fixed, self.fixed)):
            for name in names:
                obj = _object(doc, name)
                target[obj.Name] = self._solid(obj)
        shared = set(self.moving) & set(self.fixed)
        if shared:
            raise ValueError(
                f"Objects can't be both moving and fixed: {sorted(shared)}"
            )
        self.pivot = _vector(args.get("pivot"), "pivot")
        axis = _vector(args.get("axis"), "axis")
        if axis.Length < 1e-12:
            raise ValueError("axis must not be zero")
        self.axis = axis.normalize()
        self.threshold = float(args.get("overlap_threshold", 0.001))
        self.allow_start = bool(args.get("allow_start_overlap", False))
        self.excluded: set[tuple[str, str]] = set()
        for pair in args.get("exclude_pairs") or []:
            if not isinstance(pair, list | tuple) or len(pair) != 2:
                raise ValueError("exclude_pairs items must be [moving, fixed]")
            m_name = _object(doc, pair[0]).Name
            f_name = _object(doc, pair[1]).Name
            if m_name not in self.moving or f_name not in self.fixed:
                raise ValueError(
                    f"exclude_pairs item {list(pair)} must name a moving then a "
                    "fixed object"
                )
            self.excluded.add((m_name, f_name))
        self.pairs = [
            (m, f)
            for m in self.moving
            for f in self.fixed
            if (m, f) not in self.excluded
        ]
        if not self.pairs:
            raise ValueError("Every moving/fixed pair is excluded; nothing to test")
        self.baseline: dict[tuple[str, str], float] = {}
        self.baseline_common: dict[tuple[str, str], Any] = {}
        self.max_samples = int(args.get("max_samples", 200))
        self.samples = 0
        self.log: list[dict[str, Any]] = []
        # time_limit_s is for tests; the server never sends it.
        limit = min(
            float(args.get("time_limit_s", SWEEP_TIME_LIMIT_S)), SWEEP_TIME_LIMIT_S
        )
        self.deadline = time.monotonic() + limit
        self._rotation_type = FreeCAD.Rotation
        self._placement_type = FreeCAD.Placement
        self._vector_type = FreeCAD.Vector

    @staticmethod
    def _solid(obj: Any) -> Any:
        """Return an object's solids in global coordinates."""
        shape = _solids(_global_shape(obj))
        if shape is None:
            raise ValueError(f"Object {obj.Name} has no solids to test")
        return shape

    def motion(self, angle: float) -> Any:
        """Return the motion placement T(c) * R(angle) * T(-c)."""
        return self._placement_type(
            self._vector_type(0, 0, 0),
            self._rotation_type(self.axis, angle),
            self.pivot,
        )

    def posed(self, angle: float) -> dict[str, Any]:
        """Return the moving shapes at an angle (copies; no document change)."""
        if angle == 0:
            return dict(self.moving)
        matrix = self.motion(angle).toMatrix()
        return {name: shape.transformed(matrix) for name, shape in self.moving.items()}

    def evaluate(self, angle: float) -> dict[str, Any]:
        """Test one angle against every pair.

        Returns:
            Dictionary with clash flag, the largest overlap growth and its
            pair, and per pair: (volume, growth, common shape).

        Raises:
            RuntimeError: If a boolean fails.
        """
        self.samples += 1
        posed = self.posed(angle)
        worst: tuple[float, tuple[str, str] | None] = (0.0, None)
        overlaps: dict[tuple[str, str], tuple[float, float, Any]] = {}
        for pair in self.pairs:
            moving_shape = posed[pair[0]]
            fixed_shape = self.fixed[pair[1]]
            volume, common = 0.0, None
            if _boxes_meet(moving_shape, fixed_shape, 1e-6):
                try:
                    volume, common = _common(moving_shape, fixed_shape)
                except RuntimeError as error:
                    raise RuntimeError(
                        f"{pair[0]} vs {pair[1]} at {angle:g}°: {error}"
                    ) from error
            growth = volume - self.baseline.get(pair, 0.0)
            overlaps[pair] = (volume, growth, common)
            if growth > worst[0]:
                worst = (growth, pair)
        clash = worst[0] > self.threshold
        entry: dict[str, Any] = {"angle": _r(angle), "clash": clash}
        if worst[1] is not None:
            entry["overlap"] = _r(worst[0])
            entry["pair"] = list(worst[1])
        self.log.append(entry)
        return {"clash": clash, "worst": worst, "overlaps": overlaps}

    def out_of_budget(self) -> str | None:
        """Return why the sweep must stop, or None."""
        if self.samples >= self.max_samples:
            return "sample_limit"
        if time.monotonic() > self.deadline:
            return "time_limit"
        return None

    def gap(self, angle: float, pair: tuple[str, str] | None) -> dict[str, Any]:
        """Measure the minimum gap at an angle (one pair, or all pairs)."""
        posed = self.posed(angle)
        candidates = [pair] if pair else list(self.pairs)
        best: dict[str, Any] | None = None
        skipped = []
        for item in candidates:
            if self.baseline.get(item, 0.0) > self.threshold:
                skipped.append(list(item))
                continue
            distance, on_m, on_f, _count = _distance(
                posed[item[0]], self.fixed[item[1]]
            )
            if best is None or distance < best["distance"]:
                best = {
                    "distance": _r(distance),
                    "pair": list(item),
                    "on_moving": _vec_out(on_m) if on_m is not None else None,
                    "on_fixed": _vec_out(on_f) if on_f is not None else None,
                }
        if best is None:
            return {
                "distance": None,
                "reason": "Pairs overlapping at the start (allowed) have no "
                f"meaningful gap: {skipped}",
            }
        return best

    def region(self, pair: tuple[str, str], common: Any) -> dict[str, Any]:
        """Describe the new contact region for a pair at the first clash.

        With an allowed start overlap, the overlap already present at the
        start is cut away so the region shows only the new contact.
        """
        base = self.baseline_common.get(pair)
        if base is not None and self.baseline.get(pair, 0.0) > 0:
            try:
                extra = common.cut(base)
                if _volume(extra) > 0:
                    out = _region(extra)
                    out["excludes_start_overlap"] = True
                    return out
            except Exception:  # fall back to the whole overlap below
                pass
            out = _region(common)
            out["excludes_start_overlap"] = False
            return out
        return _region(common)


def _coarse(
    sweep: _Sweep, sign: int, limit: float, step: float
) -> tuple[float, float | None, dict[str, Any], str | None]:
    """Step from 0 towards ``limit`` until the first clash.

    Returns:
        Last clear angle, first clash angle (None if none), the clash sample,
        and why the walk stopped early (None if it didn't).
    """
    clear = 0.0
    k = 0
    while abs(clear) < abs(limit):
        k += 1
        angle = sign * min(k * step, abs(limit))
        stop = sweep.out_of_budget()
        if stop:
            return clear, None, {}, stop
        result = sweep.evaluate(angle)
        if result["clash"]:
            return clear, angle, result, None
        clear = angle
    return clear, None, {}, None


def _refine(
    sweep: _Sweep, clear: float, clash: float, found: dict[str, Any], precision: float
) -> tuple[float, float, dict[str, Any], str | None]:
    """Bisect a clear/clash bracket down to ``precision`` degrees.

    Assumes one clear-to-clash change inside the bracket.
    """
    for _ in range(_MAX_REFINE_STEPS):
        if abs(clash - clear) <= precision + 1e-12:
            break
        stop = sweep.out_of_budget()
        if stop:
            return clear, clash, found, stop
        middle = (clear + clash) / 2
        result = sweep.evaluate(middle)
        if result["clash"]:
            clash, found = middle, result
        else:
            clear = middle
    return clear, clash, found, None


def _sweep_direction(
    sweep: _Sweep, sign: int, limit: float, args: dict[str, Any]
) -> dict[str, Any]:
    """Sweep from 0 towards ``limit`` and refine the first clash bracket."""
    report: dict[str, Any] = {
        "direction": "positive" if sign > 0 else "negative",
        "limit_angle": _r(limit),
    }
    clear, clash, found, stop = _coarse(
        sweep, sign, limit, float(args.get("step", 5.0))
    )
    if stop:
        report.update(status=stop, last_clear_angle=_r(clear))
        return report
    if clash is None:
        report.update(status="clear_to_limit", last_clear_angle=_r(clear))
        report["gap_at_last_clear"] = sweep.gap(clear, None)
        return report
    clear, clash, found, stop = _refine(
        sweep, clear, clash, found, float(args.get("precision", 0.5))
    )
    if stop:
        report["refinement_stopped"] = stop
    growth, pair = found["worst"]
    volume, _growth, common = found["overlaps"][pair]
    report.update(
        status="clash",
        last_clear_angle=_r(clear),
        first_clash_angle=_r(clash),
        bracket_width=_r(abs(clash - clear)),
        limiting_pair={"moving": pair[0], "fixed": pair[1]},
        overlap_volume=_r(growth),
    )
    if sweep.baseline.get(pair, 0.0) > 0:
        report["overlap_volume_total"] = _r(volume)
    others = [
        {"moving": p[0], "fixed": p[1], "overlap_volume": _r(g)}
        for p, (_v, g, _c) in found["overlaps"].items()
        if p != pair and g > sweep.threshold
    ]
    if others:
        report["other_clashing_pairs"] = others
    if common is not None:
        report["contact_region"] = sweep.region(pair, common)
    report["gap_at_last_clear"] = sweep.gap(clear, pair)
    return report


def sweep_clash(args: dict[str, Any]) -> dict[str, Any]:
    """Rotate moving objects about a pivot and find where they hit fixed ones.

    Read-only: works on in-memory shape copies.

    Args:
        args: moving, fixed (object names), pivot, axis, min_angle, max_angle,
            step, precision (degrees), overlap_threshold (mm³),
            allow_start_overlap, exclude_pairs, max_samples, max_log,
            doc_name.

    Returns:
        Start-pose overlaps, a report per direction, the inputs echoed, a
        capped sample log and notes.
    """
    started = time.monotonic()
    sweep = _Sweep(args)
    min_angle = float(args.get("min_angle", -90.0))
    max_angle = float(args.get("max_angle", 90.0))
    step = float(args.get("step", 5.0))
    precision = float(args.get("precision", 0.5))
    if min_angle > 0 or max_angle < 0 or min_angle == max_angle:
        raise ValueError("Need min_angle <= 0 <= max_angle, not both zero")
    if step <= 0 or precision <= 0:
        raise ValueError("step and precision must be positive")
    max_log = int(args.get("max_log", 40))
    inputs = {
        "moving": list(sweep.moving),
        "fixed": list(sweep.fixed),
        "pivot": _vec_out(sweep.pivot),
        "axis": _vec_out(sweep.axis),
        "frame": "global document coordinates (mm); angles in degrees, "
        "positive = right-hand rotation about the axis",
        "pose": "T(pivot) * R(axis, angle) * T(-pivot) * current placement",
        "min_angle": min_angle,
        "max_angle": max_angle,
        "step": step,
        "precision": precision,
        "overlap_threshold": sweep.threshold,
        "allow_start_overlap": sweep.allow_start,
        "exclude_pairs": [list(pair) for pair in sorted(sweep.excluded)],
    }
    result: dict[str, Any] = {"inputs": inputs}
    try:
        start = sweep.evaluate(0.0)
    except RuntimeError as error:
        result.update(status="failed", reason=str(error))
        return result
    start_overlaps = [
        {"moving": p[0], "fixed": p[1], "overlap_volume": _r(v)}
        for p, (v, _g, _c) in start["overlaps"].items()
        if v > 0
    ]
    result["start_pose"] = {
        "clashes": start["clash"],
        "overlaps": start_overlaps,
        "overlap_allowed": sweep.allow_start,
    }
    if start["clash"] and not sweep.allow_start:
        result["status"] = "start_pose_overlaps"
        result["reason"] = (
            "The moving objects already overlap the fixed ones at the start "
            "pose. If that overlap is intended (e.g. a ball gripped by its "
            "socket), set allow_start_overlap or list the pair in exclude_pairs."
        )
        result["notes"] = [NOTE_THRESHOLD]
        return result
    if sweep.allow_start:
        for pair, (volume, _g, common) in start["overlaps"].items():
            sweep.baseline[pair] = volume
            if volume > 0:
                sweep.baseline_common[pair] = common
    directions = []
    for sign, limit in ((1, max_angle), (-1, min_angle)):
        if limit == 0:
            continue
        try:
            directions.append(_sweep_direction(sweep, sign, limit, args))
        except RuntimeError as error:
            directions.append(
                {
                    "direction": "positive" if sign > 0 else "negative",
                    "status": "failed",
                    "reason": str(error),
                }
            )
    result["status"] = "done"
    result["directions"] = directions
    result["sample_count"] = sweep.samples
    result["sample_log"] = sweep.log[:max_log]
    result["log_truncated"] = len(sweep.log) > max_log
    result["elapsed_s"] = _r(time.monotonic() - started, 2)
    notes = [NOTE_STEPPED, NOTE_ZERO_OVERLAP, NOTE_THRESHOLD, NOTE_COMBINED]
    if sweep.allow_start:
        notes.append(
            "allow_start_overlap: overlap present at the start pose was "
            "subtracted per pair; only growth above it counts as a clash. If "
            "an allowed overlap shrinks while a new contact grows on the same "
            "pair, the new contact can be under-reported."
        )
    if sweep.excluded:
        notes.append(
            "Excluded pairs were not tested at all, so a real stop between "
            "them would not be found."
        )
    result["notes"] = notes
    return result


# --- find_sphere_centres ---------------------------------------------------


def find_sphere_centres(args: dict[str, Any]) -> dict[str, Any]:
    """List the spherical faces of an object with centres and radii.

    Args:
        args: object_name; optional faces (names such as "Face3"),
            max_results, doc_name.

    Returns:
        Spheres grouped by centre and radius, in global coordinates, with
        their faces, total area, coverage of the full sphere and whether they
        are convex (a ball) or concave (a socket).
    """
    import Part

    doc = _document(args)
    obj = _object(doc, args.get("object_name"))
    shape = _global_shape(obj)
    names = args.get("faces")
    if names:
        faces = []
        for name in names:
            if not isinstance(name, str) or not name.startswith("Face"):
                raise ValueError(f"faces must be face names such as 'Face3': {name!r}")
            try:
                faces.append((name, shape.getElement(name)))
            except Exception as error:
                raise ValueError(f"No face {name} on {obj.Name}") from error
    else:
        faces = [(f"Face{index + 1}", face) for index, face in enumerate(shape.Faces)]
    max_results = int(args.get("max_results", 20))
    groups: list[dict[str, Any]] = []
    not_spherical = []
    for name, face in faces:
        surface = face.Surface
        if not isinstance(surface, Part.Sphere):
            not_spherical.append(name)
            continue
        centre, radius = surface.Center, float(surface.Radius)
        u0, u1, v0, v1 = face.ParameterRange
        point = face.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
        normal = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        kind = "convex" if normal.dot(point - centre) > 0 else "concave"
        tolerance = 1e-6 * max(1.0, radius)
        for group in groups:
            if (
                abs(group["_radius"] - radius) <= tolerance
                and (group["_centre"] - centre).Length <= tolerance
            ):
                break
        else:
            group = {"_centre": centre, "_radius": radius, "faces": [], "kinds": set()}
            group["_area"] = 0.0
            groups.append(group)
        group["faces"].append(name)
        group["kinds"].add(kind)
        group["_area"] += float(face.Area)
    spheres = []
    for group in groups:
        kinds = group["kinds"]
        kind = kinds.pop() if len(kinds) == 1 else "mixed"
        full = 4 * math.pi * group["_radius"] ** 2
        spheres.append(
            {
                "centre": _vec_out(group["_centre"]),
                "radius": _r(group["_radius"]),
                "kind": kind,
                "faces": group["faces"],
                "area": _r(group["_area"], 4),
                "coverage": _r(group["_area"] / full, 4),
            }
        )
    spheres.sort(key=lambda item: -item["area"])
    result: dict[str, Any] = {
        "object": obj.Name,
        "frame": "global document coordinates (mm)",
        "spheres": spheres[:max_results],
        "truncated": len(spheres) > max_results,
        "sphere_face_count": sum(len(item["faces"]) for item in spheres),
        "notes": [
            "convex = material inside the sphere (a ball); concave = material "
            "outside (a socket). Spline faces that only look spherical are not "
            "detected."
        ],
    }
    if names and not_spherical:
        result["not_spherical"] = not_spherical
    return result


OPERATIONS = {
    "check_overlap": check_overlap,
    "sweep_clash": sweep_clash,
    "find_sphere_centres": find_sphere_centres,
}
