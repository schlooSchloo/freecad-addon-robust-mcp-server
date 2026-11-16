"""Tools that edit existing sketch geometry with the sketch solver.

Moving a vertex, resizing a circle, changing a dimension or drawing a closed
outline previously meant deleting and re-adding lines and their constraints.
These tools do each in one call. Every call is one undo step, recomputes the
document and reports the sketch's solver state, so the caller can see at once
whether the sketch is still healthy.

The FreeCAD-side code is in the workbench's ``custom_runtime/sketch_editing.py``.
"""

import math
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from mcp.types import ToolAnnotations

from freecad_mcp.tools.custom.runtime import run_operation

_EDIT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)
_ADD = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


def _point_pair(value: Any, name: str) -> list[float]:
    """Validate an [x, y] pair of finite numbers."""
    if (
        not isinstance(value, list | tuple)
        or len(value) != 2
        or not all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in value
        )
        or not all(math.isfinite(v) for v in value)
    ):
        msg = f"{name} must be [x, y] in mm, got {value!r}"
        raise ValueError(msg)
    return [float(value[0]), float(value[1])]


def register_sketch_editing_tools(
    mcp: Any, get_bridge: Callable[[], Awaitable[Any]]
) -> None:
    """Register the sketch editing tools.

    Args:
        mcp: The MCPServer instance.
        get_bridge: Async function returning the active bridge.
    """

    @mcp.tool(annotations=_EDIT)
    async def move_sketch_point(
        sketch_name: str,
        geometry_index: int,
        position: list[float],
        point: Literal["start", "end", "center"] = "start",
        relative: bool = False,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Move a point of existing sketch geometry, keeping its constraints.

        The solver drags the point like the sketcher does: joined geometry
        follows and constraints still hold. A point held by constraints
        doesn't move (or moves part way); the result says so and lists the
        constraints on that geometry. To change a dimension, use
        set_sketch_constraint_value instead.

        Args:
            sketch_name: Sketch name or label.
            geometry_index: 0-based index of the sketch's own geometry.
            position: Target [x, y] in mm, sketch coordinates; an offset
                [dx, dy] when relative is true.
            point: "start", "end" or "center" (circles and arcs).
            relative: Treat position as an offset from the current point.
            doc_name: Document name; defaults to the active document.

        Returns:
            before, requested and after [x, y]; moved; reached;
            distance_from_requested (mm); a message and constraints_on_geometry
            when not reached; and sketch_state (dof, fully_constrained,
            solver_status, conflicting and redundant constraints, closed
            wires).
        """
        target = _point_pair(position, "position")
        return await run_operation(
            get_bridge,
            "sketch_move_point",
            {
                "sketch_name": sketch_name,
                "geometry_index": geometry_index,
                "position": target,
                "point": point,
                "relative": relative,
                "doc_name": doc_name,
            },
            transaction=f"Move {sketch_name} point {geometry_index}.{point}",
            doc_name=doc_name,
        )

    @mcp.tool(annotations=_EDIT)
    async def set_sketch_radius(
        sketch_name: str,
        geometry_index: int,
        radius: float,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Change a circle or arc radius that no constraint sets.

        Solves with the new radius so tangent, coincident or equal geometry
        follows. If a radius or diameter constraint sets the size, this
        refuses and names it: change that constraint with
        set_sketch_constraint_value instead.

        Args:
            sketch_name: Sketch name or label.
            geometry_index: 0-based index of a circle or arc of a circle.
            radius: New radius in mm (above 0).
            doc_name: Document name; defaults to the active document.

        Returns:
            radius_before, radius, center_before, center (mm) and sketch_state
            (dof, fully_constrained, solver_status, conflicting and redundant
            constraints, closed wires).
        """
        if not math.isfinite(radius) or radius <= 0:
            msg = "radius must be a number above 0 (mm)"
            raise ValueError(msg)
        return await run_operation(
            get_bridge,
            "sketch_set_radius",
            {
                "sketch_name": sketch_name,
                "geometry_index": geometry_index,
                "radius": float(radius),
                "doc_name": doc_name,
            },
            transaction=f"Set {sketch_name} radius {geometry_index}",
            doc_name=doc_name,
        )

    @mcp.tool(annotations=_EDIT)
    async def set_sketch_constraint_value(
        sketch_name: str,
        constraint: int | str,
        value: float | str,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Change the value of a dimensional constraint and re-solve the sketch.

        Works on Distance, DistanceX, DistanceY, Radius, Diameter and Angle
        constraints. If the solver can't satisfy the new value, nothing
        changes and the error lists conflicting constraints. Constraints
        driven by an expression (e.g. a spreadsheet cell) are refused: change
        the cell instead.

        Args:
            sketch_name: Sketch name or label.
            constraint: 0-based constraint index, or the constraint's name.
            value: A number (mm for lengths, degrees for angles) or a string
                with units, e.g. "12.5 mm", "0.5 in", "30 deg".
            doc_name: Document name; defaults to the active document.

        Returns:
            constraint (index, type, name), unit, value_before, value and
            sketch_state (dof, fully_constrained, solver_status, conflicting and
            redundant constraints, closed wires).
        """
        if isinstance(value, float) and not math.isfinite(value):
            msg = "value must be finite"
            raise ValueError(msg)
        if isinstance(constraint, str) and not constraint.strip():
            msg = "constraint must be a 0-based index or a constraint name"
            raise ValueError(msg)
        return await run_operation(
            get_bridge,
            "sketch_set_constraint_value",
            {
                "sketch_name": sketch_name,
                "constraint": constraint,
                "value": value,
                "doc_name": doc_name,
            },
            transaction=f"Set {sketch_name} constraint {constraint}",
            doc_name=doc_name,
        )

    @mcp.tool(annotations=_ADD)
    async def add_sketch_polyline(
        sketch_name: str,
        points: list[list[float]],
        closed: bool = True,
        constrain: Literal["coincident", "none"] = "coincident",
        auto_horizontal_vertical: bool = True,
        construction: bool = False,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Add connected line segments to a sketch in one call.

        Joints (and the closing joint) get coincident constraints, and
        segments within 0.5 degrees of an axis can be made horizontal or
        vertical. A last point equal to the first closes the outline.

        Args:
            sketch_name: Sketch name or label.
            points: Vertices as [[x, y], ...] in mm, sketch coordinates.
            closed: Join the last point back to the first.
            constrain: "coincident" joins the segments; "none" adds them loose.
            auto_horizontal_vertical: Constrain near-axis segments.
            construction: Add as construction geometry.
            doc_name: Document name; defaults to the active document.

        Returns:
            geometry_indices, closed, constraint_indices (coincident,
            horizontal, vertical), vertices after solving and
            sketch_state (dof, fully_constrained, solver_status, conflicting and
            redundant constraints, closed wires).
        """
        if not isinstance(points, list) or len(points) < 2:
            msg = "points must list at least 2 [x, y] pairs in mm"
            raise ValueError(msg)
        clean = [_point_pair(p, f"Point {i}") for i, p in enumerate(points)]
        return await run_operation(
            get_bridge,
            "sketch_add_polyline",
            {
                "sketch_name": sketch_name,
                "points": clean,
                "closed": closed,
                "constrain": constrain,
                "auto_horizontal_vertical": auto_horizontal_vertical,
                "construction": construction,
                "doc_name": doc_name,
            },
            transaction=f"Add polyline to {sketch_name}",
            doc_name=doc_name,
        )


__all__ = ["register_sketch_editing_tools"]
