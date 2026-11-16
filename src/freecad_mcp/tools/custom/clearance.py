"""Read-only clearance tools: overlap checks, clash sweeps, sphere centres.

The FreeCAD-side code is in the workbench's ``custom_runtime/clearance.py``.
Every tool here works on in-memory copies of shapes, so the document is never
changed and nothing needs cleaning up afterwards.
"""

import math
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import ToolAnnotations

from freecad_mcp.tools.custom.runtime import run_operation

# A single overlap check on detailed parts can take a while.
CHECK_TIMEOUT_MS = 300_000
# Sweeps stop themselves after 540 s inside FreeCAD and report what they found.
SWEEP_TIMEOUT_MS = 600_000

_POSE_KEYS = frozenset({"axis", "angle", "pivot", "offset"})

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _check_vector(value: Any, label: str, *, non_zero: bool = False) -> list[float]:
    """Validate an [x, y, z] list and return it as floats.

    Raises:
        ValueError: If the value isn't three finite numbers (or is zero when
            ``non_zero``).
    """
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError(f"{label} must be [x, y, z]")
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be three numbers") from error
    if not all(math.isfinite(item) for item in numbers):
        raise ValueError(f"{label} must be finite")
    if non_zero and not any(numbers):
        raise ValueError(f"{label} must not be zero")
    return numbers


