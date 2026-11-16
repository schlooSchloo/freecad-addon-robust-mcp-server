"""Mesh tools: make a native mesh from a shape, and check mesh quality."""

from __future__ import annotations

from typing import Any

from ._mesh_helpers import (
    bounds_of,
    check_deflection,
    get_document,
    get_object,
    global_shape,
    round_value,
    tessellate,
)

# The topology analysis runs in Python; keep it to meshes it can finish.
MAX_ANALYSED_FACETS = 500_000
_DEFAULT_LINEAR = 0.1
_DEFAULT_ANGULAR = 30.0


def _shape_type_note(shape: Any) -> bool:
    """Return True if the shape contains at least one solid."""
    return bool(shape.Solids)


def create_mesh_from_shape(args: dict[str, Any]) -> dict[str, Any]:
    """Create a ``Mesh::Feature`` from an object's shape.

    Args:
        args: ``source`` (object name or label), optional ``name``,
            ``linear_deflection`` (mm), ``angular_deflection`` (degrees),
            ``relative`` and ``doc_name``.

    Returns:
        Description of the new mesh: name, counts, bounds, settings used.

    Raises:
        ValueError: For an unknown source, no faces, or bad settings.
    """
    doc = get_document(args.get("doc_name"))
    source = get_object(doc, args.get("source"))
    linear, angular = check_deflection(
        args.get("linear_deflection", _DEFAULT_LINEAR),
        args.get("angular_deflection", _DEFAULT_ANGULAR),
    )
    relative = bool(args.get("relative", False))
    custom_name = args.get("name")
    if custom_name is not None and (
        not isinstance(custom_name, str) or not custom_name
    ):
        raise ValueError("name must be a non-empty string")

    shape = global_shape(source)
    mesh = tessellate(shape, linear, angular, relative)

    unit = "relative" if relative else "mm"
    label = f"{source.Label} mesh (lin {linear:g} {unit}, ang {angular:g} deg)"
    feature = doc.addObject("Mesh::Feature", custom_name or f"{source.Name}_mesh")
    try:
        feature.Mesh = mesh
        if custom_name:
            feature.Label = custom_name
        else:
            feature.Label = label
    except Exception:
        doc.removeObject(feature.Name)
        raise

    return {
        "mesh": feature.Name,
        "label": feature.Label,
        "source": source.Name,
        "facets": mesh.CountFacets,
        "points": mesh.CountPoints,
        "edges": mesh.CountEdges,
        "bounds": bounds_of(mesh.BoundBox),
        "settings": {
            "linear_deflection": linear,
            "angular_deflection_degrees": angular,
            "relative": relative,
        },
        "source_has_solid": _shape_type_note(shape),
        "placement_policy": (
            "Mesh points are in global coordinates, copied from the source's "
            "global placement. The mesh feature's own Placement is identity."
        ),
        "note": (
            "Static snapshot: the mesh will not update when the source "
            "changes. Run again to refresh it."
        ),
    }


def _weld(points: list[Any], tolerance: float) -> tuple[list[int], int]:
    """Map each point to an id shared by points closer than ``tolerance``.

    Points are snapped to a grid of cell size ``tolerance``, so two points
    just either side of a cell boundary can stay separate.

    Args:
        points: ``FreeCAD.Vector`` points.
        tolerance: Cell size in mm.

    Returns:
        The id for each point and the number of duplicates found.
    """
    ids: dict[tuple[int, int, int], int] = {}
    mapping: list[int] = []
    for point in points:
        key = (
            round(point.x / tolerance),
            round(point.y / tolerance),
            round(point.z / tolerance),
        )
        mapping.append(ids.setdefault(key, len(ids)))
    return mapping, len(points) - len(ids)


def _find(parent: dict[Any, Any], item: Any) -> Any:
    """Union-find root lookup with path halving."""
    while parent[item] != item:
        parent[item] = parent[parent[item]]
        item = parent[item]
    return item


