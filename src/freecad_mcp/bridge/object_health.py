"""FreeCAD-side helpers that report object health truthfully.

The functions in :data:`OBJECT_HEALTH_HELPERS` are prepended to code that
runs inside FreeCAD (which need not have the MCP package installed). They are
shared by the recompute, validation and inspection tools so that every tool
reads errors the same way:

- An object is in error only when FreeCAD says so (``Invalid`` or ``Error``
  in its ``State``), and the reason is FreeCAD's own message from
  ``getStatusString()``.
- A shape that is missing or null has no validity: ``shape_valid`` is
  ``None`` ("not applicable"), never ``True``.
- Links whose target can't be found are diagnosed from the stored link data,
  naming the missing file, document or object.
- Sketches that hold only construction or external (projected) geometry are
  references with no output geometry, which is not a failure.
- Unbounded shapes (datum planes and lines) have no meaningful area, volume
  or bounding box.

All text that leaves FreeCAD goes through ``_mcp_text`` so it is valid
XML 1.0 (needed by the XML-RPC transport) and bounded in length.
"""

OBJECT_HEALTH_HELPERS = r'''
def _mcp_text(value, limit=1000):
    """Return bounded, XML-safe text."""
    value = str(value)
    if len(value) > limit:
        value = value[:limit] + "<truncated>"
    return "".join(
        char if (ord(char) in (9, 10, 13) or 32 <= ord(char) <= 0xD7FF
                 or 0xE000 <= ord(char) <= 0xFFFD
                 or 0x10000 <= ord(char) <= 0x10FFFF) else "�"
        for char in value
    )


def _mcp_state(obj):
    try:
        return [str(item) for item in obj.State]
    except Exception:
        return []


def _mcp_in_error(obj):
    state = _mcp_state(obj)
    return "Invalid" in state or "Error" in state


def _mcp_status_message(obj):
    """Return FreeCAD's own error text for an object in error, else None."""
    if not _mcp_in_error(obj):
        return None
    try:
        text = str(obj.getStatusString()).strip()
    except Exception:
        text = ""
    if text in ("", "Valid", "Touched"):
        return "FreeCAD marked the object as failed but gave no error message"
    return _mcp_text(text)


def _mcp_xlink_targets(obj, prop):
    """Read the stored file and object name of an XLink property.

    An unresolved XLink reads as None in Python, so the stored target is
    taken from the property's saved content instead.
    """
    import io
    import re
    import zipfile
    from xml.sax.saxutils import unescape

    try:
        data = bytes(obj.dumpPropertyContent(prop))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            text = "".join(
                archive.read(name).decode("utf-8", "replace")
                for name in archive.namelist()
            )
    except Exception:
        return []
    targets = []
    for element in re.findall(r"<XLink\b[^>]*>", text):
        attrs = dict(
            (key, unescape(value, {"&quot;": '"', "&apos;": "'"}))
            for key, value in re.findall(r'(\w+)="([^"]*)"', element)
        )
        targets.append((attrs.get("file", ""), attrs.get("name", "")))
    return targets


def _mcp_find_document(owner, file_name):
    """Return the open document saved at file_name (relative to owner)."""
    import os

    base = os.path.dirname(getattr(owner, "FileName", "") or "")
    path = file_name if os.path.isabs(file_name) else os.path.join(base, file_name)
    path = os.path.normcase(os.path.abspath(path))
    for candidate in FreeCAD.listDocuments().values():
        candidate_path = getattr(candidate, "FileName", "") or ""
        if candidate_path and os.path.normcase(os.path.abspath(candidate_path)) == path:
            return candidate, path
    return None, path


def _mcp_link_problems(obj):
    """Describe link properties whose target can't be found.

    Returns a list of dicts with ``property``, ``problem``, ``message`` and,
    where known, ``linked_object``, ``linked_file`` and ``linked_document``.
    """
    import os

    problems = []
    try:
        names = list(obj.PropertiesList)
    except Exception:
        return problems
    for prop in names:
        try:
            type_id = obj.getTypeIdOfProperty(prop)
        except Exception:
            continue
        if "XLink" not in type_id or "List" in type_id:
            continue
        try:
            value = getattr(obj, prop)
        except Exception:
            value = None
        if value is not None:
            continue
        targets = [t for t in _mcp_xlink_targets(obj, prop) if t[1]]
        if not targets:
            if prop == "LinkedObject":
                problems.append({
                    "property": prop,
                    "problem": "no_target",
                    "message": "Link has no target object (LinkedObject is empty)",
                })
            continue
        for file_name, object_name in targets:
            entry = {"property": prop, "linked_object": _mcp_text(object_name)}
            if not file_name:
                entry["problem"] = "missing_object"
                entry["message"] = _mcp_text(
                    "Linked object '" + object_name + "' was not found in this document"
                )
            else:
                target_doc, path = _mcp_find_document(obj.Document, file_name)
                entry["linked_file"] = _mcp_text(path)
                if target_doc is None:
                    if os.path.exists(path):
                        entry["problem"] = "document_not_open"
                        entry["message"] = _mcp_text(
                            "Linked document is not open: " + path
                            + " (open it to restore the link to '" + object_name + "')"
                        )
                    else:
                        entry["problem"] = "missing_file"
                        entry["message"] = _mcp_text(
                            "Linked file not found: " + path
                            + " (expected to contain '" + object_name + "')"
                        )
                else:
                    entry["linked_document"] = _mcp_text(target_doc.Name)
                    if target_doc.getObject(object_name) is None:
                        entry["problem"] = "missing_object"
                        entry["message"] = _mcp_text(
                            "Linked object '" + object_name + "' was not found in document '"
                            + target_doc.Name + "'"
                        )
                    else:
                        entry["problem"] = "not_restored"
                        entry["message"] = _mcp_text(
                            "Linked object '" + object_name + "' exists in document '"
                            + target_doc.Name + "' but the link is not restored; recompute the document"
                        )
            problems.append(entry)
    return problems


def _mcp_sketch_geometry(obj):
    """Count a sketch's normal, construction and external geometry, else None."""
    try:
        if not obj.isDerivedFrom("Sketcher::SketchObject"):
            return None
    except Exception:
        return None
    normal = 0
    construction = 0
    geometry = list(getattr(obj, "Geometry", []) or [])
    for index, geo in enumerate(geometry):
        try:
            is_construction = bool(obj.getConstruction(index))
        except Exception:
            is_construction = bool(getattr(geo, "Construction", False))
        if is_construction:
            construction += 1
        else:
            normal += 1
    try:
        external = sum(len(subs) for _, subs in obj.ExternalGeometry)
    except Exception:
        external = 0
    return {"normal": normal, "construction": construction, "external": external}


def _mcp_shape(obj):
    """Return the object's shape, or None if it has none or reading fails."""
    try:
        return getattr(obj, "Shape", None)
    except Exception:
        return None


def _mcp_is_null(shape):
    try:
        return bool(shape.isNull())
    except Exception:
        return False


def _mcp_is_unbounded(shape):
    """True for infinite shapes such as datum planes and lines."""
    try:
        if hasattr(shape, "isInfinite"):
            return bool(shape.isInfinite())
    except Exception:
        pass
    try:
        box = shape.BoundBox
        return max(abs(box.XMin), abs(box.XMax), abs(box.YMin), abs(box.YMax),
                   abs(box.ZMin), abs(box.ZMax)) >= 1e90
    except Exception:
        return False


def _mcp_solid_volume(shape):
    """Total volume of the shape's solids, or None if it has no solids."""
    try:
        solids = list(shape.Solids)
    except Exception:
        return None
    if not solids:
        return None
    return sum(solid.Volume for solid in solids)


def _mcp_check_object(obj):
    """Check one object and explain any problem in FreeCAD's own words."""
    state = _mcp_state(obj)
    has_errors = "Invalid" in state or "Error" in state
    error_messages = []
    warnings = []
    status_message = _mcp_status_message(obj)
    if status_message:
        error_messages.append(status_message)

    link_problems = _mcp_link_problems(obj)
    for problem in link_problems:
        error_messages.append(problem["message"])

    sketch = _mcp_sketch_geometry(obj)
    shape = _mcp_shape(obj)
    has_shape = shape is not None
    shape_null = _mcp_is_null(shape) if has_shape else None
    shape_valid = None
    unbounded = False
    volume = None
    area = None
    if has_shape and not shape_null:
        try:
            shape_valid = bool(shape.isValid())
            if not shape_valid:
                error_messages.append("Shape geometry is invalid")
        except Exception as e:
            shape_valid = False
            error_messages.append("Error checking shape: " + _mcp_text(e))
        unbounded = _mcp_is_unbounded(shape)
        if unbounded:
            warnings.append(
                "Shape is unbounded (an infinite datum plane or line), "
                "so area and volume are not applicable"
            )
        else:
            try:
                volume = _mcp_solid_volume(shape)
                if volume is not None and volume <= 0:
                    warnings.append("Shape has non-positive volume: " + str(volume))
            except Exception:
                volume = None
            try:
                area = shape.Area
            except Exception:
                area = None

    reference_only = (
        sketch is not None and sketch["normal"] == 0 and not has_errors and not link_problems
    )
    if reference_only:
        if sketch["construction"] or sketch["external"]:
            warnings.append(
                "Sketch has only construction or external (projected) geometry: "
                "it is a valid reference but produces no output geometry"
            )
        else:
            warnings.append("Sketch is empty: it produces no output geometry")
    elif has_shape and shape_null and not has_errors and not link_problems:
        type_id = getattr(obj, "TypeId", "")
        if (type_id.startswith("Part::") or type_id.startswith("PartDesign::")) \
                and not type_id.endswith("Body"):
            warnings.append("Object has no shape (null shape)")
    if has_errors and has_shape and not shape_null:
        warnings.append(
            "FreeCAD reports an error, so the shape values may be from the "
            "last successful recompute"
        )
    if "Touched" in state and not has_errors:
        warnings.append("Object needs recompute; reported values may be out of date")

    if link_problems:
        classification = "unresolved_link"
    elif has_errors:
        classification = "error"
    elif shape_valid is False:
        classification = "invalid_shape"
    elif reference_only:
        classification = "reference_only"
    elif not has_shape or shape_null:
        classification = "no_shape"
    else:
        classification = "ok"

    if has_errors and not error_messages:
        error_messages.append("FreeCAD marked the object as failed but gave no error message")

    return {
        "valid": not has_errors and shape_valid is not False and not link_problems,
        "name": obj.Name,
        "label": _mcp_text(obj.Label),
        "type_id": obj.TypeId,
        "classification": classification,
        "state": state,
        "has_errors": has_errors,
        "recompute_needed": "Touched" in state,
        "status_message": status_message,
        "has_shape": has_shape,
        "shape_null": shape_null,
        "shape_valid": shape_valid,
        "unbounded": unbounded,
        "volume": volume,
        "area": area,
        "output_geometry": (not reference_only) and has_shape and not shape_null,
        "sketch_geometry": sketch,
        "link_problems": link_problems,
        "error_messages": error_messages,
        "warnings": warnings,
    }


def _mcp_failure_entry(obj, dependents_limit=20):
    """Describe an object FreeCAD marked as failed, with its message."""
    entry = {
        "document": obj.Document.Name,
        "name": obj.Name,
        "label": _mcp_text(obj.Label),
        "type_id": obj.TypeId,
        "state": _mcp_state(obj),
        "message": _mcp_status_message(obj),
    }
    problems = _mcp_link_problems(obj)
    if problems:
        entry["link_problems"] = problems
    try:
        dependents = sorted(o.Name for o in obj.InListRecursive)
    except Exception:
        dependents = []
    entry["dependents"] = dependents[:dependents_limit]
    entry["dependents_truncated"] = len(dependents) > dependents_limit
    return entry


def _mcp_recompute_report(objects, limit=50):
    """List failed objects after a recompute, capped at limit entries."""
    failed = [obj for obj in objects if _mcp_in_error(obj)]
    entries = [_mcp_failure_entry(obj) for obj in failed[:limit]]
    if entries:
        parts = []
        for entry in entries[:5]:
            parts.append(entry["name"] + ": " + (entry["message"] or "no message"))
        error = (
            str(len(failed)) + " object(s) failed to recompute. " + "; ".join(parts)
        )
        if len(failed) > 5:
            error += "; ..."
    else:
        error = None
    return {
        "success": not failed,
        "invalid_objects": entries,
        "invalid_objects_truncated": len(failed) > limit,
        "error": error,
    }
'''