def _check_pose(pose: dict[str, Any] | None, label: str) -> dict[str, Any] | None:
    """Validate a pose dictionary for check_overlap.

    Raises:
        ValueError: On unknown keys or malformed values.
    """
    if not pose:
        return None
    unknown = set(pose) - _POSE_KEYS
    if unknown:
        raise ValueError(
            f"{label}: unknown keys {sorted(unknown)}; use axis, angle, pivot, offset"
        )
    clean: dict[str, Any] = {}
    if "angle" in pose:
        try:
            clean["angle"] = float(pose["angle"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label}.angle must be a number (degrees)") from error
        if clean["angle"] and "axis" not in pose:
            raise ValueError(f"{label}: give an axis with the angle")
    if "axis" in pose:
        clean["axis"] = _check_vector(pose["axis"], f"{label}.axis", non_zero=True)
    for key in ("pivot", "offset"):
        if key in pose:
            clean[key] = _check_vector(pose[key], f"{label}.{key}")
    return clean


def _check_names(names: list[str], label: str) -> list[str]:
    """Validate a non-empty list of object names.

    Raises:
        ValueError: If the list is empty or has blank names.
    """
    if not names or not all(isinstance(name, str) and name for name in names):
        raise ValueError(f"{label} must list at least one object name")
    return list(names)


def sweep_sample_estimate(
    min_angle: float, max_angle: float, step: float, precision: float
) -> int:
    """Return the most samples a sweep can need (start, coarse and refinement).

    Args:
        min_angle: Negative limit in degrees (0 to skip that direction).
        max_angle: Positive limit in degrees (0 to skip that direction).
        step: Coarse step in degrees.
        precision: Bracket width to refine to, in degrees.

    Returns:
        Upper bound on the number of tested poses.
    """
    total = 1
    refine = max(0, math.ceil(math.log2(step / precision))) if step > precision else 0
    for limit in (max_angle, -min_angle):
        if limit > 0:
            total += math.ceil(limit / step - 1e-9) + refine
    return total


def register_clearance_tools(
    mcp: Any, get_bridge: Callable[[], Awaitable[Any]]
) -> None:
    """Register the clearance tools.

    Args:
        mcp: The MCPServer instance.
        get_bridge: Async function to get the active bridge.
    """

    @mcp.tool(annotations=_READ_ONLY)
    async def check_overlap(
        object_a: str,
        object_b: str,
        elements_a: list[str] | None = None,
        elements_b: list[str] | None = None,
        pose_a: dict[str, Any] | None = None,
        pose_b: dict[str, Any] | None = None,
        volume_threshold: float = 0.001,
        touch_tolerance: float = 0.001,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Check two objects for overlap, contact or a gap. Read-only.

        Status is "overlapping" (shared volume above volume_threshold, mm³;
        gives overlap_volume and overlap_region centre/bound_box),
        "touching" (distance within touch_tolerance, mm), "separate"
        (distance and closest_points) or "failed" (with reason). Zero
        overlap does not prove clearance. The threshold only filters
        numerical noise. Faces/edges (elements_a/b, e.g. ["Face3"]) get a
        distance only. Coordinates are global, in mm.

        pose_a/pose_b test a pose without moving anything: {"axis": [x,y,z],
        "angle": degrees, "pivot": [x,y,z], "offset": [x,y,z]}, applied on
        top of the current placement as offset * rotate-about-pivot.

        Args:
            object_a: First object name or label.
            object_b: Second object name or label.
            elements_a: Sub-elements of A to use instead of the whole shape.
            elements_b: Sub-elements of B.
            pose_a: Extra motion for A (see above).
            pose_b: Extra motion for B.
            volume_threshold: Overlap below this (mm³) counts as none.
            touch_tolerance: Distances up to this (mm) count as touching.
            doc_name: Document. Uses the active document if None.

        Returns:
            Status, overlap or distance results, per-object info, notes.
        """
        if volume_threshold < 0 or touch_tolerance < 0:
            raise ValueError(
                "volume_threshold and touch_tolerance must not be negative"
            )
        args = {
            "object_a": object_a,
            "object_b": object_b,
            "elements_a": elements_a or None,
            "elements_b": elements_b or None,
            "pose_a": _check_pose(pose_a, "pose_a"),
            "pose_b": _check_pose(pose_b, "pose_b"),
            "volume_threshold": volume_threshold,
            "touch_tolerance": touch_tolerance,
            "doc_name": doc_name,
        }
        result: dict[str, Any] = await run_operation(
            get_bridge, "check_overlap", args, timeout_ms=CHECK_TIMEOUT_MS
        )
        return result

    @mcp.tool(annotations=_READ_ONLY)
    async def sweep_clash(
        moving: list[str],
        fixed: list[str],
        pivot: list[float],
        axis: list[float],
        min_angle: float = -90.0,
        max_angle: float = 90.0,
        step: float = 5.0,
        precision: float = 0.5,
        overlap_threshold: float = 0.001,
        allow_start_overlap: bool = False,
        exclude_pairs: list[list[str]] | None = None,
        max_samples: int = 200,
        max_log: int = 40,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Rotate objects about a pivot and find where they first hit others.

        Read-only (shape copies). The moving objects turn together from
        their current pose (angle 0) out to max_angle and to min_angle
        (degrees; right-hand rule about axis; global mm), every `step`, then
        each first clear/clash bracket is refined to `precision`.

        Per direction: last_clear_angle, first_clash_angle, limiting_pair,
        overlap_volume (mm³), contact_region, gap_at_last_clear. Results are
        brackets from stepped tests, not a continuous sweep.

        Intended contact (a ball gripped by its socket): allow_start_overlap
        ignores overlap present at angle 0 and counts only growth above it;
        exclude_pairs ([[moving, fixed], ...]) skips pairs entirely, so a
        stop between them is never found. Otherwise a start overlap stops
        the sweep. Use find_sphere_centres for ball pivots. Can take minutes.

        Args:
            moving: Objects that rotate together.
            fixed: Objects that stay put.
            pivot: Point on the rotation axis [x, y, z], mm.
            axis: Rotation axis direction [x, y, z].
            min_angle: Negative limit, degrees (0 skips that direction).
            max_angle: Positive limit, degrees (0 skips that direction).
            step: Coarse step, degrees.
            precision: Refined bracket width, degrees.
            overlap_threshold: Overlap (mm³) treated as numerical noise.
            allow_start_overlap: Count only overlap growth beyond angle 0.
            exclude_pairs: [moving, fixed] pairs not tested.
            max_samples: Cap on tested poses.
            max_log: Cap on sample_log entries.
            doc_name: Document. Uses the active document if None.

        Returns:
            inputs echoed, start_pose, directions, sample_log, notes.
        """
        moving = _check_names(moving, "moving")
        fixed = _check_names(fixed, "fixed")
        shared = sorted(set(moving) & set(fixed))
        if shared:
            raise ValueError(f"Objects can't be both moving and fixed: {shared}")
        if min_angle > 0 or max_angle < 0 or min_angle == max_angle:
            raise ValueError("Need min_angle <= 0 <= max_angle, not both zero")
        if step <= 0 or precision <= 0:
            raise ValueError("step and precision must be positive (degrees)")
        if overlap_threshold < 0:
            raise ValueError("overlap_threshold must not be negative")
        if max_samples < 1 or max_log < 0:
            raise ValueError("max_samples must be at least 1 and max_log not negative")
        needed = sweep_sample_estimate(min_angle, max_angle, step, precision)
        if needed > max_samples:
            raise ValueError(
                f"This sweep can need {needed} samples, more than max_samples "
                f"({max_samples}). Use a larger step, a smaller range, or raise "
                "max_samples."
            )
        pairs = None
        if exclude_pairs:
            pairs = []
            for pair in exclude_pairs:
                if len(pair) != 2 or pair[0] not in moving or pair[1] not in fixed:
                    raise ValueError(
                        f"exclude_pairs item {pair!r} must be [moving, fixed] "
                        "using names from the moving and fixed lists"
                    )
                pairs.append(list(pair))
        args = {
            "moving": moving,
            "fixed": fixed,
            "pivot": _check_vector(pivot, "pivot"),
            "axis": _check_vector(axis, "axis", non_zero=True),
            "min_angle": min_angle,
            "max_angle": max_angle,
            "step": step,
            "precision": precision,
            "overlap_threshold": overlap_threshold,
            "allow_start_overlap": allow_start_overlap,
            "exclude_pairs": pairs,
            "max_samples": max_samples,
            "max_log": max_log,
            "doc_name": doc_name,
        }
        result: dict[str, Any] = await run_operation(
            get_bridge, "sweep_clash", args, timeout_ms=SWEEP_TIMEOUT_MS
        )
        return result

    @mcp.tool(annotations=_READ_ONLY)
    async def find_sphere_centres(
        object_name: str,
        faces: list[str] | None = None,
        max_results: int = 20,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Find spherical faces on an object: centres and radii. Read-only.

        Use it to get ball-joint pivots instead of guessing from bounding
        boxes. Faces on the same sphere are grouped. Each sphere gives
        centre (global, mm), radius (mm), kind ("convex" = ball, "concave"
        = socket), faces, area (mm²) and coverage (fraction of a full
        sphere). Spline faces that only look spherical are not found.

        Args:
            object_name: Object name or label.
            faces: Only these faces (e.g. ["Face3"]). All faces if None.
            max_results: Cap on spheres returned (largest area first).
            doc_name: Document. Uses the active document if None.

        Returns:
            spheres, truncated flag, sphere_face_count, notes.
        """
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        args = {
            "object_name": object_name,
            "faces": faces or None,
            "max_results": max_results,
            "doc_name": doc_name,
        }
        result: dict[str, Any] = await run_operation(
            get_bridge, "find_sphere_centres", args
        )
        return result
