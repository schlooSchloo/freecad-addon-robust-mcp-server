"""Read-only geometry tools: shape geometry and cross-section outlines.

These replace the need for Python (or spreadsheet formulas) to read
geometry. The FreeCAD-side code is in the workbench's
``custom_runtime/geometry.py``; it works on in-memory shape copies and never
adds, changes or recomputes document objects.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from mcp.types import ToolAnnotations

from freecad_mcp.tools.custom.runtime import run_operation

Section = Literal["summary", "vertices", "edges", "faces"]

_ELEMENT = re.compile(r"^(Vertex|Edge|Face)[1-9][0-9]*$")
_SECTIONS = ("summary", "vertices", "edges", "faces")

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _check_samples(samples: int | None, sample_tolerance: float | None) -> None:
    """Validate the sample-point options shared by both tools.

    Raises:
        ValueError: If both are given or either is out of range.
    """
    if samples is not None and sample_tolerance is not None:
        raise ValueError("Give samples or sample_tolerance, not both")
    if samples is not None and not 2 <= samples <= 500:
        raise ValueError("samples must be between 2 and 500")
    if sample_tolerance is not None and not sample_tolerance > 0:
        raise ValueError("sample_tolerance must be greater than 0 mm")


def _check_vector(value: list[float] | None, name: str) -> None:
    """Validate an [x, y, z] argument.

    Raises:
        ValueError: If it does not have three numbers.
    """
    if value is not None and len(value) != 3:
        raise ValueError(f"{name} must be [x, y, z]")


def register_geometry_tools(mcp: Any, get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register the read-only geometry tools.

    Args:
        mcp: The MCPServer instance.
        get_bridge: Async function returning the active bridge.
    """

    @mcp.tool(annotations=READ_ONLY)
    async def get_shape_geometry(
        object_name: str,
        elements: list[str] | None = None,
        include: list[Section] | None = None,
        max_items: int = 25,
        offset: int = 0,
        samples: int | None = None,
        sample_tolerance: float | None = None,
        frame: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Read an object's geometry without Python. Read-only.

        Edges: curve type, ends, length, line direction, circle/ellipse
        centre, radius and axis, B-spline degree and poles. Faces: surface
        type, area, centre of mass, bounding box, edge names, plane normal,
        cylinder/sphere/cone/torus axis, centre, radius, facing
        outward/inward. Summary: type, counts, validity, bounding box, area,
        volume, centre of mass. Units mm, mm², mm³, degrees. Bodies use the
        body's shape; links include their placement.

        Args:
            object_name: Object name or label.
            elements: Only these, e.g. ["Edge3", "Face2"].
            include: Any of summary, vertices, edges, faces. Default summary,
                edges, faces (or the types named in elements).
            max_items: Most items per section (1-1000).
            offset: Items to skip per section, for paging.
            samples: Points per edge (2-500).
            sample_tolerance: Or sample to this chord error, mm.
            frame: Give coordinates in this object's local frame.
            doc_name: Document. Defaults to the active one.

        Returns:
            object, frame, needs_recompute, summary, and per section total,
            offset, returned, truncated, items. Unbounded values are null.
        """
        if elements is not None:
            bad = [e for e in elements if not _ELEMENT.match(e.strip())]
            if bad:
                raise ValueError(
                    f"Unknown element names {bad}; use names such as "
                    '"Vertex1", "Edge3" or "Face2"'
                )
        if include is not None:
            unknown = [s for s in include if s not in _SECTIONS]
            if unknown or not include:
                raise ValueError("include may list: " + ", ".join(_SECTIONS))
        if not 1 <= max_items <= 1000:
            raise ValueError("max_items must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be 0 or more")
        _check_samples(samples, sample_tolerance)
        result: dict[str, Any] = await run_operation(
            get_bridge,
            "get_shape_geometry",
            {
                "object_name": object_name,
                "elements": [e.strip() for e in elements] if elements else None,
                "include": list(include) if include is not None else None,
                "max_items": max_items,
                "offset": offset,
                "samples": samples,
                "sample_tolerance": sample_tolerance,
                "frame": frame,
                "doc_name": doc_name,
            },
        )
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def get_section_outline(
        object_name: str,
        plane: str | None = None,
        origin: list[float] | None = None,
        normal: list[float] | None = None,
        offsets: list[float] | None = None,
        max_edges: int = 200,
        samples: int | None = None,
        sample_tolerance: float | None = None,
        frame: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Cross-section an object through planes without adding objects.

        Read-only. Each section lists its wires and edges (as in
        get_shape_geometry), closed and open wire counts, each closed wire's
        enclosed area and the net area with holes removed (mm²).

        Args:
            object_name: Object name or label.
            plane: "XY", "XZ" or "YZ" (global; offsets along +Z, +Y, +X), or
                a datum plane, origin plane or sketch name.
            origin: Point on the plane [x, y, z] mm, used with normal.
            normal: Plane normal [x, y, z], instead of plane.
            offsets: Distances along the normal, mm, one section each (up to
                50). Default [0].
            max_edges: Most edges listed over all sections (1-5000).
            samples: Points per edge (2-500).
            sample_tolerance: Or sample to this chord error, mm.
            frame: Give coordinates in this object's local frame.
            doc_name: Document. Defaults to the active one.

        Returns:
            object, frame, plane (origin, normal, shape_offset_range) and
            sections: offset, hit, wire counts, area, wires, truncated.
        """
        if plane and (origin is not None or normal is not None):
            raise ValueError("Give plane, or origin and normal, not both")
        if not plane and normal is None:
            raise ValueError("Give plane (XY, XZ, YZ or a plane object) or normal")
        _check_vector(origin, "origin")
        _check_vector(normal, "normal")
        if normal is not None and all(value == 0 for value in normal):
            raise ValueError("normal must not be a zero vector")
        if offsets is not None and not 1 <= len(offsets) <= 50:
            raise ValueError("offsets must list 1 to 50 distances")
        if not 1 <= max_edges <= 5000:
            raise ValueError("max_edges must be between 1 and 5000")
        _check_samples(samples, sample_tolerance)
        result: dict[str, Any] = await run_operation(
            get_bridge,
            "get_section_outline",
            {
                "object_name": object_name,
                "plane": plane,
                "origin": list(origin) if origin is not None else None,
                "normal": list(normal) if normal is not None else None,
                "offsets": list(offsets) if offsets is not None else None,
                "max_edges": max_edges,
                "samples": samples,
                "sample_tolerance": sample_tolerance,
                "frame": frame,
                "doc_name": doc_name,
            },
        )
        return result
