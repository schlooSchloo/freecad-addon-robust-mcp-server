"""Read-only geometry queries: shape geometry and cross-section outlines.

These operations read an object's shape and describe it (vertices, edges,
faces, sections) without adding, changing or recomputing anything in the
document. They work on in-memory copies of shapes.
"""

from __future__ import annotations

import math
import re
from typing import Any

from ._shape_serialise import (
    PLANE_TYPES,
    RESULT_SIZE_LIMIT,
    ElementIndex,
    SampleBudget,
    SizeBudget,
    bound_box_info,
    edge_info,
    find_document,
    find_object,
    global_placement,
    global_shape,
    is_unbounded,
    num,
    resolve_frame,
    to_frame,
    vec,
    wire_outline,
)

SECTIONS = ("summary", "vertices", "edges", "faces")
DEFAULT_SECTIONS = ("summary", "edges", "faces")
_ELEMENT = re.compile(r"^(Vertex|Edge|Face)([0-9]+)$")
_SECTION_FOR = {"Vertex": "vertices", "Edge": "edges", "Face": "faces"}

STANDARD_PLANES = {
    "XY": (0.0, 0.0, 1.0),
    "XZ": (0.0, 1.0, 0.0),
    "YZ": (1.0, 0.0, 0.0),
}


def _int_arg(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a whole number")
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return value


def _sample_budget(args: dict[str, Any]) -> SampleBudget:
    samples = args.get("samples")
    tolerance = args.get("sample_tolerance")
    if samples is not None and tolerance is not None:
        raise ValueError("Give samples or sample_tolerance, not both")
    if samples is not None:
        samples = _int_arg(args, "samples", 0, 2, 500)
    if tolerance is not None:
        tolerance = float(tolerance)
        if not tolerance > 0:
            raise ValueError("sample_tolerance must be greater than 0 mm")
    return SampleBudget(samples, tolerance)


def _state(obj: Any) -> dict[str, Any]:
    state = list(getattr(obj, "State", []) or [])
    info: dict[str, Any] = {
        "needs_recompute": "Touched" in state,
    }
    if "Invalid" in state:
        info["error"] = obj.getStatusString() if hasattr(obj, "getStatusString") else ""
    if info["needs_recompute"]:
        info["note"] = (
            "The object is marked for recompute; its geometry may be out of date."
        )
    return info


def _is_closed(shape: Any) -> bool | None:
    solids = shape.Solids
    if solids:
        return all(shell.isClosed() for solid in solids for shell in solid.Shells)
    try:
        return bool(shape.isClosed())
    except Exception:  # not defined for every shape type
        return None


def _summary(shape: Any) -> dict[str, Any]:
    """Describe a whole shape: type, counts, size, volume, area."""
    solids = shape.Solids
    summary: dict[str, Any] = {
        "shape_type": shape.ShapeType,
        "is_valid": bool(shape.isValid()),
        "is_closed": _is_closed(shape),
        "solid_count": len(solids),
        "shell_count": len(shape.Shells),
        "face_count": len(shape.Faces),
        "wire_count": len(shape.Wires),
        "edge_count": len(shape.Edges),
        "vertex_count": len(shape.Vertexes),
    }
    unbounded = is_unbounded(shape.BoundBox)
    summary["bound_box"] = bound_box_info(shape.BoundBox)
    if unbounded:
        summary["unbounded"] = True
        summary["area"] = None
        summary["volume"] = None
        summary["center_of_mass"] = None
        summary["note"] = (
            "The shape is unbounded (e.g. an infinite datum plane), so size, "
            "area and volume are not applicable."
        )
        return summary
    summary["area"] = num(shape.Area)
    if solids:
        volume = sum(solid.Volume for solid in solids)
        summary["volume"] = num(volume)
        if volume > 0:
            weighted = [solid.CenterOfMass * solid.Volume for solid in solids]
            total = weighted[0]
            for item in weighted[1:]:
                total = total + item
            summary["center_of_mass"] = vec(total * (1.0 / volume))
        else:
            summary["center_of_mass"] = None
    else:
        summary["volume"] = None
        summary["center_of_mass"] = None
        summary["volume_note"] = "No solids: volume and centre of mass not applicable."
    return summary


def _facing(face: Any, kind: str, surface: Any) -> str | None:
    """Say whether a curved face's normal points away from its axis or centre."""
    try:
        u0, u1, v0, v1 = face.ParameterRange
        u, v = (u0 + u1) / 2.0, (v0 + v1) / 2.0
        point = face.valueAt(u, v)
        normal = face.normalAt(u, v)
        if kind == "Sphere":
            radial = point - surface.Center
        else:
            axis = surface.Axis
            centre = surface.Center
            along = (point - centre).dot(axis)
            radial = point - (centre + axis * along)
            if kind == "Toroid":
                if radial.Length == 0:
                    return None
                radial.normalize()
                radial = point - (centre + axis * along + radial * surface.MajorRadius)
        value = normal.dot(radial)
    except Exception:  # surface evaluation failed
        return None
    if abs(value) < 1e-12:
        return None
    return "outward" if value > 0 else "inward"


def _surface_details(face: Any, surface: Any, info: dict[str, Any]) -> None:
    kind = info["surface_type"]
    if kind == "Plane":
        try:
            u, v = surface.parameter(face.CenterOfMass)
        except Exception:  # unbounded faces have no centre of mass
            u0, u1, v0, v1 = face.ParameterRange
            u, v = (u0 + u1) / 2.0, (v0 + v1) / 2.0
            u = 0.0 if not math.isfinite(u) or abs(u) > 1e50 else u
            v = 0.0 if not math.isfinite(v) or abs(v) > 1e50 else v
        info["normal"] = vec(face.normalAt(u, v))
        if info.get("unbounded"):
            info["position"] = vec(surface.Position)
    elif kind == "Cylinder":
        info["axis"] = vec(surface.Axis)
        info["axis_point"] = vec(surface.Center)
        info["radius"] = num(surface.Radius)
        info["facing"] = _facing(face, kind, surface)
    elif kind == "Sphere":
        info["center"] = vec(surface.Center)
        info["radius"] = num(surface.Radius)
        info["facing"] = _facing(face, kind, surface)
    elif kind == "Cone":
        info["apex"] = vec(surface.Apex)
        info["axis"] = vec(surface.Axis)
        info["semi_angle_deg"] = num(math.degrees(surface.SemiAngle))
        info["reference_radius"] = num(surface.Radius)
        info["reference_center"] = vec(surface.Center)
        info["facing"] = _facing(face, kind, surface)
    elif kind == "Toroid":
        info["center"] = vec(surface.Center)
        info["axis"] = vec(surface.Axis)
        info["major_radius"] = num(surface.MajorRadius)
        info["minor_radius"] = num(surface.MinorRadius)
        info["facing"] = _facing(face, kind, surface)
    elif kind in ("BSplineSurface", "BezierSurface"):
        info["u_degree"] = int(surface.UDegree)
        info["v_degree"] = int(surface.VDegree)
        info["u_pole_count"] = int(surface.NbUPoles)
        info["v_pole_count"] = int(surface.NbVPoles)


def _face_info(face: Any, name: str, edges: ElementIndex) -> dict[str, Any]:
    info: dict[str, Any] = {"name": name}
    try:
        surface = face.Surface
        info["surface_type"] = type(surface).__name__
    except Exception:  # very rare: no underlying surface
        surface = None
        info["surface_type"] = None
    info["orientation"] = face.Orientation
    if is_unbounded(face.BoundBox):
        info.update(
            {"unbounded": True, "area": None, "center_of_mass": None, "bound_box": None}
        )
    else:
        info["area"] = num(face.Area)
        info["center_of_mass"] = vec(face.CenterOfMass)
        info["bound_box"] = bound_box_info(face.BoundBox)
    info["wire_count"] = len(face.Wires)
    if surface is not None:
        _surface_details(face, surface, info)
    info["edges"] = [n for n in (edges.name(e) for e in face.Edges) if n]
    return info


def _vertex_info(vertex: Any, name: str) -> dict[str, Any]:
    return {"name": name, "point": vec(vertex.Point)}


def _page(items: list[Any], offset: int, max_items: int) -> dict[str, Any]:
    chosen = items[offset : offset + max_items]
    return {
        "total": len(items),
        "offset": offset,
        "returned": len(chosen),
        "truncated": offset + len(chosen) < len(items),
        "chosen": chosen,
    }


def _parse_elements(raw: Any, shape: Any) -> dict[str, list[int]]:
    """Group requested element names by section, checking they exist."""
    if not isinstance(raw, list) or not all(isinstance(e, str) for e in raw):
        raise ValueError('elements must be a list of names such as "Edge3"')
    counts = {
        "Vertex": len(shape.Vertexes),
        "Edge": len(shape.Edges),
        "Face": len(shape.Faces),
    }
    grouped: dict[str, list[int]] = {}
    for element in raw:
        match = _ELEMENT.match(element.strip())
        if match is None:
            raise ValueError(
                f"Unknown element name {element!r}; use names such as "
                '"Vertex1", "Edge3" or "Face2"'
            )
        kind, number = match.group(1), int(match.group(2))
        if not 1 <= number <= counts[kind]:
            raise ValueError(
                f"{element} does not exist: the shape has {counts[kind]} "
                f"{kind.lower()}s"
            )
        indices = grouped.setdefault(_SECTION_FOR[kind], [])
        if number - 1 not in indices:
            indices.append(number - 1)
    return grouped


def _choose_sections(
    args: dict[str, Any], shape: Any
) -> tuple[dict[str, list[int]] | None, list[str]]:
    """Return the requested elements (by section) and the sections to list."""
    requested = None
    if args.get("elements"):
        requested = _parse_elements(args["elements"], shape)
    include = args.get("include")
    if include is None:
        if requested is not None:
            include = ["summary", *requested]
        else:
            include = list(DEFAULT_SECTIONS)
    if not isinstance(include, list) or not all(s in SECTIONS for s in include):
        raise ValueError("include may list: " + ", ".join(SECTIONS))
    return requested, include


def _section_indices(
    shape: Any, section: str, requested: dict[str, list[int]] | None
) -> list[int]:
    """Indices of the elements to list in one section."""
    if requested is not None:
        return requested.get(section, [])
    counts = {
        "vertices": len(shape.Vertexes),
        "edges": len(shape.Edges),
        "faces": len(shape.Faces),
    }
    return list(range(counts[section]))


def get_shape_geometry(args: dict[str, Any]) -> dict[str, Any]:
    """Describe an object's shape geometry.

    Args:
        args: ``object_name``; optional ``doc_name``, ``elements`` (list of
            "Vertex1"/"Edge3"/"Face2"), ``include`` (sections), ``max_items``,
            ``offset``, ``samples``, ``sample_tolerance`` and ``frame``.

    Returns:
        The object, frame, state, and the requested sections. Each listed
        section has total, offset, returned, truncated and items.

    Raises:
        ValueError: For a missing object, empty shape or bad argument.
    """
    doc = find_document(args.get("doc_name"))
    obj = find_object(doc, str(args.get("object_name") or ""))
    max_items = _int_arg(args, "max_items", 25, 1, 1000)
    offset = _int_arg(args, "offset", 0, 0, 10_000_000)
    budget = _sample_budget(args)
    inverse, frame_info = resolve_frame(doc, args.get("frame"))
    shape = to_frame(global_shape(obj), inverse)

    requested, include = _choose_sections(args, shape)

    result: dict[str, Any] = {
        "object": obj.Name,
        "label": obj.Label,
        "type": obj.TypeId,
        "frame": frame_info,
        **_state(obj),
    }
    if "summary" in include:
        result["summary"] = _summary(shape)

    # Read each list once: every access builds a new list of sub-shapes.
    vertices, edges, faces = shape.Vertexes, shape.Edges, shape.Faces
    vertex_index = ElementIndex(vertices, "Vertex")
    edge_index = ElementIndex(edges, "Edge")
    listed = [s for s in ("vertices", "edges", "faces") if s in include]
    size_limited = False
    for section in listed:
        # Each listed section gets an equal share of the size limit.
        size = SizeBudget(RESULT_SIZE_LIMIT // len(listed))
        page = _page(_section_indices(shape, section, requested), offset, max_items)
        items = []
        for index in page.pop("chosen"):
            if section == "vertices":
                items.append(_vertex_info(vertices[index], f"Vertex{index + 1}"))
            elif section == "edges":
                name = f"Edge{index + 1}"
                items.append(edge_info(edges[index], budget, name, vertex_index))
            else:
                name = f"Face{index + 1}"
                items.append(_face_info(faces[index], name, edge_index))
            if not size.take(items[-1]):
                items.pop()
                break
        page["returned"] = len(items)
        page["truncated"] = page["offset"] + len(items) < page["total"]
        page["items"] = items
        result[section] = page
        size_limited = size_limited or size.exhausted
    if size_limited:
        result["size_note"] = (
            "Listing stopped early to stay within the bridge's result size "
            "limit. Use offset, a smaller max_items, fewer sections or fewer "
            "sample points to see the rest."
        )
    if budget.omitted:
        result["samples_note"] = (
            f"Sample points were left out for {budget.omitted} edges: the limit "
            "is 5000 points per call. Ask for fewer edges or a larger tolerance."
        )
    return result


def _vector(values: Any, name: str, allow_zero: bool = True) -> Any:
    import FreeCAD

    if not isinstance(values, list | tuple) or len(values) != 3:
        raise ValueError(f"{name} must be a list of three numbers [x, y, z]")
    vector = FreeCAD.Vector(*(float(v) for v in values))
    if not allow_zero and vector.Length < 1e-12:
        raise ValueError(f"{name} must not be a zero vector")
    return vector


def _is_any_type(obj: Any, names: tuple[str, ...]) -> bool:
    for name in names:
        try:
            if obj.isDerivedFrom(name):
                return True
        except Exception:  # type unknown in this FreeCAD version
            pass
    return False


def _resolve_plane(doc: Any, args: dict[str, Any]) -> tuple[Any, Any, str]:
    """Return (origin, unit normal, description) of the base section plane."""
    import FreeCAD

    plane = args.get("plane")
    origin = args.get("origin")
    normal = args.get("normal")
    if plane and (origin is not None or normal is not None):
        raise ValueError("Give plane, or origin and normal, not both")
    if plane:
        key = str(plane).strip().upper()
        if key in STANDARD_PLANES:
            direction = FreeCAD.Vector(*STANDARD_PLANES[key])
            return FreeCAD.Vector(), direction, f"global {key} plane"
        target = find_object(doc, str(plane), "Plane object")
        if not _is_any_type(target, PLANE_TYPES):
            raise ValueError(
                f"{target.Name!r} is a {target.TypeId}, not a plane. Use XY, XZ, "
                "YZ, a datum or origin plane, or a sketch."
            )
        placement = global_placement(target)
        direction = placement.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
        return placement.Base, direction, f"plane of {target.Name}"
    if normal is None:
        raise ValueError("Give plane (XY, XZ, YZ or a plane object) or normal")
    base = _vector(origin, "origin") if origin is not None else FreeCAD.Vector()
    direction = _vector(normal, "normal", allow_zero=False)
    direction.normalize()
    return base, direction, "plane through origin with normal"


def _section_wires(shape: Any, normal: Any, distance: float) -> list[Any]:
    """Cut a shape with the plane ``p . normal = distance``; return wires."""
    import FreeCAD
    import Part

    try:
        return list(shape.slice(normal, distance))
    except Exception:  # fall back to a boolean section with a large face
        box = shape.BoundBox
        size = 4.0 * max(box.DiagonalLength, 1.0)
        centre = box.Center
        point = centre + normal * (distance - centre.dot(normal))
        face = Part.makePlane(size, size, FreeCAD.Vector(), normal)
        corner_shift = face.BoundBox.Center
        face.translate(point - corner_shift)
        cut = shape.section(face)
        groups = Part.sortEdges(cut.Edges) if cut.Edges else []
        return [Part.Wire(edges) for edges in groups]


def _net_area(wires: list[Any]) -> float | None:
    """Area enclosed by closed wires, with inner wires taken as holes."""
    import Part

    closed = [w for w in wires if w.isClosed()]
    if not closed:
        return 0.0
    try:
        face = Part.makeFace(closed, "Part::FaceMakerBullseye")
        return num(face.Area)
    except Exception:  # wires not planar or overlapping
        return None


def get_section_outline(args: dict[str, Any]) -> dict[str, Any]:
    """Cut an object's shape with one or more parallel planes.

    Args:
        args: ``object_name``; ``plane`` (XY/XZ/YZ or a plane object) or
            ``normal`` (with optional ``origin``); optional ``offsets`` (mm
            along the normal), ``max_edges``, ``samples``,
            ``sample_tolerance``, ``frame`` and ``doc_name``.

    Returns:
        The object, frame, plane, and one entry per offset with wire counts,
        enclosed areas (mm²) and the outline edges.

    Raises:
        ValueError: For a missing object, empty shape or bad argument.
    """
    doc = find_document(args.get("doc_name"))
    obj = find_object(doc, str(args.get("object_name") or ""))
    max_edges = _int_arg(args, "max_edges", 200, 1, 5000)
    budget = _sample_budget(args)
    offsets = args.get("offsets")
    if offsets is None:
        offsets = [0.0]
    if not isinstance(offsets, list) or not offsets or len(offsets) > 50:
        raise ValueError("offsets must be a list of 1 to 50 numbers (mm)")
    offsets = [float(value) for value in offsets]
    origin, normal, description = _resolve_plane(doc, args)
    inverse, frame_info = resolve_frame(doc, args.get("frame"))
    shape = global_shape(obj)
    if not shape.Faces:
        raise ValueError(
            f"{obj.Name!r} has no faces to cut (it is a {shape.ShapeType}); "
            "sections need a solid, shell or face."
        )

    def frame_point(point: Any) -> Any:
        return inverse.multVec(point) if inverse is not None else point

    def frame_dir(direction: Any) -> Any:
        if inverse is None:
            return direction
        return inverse.Rotation.multVec(direction)

    corners = [shape.BoundBox.getPoint(i).dot(normal) for i in range(8)]
    extent = [
        num(min(corners) - origin.dot(normal)),
        num(max(corners) - origin.dot(normal)),
    ]

    sections = []
    remaining = max_edges
    size = SizeBudget()
    for offset in offsets:
        point = origin + normal * offset
        wires = _section_wires(shape, normal, point.dot(normal))
        wires = [to_frame(wire, inverse) for wire in wires]
        outline = wire_outline(wires, max(remaining, 0), budget, size)
        remaining -= outline["edges_returned"]
        closed = sum(1 for wire in wires if wire.isClosed())
        entry = {
            "offset": num(offset),
            "plane_point": vec(frame_point(point)),
            "hit": bool(wires),
            "closed_wire_count": closed,
            "open_wire_count": len(wires) - closed,
            "area": _net_area(wires),
            **outline,
        }
        if not wires:
            entry["note"] = (
                "The plane does not cut the shape. Offsets between "
                f"{extent[0]} and {extent[1]} mm (bounding box) can."
            )
        sections.append(entry)
    result: dict[str, Any] = {
        "object": obj.Name,
        "label": obj.Label,
        "frame": frame_info,
        **_state(obj),
        "plane": {
            "description": description,
            "origin": vec(frame_point(origin)),
            "normal": vec(frame_dir(normal)),
            "shape_offset_range": extent,
        },
        "sections": sections,
        "truncated": any(s["truncated"] for s in sections),
    }
    if result["truncated"]:
        result["note"] = (
            f"Edges are listed up to max_edges={max_edges} in total; raise it "
            "to see the rest. Counts and areas are always complete."
        )
    if size.exhausted:
        result["note"] = (
            "Edges stopped early to stay within the bridge's result size limit. "
            "Use fewer offsets or fewer sample points to see the rest. Counts "
            "and areas are always complete."
        )
    if budget.omitted:
        result["samples_note"] = (
            f"Sample points were left out for {budget.omitted} edges: the limit "
            "is 5000 points per call."
        )
    return result


OPERATIONS = {
    "get_shape_geometry": get_shape_geometry,
    "get_section_outline": get_section_outline,
}