def _analyse(  # noqa: PLR0912
    mesh: Any, tolerance: float, max_components: int
) -> dict[str, Any]:
    """Analyse a mesh's topology and geometry (read-only).

    Args:
        mesh: A ``Mesh.Mesh`` (not modified), points in global coordinates.
        tolerance: Welding and degeneracy tolerance in mm.
        max_components: How many component sizes to list.

    Returns:
        The measured values (no verdict text).
    """
    points, facets = mesh.Topology
    ids, duplicated_points = _weld(points, tolerance)
    tolerance_area = tolerance * tolerance

    degenerate = 0
    seen_triples: set[tuple[int, int, int]] = set()
    duplicated_facets = 0
    edge_uses: dict[
        tuple[int, int], list[int]
    ] = {}  # (low, high) -> [forward, backward]
    kept: list[tuple[int, int, int]] = []
    area = 0.0
    signed_six_volume = 0.0
    origin = points[0] if points else None
    for facet in facets:
        p0, p1, p2 = (points[facet[0]], points[facet[1]], points[facet[2]])
        a, b, c = (ids[facet[0]], ids[facet[1]], ids[facet[2]])
        cross = (p1 - p0).cross(p2 - p0)
        facet_area = cross.Length / 2
        area += facet_area
        o0, o1, o2 = p0 - origin, p1 - origin, p2 - origin
        signed_six_volume += o0.dot(o1.cross(o2))
        if len({a, b, c}) < 3 or facet_area < tolerance_area:
            degenerate += 1
            continue
        triple = tuple(sorted((a, b, c)))
        if triple in seen_triples:
            duplicated_facets += 1
        seen_triples.add(triple)  # type: ignore[arg-type]
        kept.append((a, b, c))
        for start, end in ((a, b), (b, c), (c, a)):
            key = (start, end) if start < end else (end, start)
            uses = edge_uses.setdefault(key, [0, 0])
            uses[0 if start < end else 1] += 1

    open_edges = nonmanifold_edges = inconsistent_edges = 0
    for forward, backward in edge_uses.values():
        total = forward + backward
        if total == 1:
            open_edges += 1
        elif total > 2:
            nonmanifold_edges += 1
        elif forward != 1:
            inconsistent_edges += 1  # two uses in the same direction

    # Components and non-manifold points (vertices touching separate fans).
    parent = {vertex: vertex for facet in kept for vertex in facet}
    fans: dict[int, dict[int, int]] = {}
    for a, b, c in kept:
        for vertex, other_1, other_2 in ((a, b, c), (b, c, a), (c, a, b)):
            fan = fans.setdefault(vertex, {})
            for neighbour in (other_1, other_2):
                fan.setdefault(neighbour, neighbour)
            fan_root_1, fan_root_2 = _find(fan, other_1), _find(fan, other_2)
            fan[fan_root_1] = fan_root_2
        root_a, root_b, root_c = _find(parent, a), _find(parent, b), _find(parent, c)
        parent[root_a] = root_c
        parent[root_b] = root_c
    nonmanifold_points = 0
    for fan in fans.values():
        if len({_find(fan, key) for key in fan}) > 1:
            nonmanifold_points += 1
    sizes: dict[Any, int] = {}
    for a, _b, _c in kept:
        root = _find(parent, a)
        sizes[root] = sizes.get(root, 0) + 1
    component_sizes = sorted(sizes.values(), reverse=True)

    return {
        "facets": len(facets),
        "points": len(points),
        "duplicated_points": duplicated_points,
        "degenerate_facets": degenerate,
        "duplicated_facets": duplicated_facets,
        "open_edges": open_edges,
        "nonmanifold_edges": nonmanifold_edges,
        "nonmanifold_points": nonmanifold_points,
        "inconsistent_edges": inconsistent_edges,
        "component_sizes": component_sizes,
        "max_components": max_components,
        "area": area,
        "signed_volume": signed_six_volume / 6,
    }


def _self_intersections(mesh: Any, cap: int) -> dict[str, Any]:
    """Look for self-intersecting facets using FreeCAD's mesh API.

    Args:
        mesh: A ``Mesh.Mesh`` (not modified).
        cap: Most facet pairs to return.

    Returns:
        Whether there are any, the number of pairs, and a truncation flag.
    """
    pairs = mesh.getSelfIntersections()
    return {
        "has_self_intersections": bool(pairs),
        "intersecting_facet_pairs": len(pairs),
        "pairs": [list(pair) for pair in pairs[:cap]],
        "truncated": len(pairs) > cap,
    }


def _issues_and_repairs(
    measured: dict[str, Any], consistent: bool, inverted: bool
) -> tuple[list[str], list[str]]:
    """Describe problems in words and name the repair that would apply.

    Args:
        measured: Values from ``_analyse``.
        consistent: Whether facet winding is consistent.
        inverted: Whether the mesh is closed and inside out.

    Returns:
        Issue descriptions and suggested FreeCAD repairs (nothing is run).
    """
    issues: list[str] = []
    repairs: list[str] = []
    if measured["open_edges"]:
        issues.append(f"{measured['open_edges']} open (boundary) edges: not watertight")
        repairs.append("fill holes (Mesh.fillupHoles)")
    if measured["nonmanifold_edges"]:
        issues.append(f"{measured['nonmanifold_edges']} non-manifold edges")
        repairs.append("remove non-manifolds (Mesh.removeNonManifolds)")
    if measured["nonmanifold_points"]:
        issues.append(f"{measured['nonmanifold_points']} non-manifold points")
        repairs.append("remove non-manifold points (Mesh.removeNonManifoldPoints)")
    if not consistent:
        issues.append("facet winding is inconsistent")
        repairs.append("harmonize normals (Mesh.harmonizeNormals)")
    if inverted:
        issues.append("normals point inward (negative enclosed volume)")
        repairs.append("flip normals (Mesh.flipNormals)")
    if measured["degenerate_facets"]:
        issues.append(f"{measured['degenerate_facets']} degenerate facets")
        repairs.append("fix degenerations (Mesh.fixDegenerations)")
    if measured["duplicated_facets"]:
        issues.append(f"{measured['duplicated_facets']} duplicated facets")
        repairs.append("remove duplicated facets (Mesh.removeDuplicatedFacets)")
    if measured["duplicated_points"]:
        issues.append(f"{measured['duplicated_points']} duplicated points")
        repairs.append("remove duplicated points (Mesh.removeDuplicatedPoints)")
    return issues, repairs


