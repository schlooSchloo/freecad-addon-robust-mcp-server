"""PartDesign reference geometry: binders, projections and attached datums.

These operations change the document. The MCP server wraps each call in a
transaction, so a failure here (a raised exception) rolls everything back.

Findings for FreeCAD 1.1.4 that shaped this code:

- A sketch or datum in one body cannot use another body's geometry directly.
  Sketches refuse it ("not allowed as external geometry"); datum planes accept
  it silently but ignore both bodies' placements, so they end up in the wrong
  place. A SubShapeBinder in the target body is the fix.
- SubShapeBinder links are made relative to the closest container shared by
  the source and the binder (as the FreeCAD GUI does). Linking through a
  container that also holds the binder's body creates a dependency loop.
- With ``Relative`` on (the default) the binder keeps the source's global
  position, compensating the placements of both bodies, and follows later
  moves while synchronized. With it off the geometry is copied in the source
  object's own coordinates; for sub-elements FreeCAD may drop even the
  element's offset (a box's top face at z=5 came out at z=0).
- Cross-document binding works only when both documents are saved, because
  FreeCAD stores the link by file path.
- The classic PartDesign::ShapeBinder is not offered: SubShapeBinder covers it
  (multiple objects, other documents, frozen copies, placement handling).
- ``NormalToEdge`` makes the plane's normal point against the edge's curve
  direction; ``reverse`` flips it.
"""

from __future__ import annotations

import math
import re
from typing import Any