def check_mesh(args: dict[str, Any]) -> dict[str, Any]:
    """Report the quality of a mesh, or of a temporary tessellation.

    Nothing is added to or changed in the document.

    Args:
        args: ``mesh`` (a ``Mesh::Feature``, or another shape object to
            tessellate temporarily), ``tolerance`` (mm),
            ``check_self_intersections``, ``max_components``,
            ``linear_deflection`` and ``angular_deflection`` (shape objects
            only) and ``doc_name``.

    Returns:
        The quality report.

    Raises:
        ValueError: For an unknown object, bad settings, or a mesh too large
            to analyse.
    """
    doc = get_document(args.get("doc_name"))
    obj = get_object(doc, args.get("mesh"))
    tolerance = args.get("tolerance", 1e-4)
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not tolerance > 0
    ):
        raise ValueError("tolerance must be a number greater than 0")
    max_components = args.get("max_components", 20)
    if (
        isinstance(max_components, bool)
        or not isinstance(max_components, int)
        or max_components < 1
    ):
        raise ValueError("max_components must be a whole number of at least 1")

    source_kind = "mesh"
    settings: dict[str, Any] | None = None
    if obj.isDerivedFrom("Mesh::Feature"):
        mesh = obj.Mesh
    else:
        linear, angular = check_deflection(
            args.get("linear_deflection", _DEFAULT_LINEAR),
            args.get("angular_deflection", _DEFAULT_ANGULAR),
        )
        mesh = tessellate(global_shape(obj), linear, angular, False)
        source_kind = "shape_tessellation"
        settings = {"linear_deflection": linear, "angular_deflection_degrees": angular}

    if mesh.CountFacets == 0:
        return {
            "object": obj.Name,
            "source_kind": source_kind,
            "facets": 0,
            "points": mesh.CountPoints,
            "issues": ["the mesh has no facets"],
            "suggested_repairs": [],
        }
    if mesh.CountFacets > MAX_ANALYSED_FACETS:
        raise ValueError(
            f"The mesh has {mesh.CountFacets} facets; this check handles up "
            f"to {MAX_ANALYSED_FACETS}"
        )

    measured = _analyse(mesh, float(tolerance), max_components)
    sizes = measured["component_sizes"]
    closed = (
        measured["open_edges"] == 0
        and measured["nonmanifold_edges"] == 0
        and bool(sizes)
    )
    consistent = (
        measured["inconsistent_edges"] == 0
        and mesh.countNonUniformOrientedFacets() == 0
    )
    volume = measured["signed_volume"]
    volume_valid = closed and consistent
    inverted = volume_valid and volume < 0

    issues, repairs = _issues_and_repairs(measured, consistent, inverted)

    result: dict[str, Any] = {
        "object": obj.Name,
        "source_kind": source_kind,
        "tessellation_settings": settings,
        "facets": measured["facets"],
        "points": measured["points"],
        "bounds": bounds_of(mesh.BoundBox),
        "area_mm2": round_value(measured["area"]),
        "signed_volume_mm3": round_value(volume, 4),
        "volume_valid": volume_valid,
        "inverted": inverted,
        "is_closed": closed,
        "consistent_winding": consistent,
        "is_solid": closed and consistent and not measured["nonmanifold_points"],
        "freecad_is_solid": bool(mesh.isSolid()),
        "components": len(sizes),
        "component_facets": sizes[:max_components],
        "components_truncated": len(sizes) > max_components,
        "open_edges": measured["open_edges"],
        "nonmanifold_edges": measured["nonmanifold_edges"],
        "nonmanifold_points": measured["nonmanifold_points"],
        "inconsistent_orientation_edges": measured["inconsistent_edges"],
        "degenerate_facets": measured["degenerate_facets"],
        "duplicated_facets": measured["duplicated_facets"],
        "duplicated_points": measured["duplicated_points"],
        "self_intersections": None,
        "tolerances": {
            "point_merge_mm": float(tolerance),
            "degenerate_facet_area_mm2": float(tolerance) ** 2,
            "note": (
                "Points within this distance count as the same point "
                "(grid snapping). Components are groups of facets that "
                "share points."
            ),
        },
    }
    if args.get("check_self_intersections"):
        found = _self_intersections(mesh, 20)
        result["self_intersections"] = found
        if found["has_self_intersections"]:
            issues.append(
                f"{found['intersecting_facet_pairs']} self-intersecting facet pairs"
            )
            repairs.append("fix self-intersections (Mesh.fixSelfIntersections)")
    if len(sizes) > 1:
        issues.append(
            f"{len(sizes)} separate components (reported, not an error if intended)"
        )
    if not volume_valid:
        result["volume_note"] = (
            "Volume is not a reliable physical measurement: the mesh is not "
            "closed with consistent winding."
        )
    result["issues"] = issues
    result["suggested_repairs"] = repairs
    result["no_repair_done"] = True
    return result


OPERATIONS = {
    "create_mesh_from_shape": create_mesh_from_shape,
    "check_mesh": check_mesh,
}