_ELEMENT_RE = re.compile(r"^(Face|Edge|Vertex)[1-9][0-9]*$")
_ORIGIN_ROLES = (
    "X_Axis",
    "Y_Axis",
    "Z_Axis",
    "XY_Plane",
    "XZ_Plane",
    "YZ_Plane",
    "Origin",
)
_BIND_MODES = {
    "synchronized": "Synchronized",
    "frozen": "Frozen",
    "detached": "Detached",
}
_DATUM_TYPES = {
    "plane": "PartDesign::Plane",
    "line": "PartDesign::Line",
    "point": "PartDesign::Point",
}
_DATUM_NAMES = {"plane": "DatumPlane", "line": "DatumLine", "point": "DatumPoint"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _num(value: float) -> float:
    """Round a float to 1e-6 and clear negative zero."""
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded


def _vec(vector: Any) -> list[float]:
    """Return a FreeCAD vector as a rounded list."""
    return [_num(vector.x), _num(vector.y), _num(vector.z)]


def _same(first: Any, second: Any) -> bool:
    """Return True if two FreeCAD objects are the same document object."""
    if first is None or second is None:
        return first is second
    return bool(
        first.Name == second.Name and first.Document.Name == second.Document.Name
    )


def _document(name: str | None) -> Any:
    """Return the named document, or the active one.

    Raises:
        ValueError: If there is no such document.
    """
    import FreeCAD

    if name:
        if name not in FreeCAD.listDocuments():
            raise ValueError(f"Document not found: {name!r}")
        return FreeCAD.getDocument(name)
    if FreeCAD.ActiveDocument is None:
        raise ValueError("No active document")
    return FreeCAD.ActiveDocument


def _object(doc: Any, name: str) -> Any:
    """Find an object by name, or by a unique label.

    Raises:
        ValueError: If nothing (or more than one object) matches.
    """
    found = doc.getObject(name)
    if found is not None:
        return found
    matches = doc.getObjectsByLabel(name)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError(
            f"Label {name!r} matches {len(matches)} objects in {doc.Name}; "
            "use the object name"
        )
    raise ValueError(f"Object not found in {doc.Name}: {name!r}")


def _body(doc: Any, name: str) -> Any:
    """Return a PartDesign body by name or label.

    Raises:
        ValueError: If the object is missing or not a body.
    """
    body = _object(doc, name)
    if body.TypeId != "PartDesign::Body":
        raise ValueError(f"{body.Name} is a {body.TypeId}, not a PartDesign body")
    return body


def _check_element(element: str) -> None:
    """Raise ValueError unless the text looks like Face3, Edge2 or Vertex1."""
    if not _ELEMENT_RE.match(element):
        raise ValueError(
            f"Bad sub-element {element!r}: use names like 'Face3', 'Edge2', 'Vertex1'"
        )


def _parse_reference(text: str, default_doc: Any) -> tuple[Any, Any, str]:
    """Parse ``Object``, ``Object.Edge3`` or ``Document#Object.Edge3``.

    Returns:
        (document, object, element); element is "" for the whole object.

    Raises:
        ValueError: If the text is malformed or names something missing.
    """
    import FreeCAD

    if not isinstance(text, str) or not text.strip():
        raise ValueError("References must be non-empty strings")
    doc = default_doc
    rest = text.strip()
    if "#" in rest:
        doc_name, rest = rest.split("#", 1)
        if doc_name not in FreeCAD.listDocuments():
            raise ValueError(f"Document not found (it must be open): {doc_name!r}")
        doc = FreeCAD.getDocument(doc_name)
    parts = rest.split(".")
    if len(parts) > 2 or not parts[0]:
        raise ValueError(
            f"Bad reference {text!r}: use 'Object', 'Object.Edge3' or "
            "'Document#Object.Edge3'"
        )
    element = parts[1] if len(parts) == 2 else ""
    if element:
        _check_element(element)
    return doc, _object(doc, parts[0]), element


def _full_path(obj: Any) -> tuple[Any, str]:
    """Return (root object, subname path ending in ".") for an object.

    The path includes the object itself, e.g. (Body, "Pad."). For an object
    at the top of its document the root is the object and the path is "".
    """
    parents = list(getattr(obj, "Parents", []) or [])
    if parents:
        root, path = parents[0]
        return root, path
    return obj, ""


def _chain(obj: Any) -> list[Any]:
    """Return the objects from the document root down to ``obj``."""
    root, path = _full_path(obj)
    chain = [root]
    tokens = [token for token in path.split(".") if token]
    for index in range(len(tokens)):
        sub = root.getSubObject(".".join(tokens[: index + 1]) + ".", retType=1)
        if sub is None:
            break
        chain.append(sub)
    if not _same(chain[-1], obj):
        chain.append(obj)
    return chain


def _global_shape(obj: Any, element: str = "") -> Any:
    """Return an object's shape, or one of its elements, in global coordinates.

    Returns a null shape when the element does not exist.
    """
    import Part

    root, path = _full_path(obj)
    if _same(root, obj):
        return Part.getShape(obj, element, needSubElement=bool(element))
    # needSubElement on a whole-object path returns only the first sub-shape
    # of a compound, so ask for it only when an element is named.
    return Part.getShape(root, path + element, needSubElement=bool(element))


def _element_count(shape: Any, element: str) -> int:
    """Return how many faces, edges or vertices of that kind a shape has."""
    kind = re.sub(r"[0-9]+$", "", element)
    return len(
        {"Face": shape.Faces, "Edge": shape.Edges, "Vertex": shape.Vertexes}[kind]
    )


def _require_element(obj: Any, element: str) -> Any:
    """Return the global shape of an element, or raise a clear ValueError."""
    shape = _global_shape(obj, element)
    if shape is None or shape.isNull():
        whole = _global_shape(obj)
        count = 0 if whole is None or whole.isNull() else _element_count(whole, element)
        raise ValueError(
            f"{obj.Name} has no {element} (it has {count} "
            f"{re.sub(r'[0-9]+$', '', element).lower()} elements)"
        )
    return shape


def _bound_box(shape: Any) -> dict[str, list[float]] | None:
    """Return a shape's bounding box, or None for an empty or infinite shape."""
    if shape is None or shape.isNull():
        return None
    box = shape.BoundBox
    values = (box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax)
    if not box.isValid() or any(not math.isfinite(v) or abs(v) > 1e12 for v in values):
        return None
    return {
        "min": [_num(box.XMin), _num(box.YMin), _num(box.ZMin)],
        "max": [_num(box.XMax), _num(box.YMax), _num(box.ZMax)],
    }


def _shape_summary(shape: Any) -> dict[str, Any]:
    """Summarise a shape: type, counts, area/volume and global bounding box."""
    if shape is None or shape.isNull():
        return {"shape_type": None, "is_null": True}
    summary: dict[str, Any] = {
        "shape_type": shape.ShapeType,
        "solids": len(shape.Solids),
        "faces": len(shape.Faces),
        "edges": len(shape.Edges),
        "vertices": len(shape.Vertexes),
        "area_mm2": _num(shape.Area) if shape.Faces else None,
        "volume_mm3": _num(shape.Volume) if shape.Solids else None,
        "edge_length_mm": _num(sum(edge.Length for edge in shape.Edges))
        if shape.Edges and not shape.Faces
        else None,
        "bound_box": _bound_box(shape),
    }
    return summary


def _boxes_match(first: Any, second: Any, tolerance: float = 1e-5) -> bool | None:
    """Compare two bounding boxes; None if either is missing."""
    a, b = _bound_box(first), _bound_box(second)
    if a is None or b is None:
        return None
    pairs = zip(a["min"] + a["max"], b["min"] + b["max"], strict=True)
    return all(abs(x - y) <= tolerance for x, y in pairs)


def _same_geometry(first: Any, second: Any) -> bool:
    """Return True if two elements coincide (type, size and position)."""
    if first.ShapeType != second.ShapeType:
        return False
    size = max(first.BoundBox.DiagonalLength, 1.0)
    tolerance = max(1e-5, size * 1e-7)
    if first.ShapeType == "Vertex":
        return first.Point.distanceToPoint(second.Point) <= tolerance
    if first.ShapeType == "Edge":
        if abs(first.Length - second.Length) > tolerance:
            return False
        ends_a = sorted(_vec(v.Point) for v in first.Vertexes)
        ends_b = sorted(_vec(v.Point) for v in second.Vertexes)
        if len(ends_a) != len(ends_b) or any(
            max(abs(x - y) for x, y in zip(p, q, strict=True)) > tolerance
            for p, q in zip(ends_a, ends_b, strict=True)
        ):
            return False
    elif abs(first.Area - second.Area) > tolerance * size:
        return False
    return first.CenterOfMass.distanceToPoint(second.CenterOfMass) <= tolerance


def _link_for(source: Any, element: str, binder_body: Any) -> tuple[Any, str]:
    """Return the (object, subname) a binder in ``binder_body`` should link.

    The link starts below the closest container shared with the binder's
    body, so it never runs through a container that holds the binder.

    Raises:
        ValueError: If the source is the binder's body or contains it.
    """
    source_chain = _chain(source)
    if _same(source_chain[-1], binder_body):
        raise ValueError(f"Cannot bind {binder_body.Name} into itself")
    common = 0
    if source.Document.Name == binder_body.Document.Name:
        target_chain = _chain(binder_body)
        while common < min(len(source_chain), len(target_chain)) and _same(
            source_chain[common], target_chain[common]
        ):
            common += 1
        if common == len(source_chain):
            raise ValueError(
                f"{source.Name} contains {binder_body.Name}; bind one of its "
                "features instead"
            )
    rest = source_chain[common:]
    sub = "".join(f"{obj.Name}." for obj in rest[1:]) + element
    return rest[0], sub


def _check_saved_for_cross_document(doc: Any, source_doc: Any) -> None:
    """Raise ValueError unless both documents of a cross-document link are saved."""
    if source_doc.Name == doc.Name:
        return
    unsaved = [d.Name for d in (doc, source_doc) if not d.FileName]
    if unsaved:
        raise ValueError(
            "Binding across documents needs both documents saved to disk first "
            "(FreeCAD stores the link by file path). Not saved: " + ", ".join(unsaved)
        )


def _is_broken(obj: Any) -> bool:
    """Return True if an object is in error after a recompute."""
    state = list(getattr(obj, "State", []) or [])
    return "Invalid" in state or "Error" in state


def _status(obj: Any) -> str:
    """Return FreeCAD's status message for an object."""
    try:
        return str(obj.getStatusString())
    except Exception:
        return "unknown error"


# ---------------------------------------------------------------------------
# create_subshape_binder
# ---------------------------------------------------------------------------


def _make_binder(
    body: Any,
    links: list[tuple[Any, list[str]]],
    *,
    name: str,
    relative: bool,
    make_face: bool,
    fuse: bool,
    before: Any = None,
) -> Any:
    """Create and compute a SubShapeBinder in a body.

    Args:
        body: The body to put the binder in.
        links: (object, subnames) pairs to bind.
        name: Name for the binder.
        relative: Keep the source's global position.
        make_face: Let FreeCAD make faces from closed wires.
        fuse: Fuse bound solids.
        before: Optional object of the body to insert the binder in front of,
            so the tree reads in order. Defaults to the end of the body.

    Raises:
        ValueError: If FreeCAD reports the binder as broken or empty.
    """
    if before is not None and hasattr(body, "insertObject"):
        binder = body.Document.addObject("PartDesign::SubShapeBinder", name)
        body.insertObject(binder, before, False)
    else:
        binder = body.newObject("PartDesign::SubShapeBinder", name)
    binder.Relative = relative
    binder.MakeFace = make_face
    binder.Fuse = fuse
    binder.Support = [(obj, subs) for obj, subs in links]
    body.Document.recompute()
    if _is_broken(binder):
        raise ValueError(f"FreeCAD could not build the binder: {_status(binder)}")
    if binder.Shape.isNull():
        raise ValueError("The binder came out empty; check the sources")
    return binder


def _support_summary(binder: Any) -> list[dict[str, Any]]:
    """Describe a binder's links."""
    return [
        {
            "object": obj.Name,
            "document": obj.Document.Name,
            "subnames": list(subs),
        }
        for obj, subs in binder.Support
    ]


def create_subshape_binder(args: dict[str, Any]) -> dict[str, Any]:
    """Create a PartDesign::SubShapeBinder bound to objects or sub-elements.

    Args:
        args: body (str), sources (list of "Object", "Object.Edge3" or
            "Document#Object.Face2"), name, bind_mode ("synchronized",
            "frozen", "detached"), relative (bool), make_face (bool), fuse
            (bool), doc_name.

    Returns:
        The binder's name, links, settings and shape summary in global
        coordinates, with the sources' bounding box for comparison.

    Raises:
        ValueError: For missing objects, bad references or a failed binder.
    """
    doc = _document(args.get("doc_name"))
    body = _body(doc, args["body"])
    sources = args.get("sources") or []
    if not sources:
        raise ValueError("Give at least one source")
    mode_key = str(args.get("bind_mode", "synchronized")).lower()
    if mode_key not in _BIND_MODES:
        raise ValueError(f"bind_mode must be one of {sorted(_BIND_MODES)}")
    relative = bool(args.get("relative", True))

    links: list[tuple[Any, list[str]]] = []
    source_shapes = []
    for text in sources:
        source_doc, obj, element = _parse_reference(text, doc)
        _check_saved_for_cross_document(doc, source_doc)
        if _same(obj, body):
            raise ValueError(f"Cannot bind {body.Name} into itself")
        if element:
            source_shapes.append(_require_element(obj, element))
        else:
            whole = _global_shape(obj)
            if whole is None or whole.isNull():
                raise ValueError(f"{obj.Name} has no shape to bind")
            source_shapes.append(whole)
        link_obj, sub = _link_for(obj, element, body)
        for existing, subs in links:
            if _same(existing, link_obj):
                subs.append(sub)
                break
        else:
            links.append((link_obj, [sub]))

    binder = _make_binder(
        body,
        links,
        name=args.get("name") or "Binder",
        relative=relative,
        make_face=bool(args.get("make_face", True)),
        fuse=bool(args.get("fuse", False)),
    )
    support = _support_summary(binder)
    binder.BindMode = _BIND_MODES[mode_key]
    if mode_key == "detached":
        doc.recompute()

    import Part

    sources_shape = Part.makeCompound(source_shapes)
    shape = _global_shape(binder)
    return {
        "name": binder.Name,
        "label": binder.Label,
        "type_id": binder.TypeId,
        "body": body.Name,
        "bind_mode": binder.BindMode,
        "relative": bool(binder.Relative),
        "make_face": bool(binder.MakeFace),
        "support": support,
        "shape": _shape_summary(shape),
        "source_bound_box": _bound_box(sources_shape),
        "matches_source_position": _boxes_match(shape, sources_shape)
        if relative
        else None,
        "body_tip": body.Tip.Name if body.Tip else None,
    }


# ---------------------------------------------------------------------------
# project_into_sketch
# ---------------------------------------------------------------------------


def _binder_relates_to(binder: Any, source: Any) -> bool:
    """Return True if a binder links ``source`` directly or through a path."""
    for obj, subs in binder.Support:
        if _same(obj, source):
            return True
        if obj.Document.Name != source.Document.Name:
            continue
        for sub in subs:
            tokens = sub.split(".")[:-1]
            if source.Name in tokens:
                return True
    return False


def _match_elements(
    binder: Any, wanted: list[tuple[str, Any]]
) -> dict[str, str] | None:
    """Find each wanted element (name, global shape) in a binder's shape.

    Returns:
        Mapping of source element to binder element, or None if any is missing.
    """
    shape = _global_shape(binder)
    if shape is None or shape.isNull():
        return None
    found: dict[str, str] = {}
    for element, target in wanted:
        kind = re.sub(r"[0-9]+$", "", element)
        candidates = {
            "Face": shape.Faces,
            "Edge": shape.Edges,
            "Vertex": shape.Vertexes,
        }[kind]
        for index, candidate in enumerate(candidates, start=1):
            if _same_geometry(target, candidate):
                found[element] = f"{kind}{index}"
                break
        else:
            return None
    return found


def _reusable_binder(body: Any, source: Any, wanted: list[tuple[str, Any]]) -> Any:
    """Return (binder, element map) for an existing live binder, or (None, None)."""
    for obj in body.Group:
        if obj.TypeId != "PartDesign::SubShapeBinder":
            continue
        if obj.BindMode != "Synchronized" or not obj.Relative or _is_broken(obj):
            continue
        if not _binder_relates_to(obj, source):
            continue
        mapping = _match_elements(obj, wanted)
        if mapping is not None:
            return obj, mapping
    return None, None


def _flat_external_refs(sketch: Any) -> list[tuple[Any, str]]:
    """Return the sketch's external references as (object, element) pairs."""
    pairs = []
    for obj, subs in sketch.ExternalGeometry:
        for sub in subs if isinstance(subs, (list, tuple)) else [subs]:
            pairs.append((obj, sub))
    return pairs


def _describe_geometry(geometry: Any) -> dict[str, Any]:
    """Describe one sketch geometry in sketch coordinates (mm)."""

    def point(vector: Any) -> list[float]:
        return [_num(vector.x), _num(vector.y)]

    kind = type(geometry).__name__
    info: dict[str, Any] = {"type": kind}
    if kind == "Point":
        info["point"] = [_num(geometry.X), _num(geometry.Y)]
        return info
    if hasattr(geometry, "Center"):
        info["center"] = point(geometry.Center)
    if hasattr(geometry, "Radius"):
        info["radius"] = _num(geometry.Radius)
    if kind != "Circle" and hasattr(geometry, "StartPoint"):
        info["start"] = point(geometry.StartPoint)
        info["end"] = point(geometry.EndPoint)
    return info


def _defining_flag(geometry: Any) -> bool | None:
    """Return whether an external geometry is defining, if FreeCAD says."""
    try:
        for extension in geometry.getExtensions():
            if type(extension).__name__ == "ExternalGeometryExtension":
                return bool(extension.testFlag("Defining"))
    except Exception:
        return None
    return None


def _add_external(
    sketch: Any, obj_name: str, element: str, defining: bool, intersection: bool
) -> None:
    """Call addExternal, using the FreeCAD 1.1 options only when asked.

    Raises:
        ValueError: If the options need a newer FreeCAD or FreeCAD refuses.
    """
    extra = (defining, intersection) if defining or intersection else ()
    try:
        sketch.addExternal(obj_name, element, *extra)
    except TypeError as error:
        if extra:
            raise ValueError(
                "Defining or intersection external geometry needs FreeCAD 1.1"
            ) from error
        raise ValueError(f"FreeCAD refused {obj_name}.{element}: {error}") from error
    except Exception as error:
        raise ValueError(f"FreeCAD refused {obj_name}.{element}: {error}") from error


def _projection_inputs(args: dict[str, Any]) -> tuple[Any, Any, Any, Any, list[str]]:
    """Validate project_into_sketch arguments.

    Returns:
        (document, sketch, source document, source object, elements).

    Raises:
        ValueError: For a missing sketch or source, or bad element names.
    """
    doc = _document(args.get("doc_name"))
    sketch = _object(doc, args["sketch"])
    if not sketch.isDerivedFrom("Sketcher::SketchObject"):
        raise ValueError(f"{sketch.Name} is a {sketch.TypeId}, not a sketch")
    elements = list(args.get("elements") or [])
    if not elements:
        raise ValueError("Give at least one element, e.g. ['Edge3']")
    for element in elements:
        _check_element(element)
    if len(set(elements)) != len(elements):
        raise ValueError("Elements are listed more than once")
    source_doc, source, element = _parse_reference(args["source"], doc)
    if element:
        raise ValueError(
            "Give the source as an object name and list its elements separately"
        )
    if _same(source, sketch):
        raise ValueError("A sketch cannot project its own geometry")
    return doc, sketch, source_doc, source, elements


def _projection_binder(
    args: dict[str, Any],
    sketch: Any,
    body: Any,
    source_doc: Any,
    source: Any,
    wanted: list[tuple[str, Any]],
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Find or create the binder a projection goes through.

    Returns:
        (binder name, source element to binder element, binder summary).

    Raises:
        ValueError: If the binder cannot be built or doesn't match the source.
    """
    binder, mapping = (None, None)
    if args.get("reuse_binder", True):
        binder, mapping = _reusable_binder(body, source, wanted)
    created = binder is None
    if binder is None:
        _check_saved_for_cross_document(sketch.Document, source_doc)
        links = [_link_for(source, element, body) for element, _ in wanted]
        binder = _make_binder(
            body,
            [(links[0][0], [sub for _, sub in links])],
            name=args.get("binder_name") or f"{source.Name}Binder",
            relative=True,
            make_face=False,
            fuse=False,
            before=sketch if any(_same(o, sketch) for o in body.Group) else None,
        )
        binder.Visibility = False
        sketch.Document.recompute()
        mapping = _match_elements(binder, wanted)
    if mapping is None:
        raise ValueError("The binder's geometry does not match the source elements")
    summary = {
        "name": binder.Name,
        "label": binder.Label,
        "created": created,
        "elements": mapping,
    }
    return binder.Name, mapping, summary


def _describe_projected(
    sketch: Any, projected: list[dict[str, Any]], max_geometry: int
) -> tuple[list[dict[str, Any]], bool]:
    """Fill in geometry ids and coordinates for the new external geometry.

    Returns:
        (projected entries, truncated flag).
    """
    total = 0
    truncated = False
    external = sketch.ExternalGeo
    for item in projected:
        start, end = item.pop("range")
        indices = list(range(start, min(end, len(external))))
        entries = []
        for index in indices:
            if total >= max_geometry:
                truncated = True
                break
            total += 1
            info = _describe_geometry(external[index])
            info["geo_id"] = -(index + 1)
            flag = _defining_flag(external[index])
            if flag is not None:
                info["defining"] = flag
            entries.append(info)
        item["geo_ids"] = [-(index + 1) for index in indices]
        item["geometry"] = entries
        if not indices:
            item["note"] = "Projects to nothing on this sketch plane"
    return projected, truncated


def project_into_sketch(args: dict[str, Any]) -> dict[str, Any]:
    """Project edges, faces or vertices of any object into a sketch.

    Args:
        args: sketch, source ("Object" or "Document#Object"), elements (list),
            defining (bool), intersection (bool), reuse_binder (bool),
            binder_name, max_geometry (int), doc_name.

    Returns:
        How the elements were linked (directly or through a binder), the new
        external geometry ids and their coordinates in the sketch.

    Raises:
        ValueError: For missing objects or elements, or if FreeCAD refuses.
    """
    doc, sketch, source_doc, source, elements = _projection_inputs(args)
    defining = bool(args.get("defining", False))
    intersection = bool(args.get("intersection", False))
    max_geometry = max(1, int(args.get("max_geometry", 50)))

    wanted = [(element, _require_element(source, element)) for element in elements]
    group = sketch.getParentGeoFeatureGroup()
    body = group if group is not None and group.TypeId == "PartDesign::Body" else None
    if body is not None and _same(source, body):
        raise ValueError(
            f"{source.Name} is the sketch's own body; project one of its features"
        )
    same_doc = source_doc.Name == doc.Name
    source_body = source.getParentGeoFeatureGroup() if same_doc else None
    direct = body is None or _same(source_body, body)

    binder_info: dict[str, Any] | None = None
    if direct:
        link_name = source.Name
        mapping = {element: element for element in elements}
    else:
        link_name, mapping, binder_info = _projection_binder(
            args, sketch, body, source_doc, source, wanted
        )

    was_broken = _is_broken(sketch)
    existing = {(obj.Name, sub) for obj, sub in _flat_external_refs(sketch)}
    projected = []
    already = []
    for element in elements:
        target = mapping[element]
        if (link_name, target) in existing:
            already.append(element)
            continue
        before = len(sketch.ExternalGeo)
        _add_external(sketch, link_name, target, defining, intersection)
        projected.append(
            {
                "element": element,
                "linked_as": f"{link_name}.{target}",
                "range": (before, len(sketch.ExternalGeo)),
            }
        )

    doc.recompute()
    if _is_broken(sketch) and not was_broken:
        raise ValueError(f"The sketch failed after projecting: {_status(sketch)}")
    results, truncated = _describe_projected(sketch, projected, max_geometry)
    return {
        "sketch": sketch.Name,
        "route": "direct" if direct else "binder",
        "binder": binder_info,
        "projected": results,
        "already_projected": already,
        "external_geometry_count": len(sketch.ExternalGeo) - 2,
        "truncated": truncated,
        "sketch_status": _status(sketch),
    }


# ---------------------------------------------------------------------------
# create_attached_datum
# ---------------------------------------------------------------------------


def _datum_reference(body: Any, text: str) -> tuple[Any, str]:
    """Resolve an attachment reference inside a body.

    Origin roles (XY_Plane, Z_Axis, Origin, ...) mean the body's own origin.

    Raises:
        ValueError: If the reference is missing or outside the body.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("References must be non-empty strings")
    parts = text.strip().split(".")
    if len(parts) > 2 or not parts[0]:
        raise ValueError(f"Bad reference {text!r}: use 'Object' or 'Object.Edge3'")
    name, element = parts[0], parts[1] if len(parts) == 2 else ""
    if element:
        _check_element(element)
    obj = None
    if name in _ORIGIN_ROLES and getattr(body, "Origin", None) is not None:
        for feature in body.Origin.OriginFeatures:
            if feature.Role == name:
                obj = feature
                break
    if obj is None:
        obj = _object(body.Document, name)
    owner = obj.getParentGeoFeatureGroup()
    if not _same(owner, body):
        where = f"in {owner.Name}" if owner is not None else "outside any body"
        raise ValueError(
            f"{obj.Name} is {where}. A datum in {body.Name} can only use geometry "
            f"from {body.Name}: FreeCAD would place it ignoring the placements "
            "involved. Bind it into this body with create_subshape_binder and "
            "attach to the binder."
        )
    if element:
        shape = obj.Shape
        if shape.isNull() or _element_count(shape, element) < int(
            re.sub(r"^[A-Za-z]+", "", element)
        ):
            raise ValueError(f"{obj.Name} has no {element}")
    return obj, element


def _rotation(values: list[float]) -> Any:
    """Build a rotation from [yaw, pitch, roll] in degrees (Z, Y, X)."""
    import FreeCAD

    yaw, pitch, roll = (float(v) for v in values)
    return FreeCAD.Rotation(yaw, pitch, roll)


def _triple(values: Any, label: str) -> list[float]:
    """Validate a list of three numbers."""
    if values is None:
        return [0.0, 0.0, 0.0]
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError(f"{label} must be a list of three numbers")
    return [float(v) for v in values]


def create_attached_datum(args: dict[str, Any]) -> dict[str, Any]:
    """Create a PartDesign datum plane, line or point with an attachment mode.

    Args:
        args: body, attach_to (list of "Object", "Object.Edge3" or origin
            roles), mode (None for FreeCAD's best fit), kind ("plane", "line",
            "point"), offset [x, y, z] mm, rotation [yaw, pitch, roll] degrees,
            path_parameter, reverse, name, doc_name.

    Returns:
        The datum's name, mode and global position with its normal (plane) or
        direction (line).

    Raises:
        ValueError: For bad references, or a mode that doesn't fit them (the
            message lists the modes that do).
    """
    import FreeCAD
    import Part

    doc = _document(args.get("doc_name"))
    body = _body(doc, args["body"])
    kind = str(args.get("kind", "plane")).lower()
    if kind not in _DATUM_TYPES:
        raise ValueError(f"kind must be one of {sorted(_DATUM_TYPES)}")
    references = list(args.get("attach_to") or [])
    if not references:
        raise ValueError("Give at least one reference to attach to")
    refs = [_datum_reference(body, text) for text in references]
    offset = _triple(args.get("offset"), "offset")
    rotation = _triple(args.get("rotation"), "rotation")

    engine_type = {
        "plane": "Attacher::AttachEnginePlane",
        "line": "Attacher::AttachEngineLine",
        "point": "Attacher::AttachEnginePoint",
    }[kind]
    engine = Part.AttachEngine(engine_type)
    engine.References = [(obj, element) for obj, element in refs]
    suggestion = engine.suggestModes()
    if suggestion.get("message") != "OK":
        raise ValueError(
            "FreeCAD cannot attach to these references: "
            f"{suggestion.get('error') or suggestion.get('message')}"
        )
    applicable = list(suggestion.get("allApplicableModes") or [])
    best = suggestion.get("bestFitMode") or None
    mode = args.get("mode") or best
    if not mode:
        raise ValueError(f"No attachment mode fits these references for a {kind}")
    if mode not in applicable:
        raise ValueError(
            f"Mode {mode!r} does not fit these references for a datum {kind}. "
            f"Modes that fit: {', '.join(applicable) or 'none'}"
            + (f" (best fit: {best})" if best else "")
        )

    datum = body.newObject(_DATUM_TYPES[kind], args.get("name") or _DATUM_NAMES[kind])
    support_property = (
        "AttachmentSupport" if hasattr(datum, "AttachmentSupport") else "Support"
    )
    setattr(datum, support_property, [(obj, element) for obj, element in refs])
    datum.MapMode = mode
    datum.MapPathParameter = float(args.get("path_parameter", 0.0))
    datum.MapReversed = bool(args.get("reverse", False))
    datum.AttachmentOffset = FreeCAD.Placement(
        FreeCAD.Vector(*offset), _rotation(rotation)
    )
    doc.recompute()
    if _is_broken(datum):
        raise ValueError(f"FreeCAD could not attach the datum: {_status(datum)}")

    placement = datum.getGlobalPlacement()
    axis = placement.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
    result: dict[str, Any] = {
        "name": datum.Name,
        "label": datum.Label,
        "type_id": datum.TypeId,
        "body": body.Name,
        "mode": datum.MapMode,
        "attached_to": [
            f"{obj.Name}.{element}" if element else obj.Name for obj, element in refs
        ],
        "offset": offset,
        "rotation": rotation,
        "position": _vec(placement.Base),
    }
    if kind == "plane":
        result["normal"] = _vec(axis)
        result["x_axis"] = _vec(placement.Rotation.multVec(FreeCAD.Vector(1, 0, 0)))
    elif kind == "line":
        result["direction"] = _vec(axis)
    if not body.getGlobalPlacement().isIdentity():
        local = datum.Placement
        result["in_body"] = {"position": _vec(local.Base)}
        if kind != "point":
            key = "normal" if kind == "plane" else "direction"
            result["in_body"][key] = _vec(
                local.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
            )
    return result


OPERATIONS = {
    "create_subshape_binder": create_subshape_binder,
    "project_into_sketch": project_into_sketch,
    "create_attached_datum": create_attached_datum,
}
