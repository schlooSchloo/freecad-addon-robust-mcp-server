"""Build the FreeCAD-side programs for editing and deleting objects.

All three bridge modes send the same programs, so property conversion, error
messages and undo names cannot drift between them. The programs run inside
FreeCAD, which need not have the MCP package installed, so they use only the
standard library and FreeCAD's own modules.

Expected problems (unknown document, object or property, a value that does not
fit the property) are reported as ``{"error": message}`` rather than raised, so
callers get one readable sentence instead of a traceback.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from freecad_mcp.bridge.base import ExecutionResult, ObjectInfo

_COMMON_HELPERS = r'''
import difflib
import math
import re


class _EditError(Exception):
    """A problem with the request, reported to the caller as plain text."""


def _xml_text(value):
    """Return text that XML-RPC can carry."""
    return "".join(
        char if (ord(char) in (9, 10, 13) or 32 <= ord(char) <= 0xD7FF
                 or 0xE000 <= ord(char) <= 0xFFFD
                 or 0x10000 <= ord(char) <= 0x10FFFF) else "�"
        for char in str(value)
    )


def _suggest(candidates):
    return (". Did you mean: " + ", ".join(candidates) + "?") if candidates else ""


def _find_doc(name):
    if name is None:
        doc = FreeCAD.ActiveDocument
        if doc is None:
            raise _EditError("No active document; pass doc_name")
        return doc
    docs = FreeCAD.listDocuments()
    doc = docs.get(name)
    if doc is None:
        names = sorted(docs)
        close = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
        shown = ", ".join(names[:10]) or "none"
        if len(names) > 10:
            shown += " and %d more" % (len(names) - 10)
        raise _EditError(
            "Document not found: %r%s Open documents: %s"
            % (name, _suggest(close) or ".", shown)
        )
    return doc


def _object_suggestions(doc, name):
    names = [o.Name for o in doc.Objects]
    labels = {o.Label: o.Name for o in doc.Objects}
    found = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
    for label in difflib.get_close_matches(name, list(labels), n=3, cutoff=0.6):
        hint = "%s (label %r)" % (labels[label], label)
        if labels[label] not in found:
            found.append(hint)
    return found[:3]


def _missing_object(doc, name):
    return _EditError(
        "Object not found: %r in document %r%s"
        % (name, doc.Name, _suggest(_object_suggestions(doc, name)))
    )


def _is_doc_object(value):
    try:
        return bool(value.isDerivedFrom("App::DocumentObject"))
    except Exception:
        return False
'''

_EDIT_PROGRAM = r'''
_LINK_TYPE = re.compile(r"^App::Property(X?)Link(Sub)?(List)?(Child|Global|Hidden)?$")
_MAX_ITEMS = 100


def _find_linked(doc, name):
    """Find a link target by internal name or, if unique, by label."""
    if not isinstance(name, str) or not name:
        raise _EditError("Linked object names must be non-empty text, got %r" % (name,))
    target = doc.getObject(name)
    if target is None:
        found = doc.getObjectsByLabel(name)
        if len(found) == 1:
            target = found[0]
    if target is None:
        raise _missing_object(doc, name)
    return target


def _link_kind(ptype):
    match = _LINK_TYPE.match(ptype or "")
    if match is None:
        return None
    is_x, has_sub, is_list, _ = match.groups()
    if is_list:
        return "sublist" if has_sub else "list"
    if is_x:
        return "xlink"
    return "sub" if has_sub else "link"


def _subelements(value, label):
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(s, str) for s in value):
        return list(value)
    raise _EditError(
        "%s: sub-elements must be a list of names such as [\"Face1\"], got %r"
        % (label, value)
    )


def _reference(doc, spec, label):
    """Parse one link reference into (object, sub-elements or None)."""
    if isinstance(spec, str):
        return _find_linked(doc, spec), None
    if isinstance(spec, dict):
        unknown = sorted(set(spec) - {"object", "document", "subelements"})
        if unknown or "object" not in spec:
            raise _EditError(
                "%s: a link is written {\"object\": name, \"subelements\": [...], "
                "\"document\": name}; got keys %s" % (label, sorted(spec))
            )
        target_doc = _find_doc(spec["document"]) if spec.get("document") else doc
        subs = spec.get("subelements")
        return (
            _find_linked(target_doc, spec["object"]),
            None if subs is None else _subelements(subs, label),
        )
    if (
        isinstance(spec, (list, tuple))
        and len(spec) == 2
        and isinstance(spec[0], (str, dict))
        and isinstance(spec[1], (str, list, tuple))
    ):
        target, _ = _reference(doc, spec[0], label)
        return target, _subelements(spec[1], label)
    raise _EditError(
        "%s: expected an object name, [name, [sub-elements]] or "
        "{\"object\": name, \"subelements\": [...]}, got %r" % (label, spec)
    )


def _is_single_reference(value):
    if isinstance(value, (str, dict)):
        return True
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], (str, dict))
        and isinstance(value[1], (list, tuple))
    )


def _convert_link(doc, kind, value, label, notes):
    if kind == "link":
        if value is None:
            return None
        target, subs = _reference(doc, value, label)
        if subs not in (None, [], [""]):
            raise _EditError("%s links a whole object; sub-elements are not allowed" % label)
        return target
    if kind == "list":
        if value is None:
            return []
        items = [value] if isinstance(value, (str, dict)) else value
        if not isinstance(items, (list, tuple)):
            raise _EditError("%s takes a list of object names, got %r" % (label, value))
        result = []
        for item in items:
            target, subs = _reference(doc, item, label)
            if subs not in (None, [], [""]):
                raise _EditError("%s links whole objects; sub-elements are not allowed" % label)
            result.append(target)
        return result
    if kind == "sub":
        if value is None:
            return None
        target, subs = _reference(doc, value, label)
        return (target, subs or [])
    if kind == "sublist":
        if value is None:
            return []
        items = [value] if _is_single_reference(value) else value
        if not isinstance(items, (list, tuple)):
            raise _EditError("%s takes a list of links, got %r" % (label, value))
        result = []
        for item in items:
            target, subs = _reference(doc, item, label)
            if subs is not None and len(subs) == 0:
                notes.append(
                    "%s: empty sub-element list for %s read as [\"\"] (the whole "
                    "object); FreeCAD drops an entry with no sub-elements"
                    % (label, target.Name)
                )
            result.append((target, subs or [""]))
        return result
    # xlink: one object, optionally in another document, with optional sub-elements
    if value is None:
        return None
    target, subs = _reference(doc, value, label)
    if not subs or subs == [""]:
        return target
    return (target, subs[0] if len(subs) == 1 else subs)


def _vector(value, label):
    if (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    ):
        return FreeCAD.Vector(*value)
    raise _EditError("%s takes a vector [x, y, z], got %r" % (label, value))


def _placement(value, current, label):
    """Accept {"position", "rotation"} / {"base", "axis", "angle"} or [pos, ypr]."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        value = {"position": value[0], "rotation": value[1]}
    if not isinstance(value, dict):
        raise _EditError(
            "%s takes {\"position\": [x, y, z], \"rotation\": [yaw, pitch, roll]} "
            "or {\"position\": [x, y, z], \"axis\": [x, y, z], \"angle\": degrees}, "
            "got %r" % (label, value)
        )
    allowed = {"position", "base", "rotation", "axis", "angle"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _EditError("%s: unknown placement keys %s; use %s" % (label, unknown, sorted(allowed)))
    position = value.get("position", value.get("base"))
    base = current.Base if position is None else _vector(position, label + " position")
    if value.get("rotation") is not None:
        ypr = value["rotation"]
        if not (isinstance(ypr, (list, tuple)) and len(ypr) == 3):
            raise _EditError("%s rotation takes [yaw, pitch, roll] in degrees, got %r" % (label, ypr))
        rotation = FreeCAD.Rotation(*[float(v) for v in ypr])
    elif value.get("axis") is not None or value.get("angle") is not None:
        axis = _vector(value.get("axis", [0, 0, 1]), label + " axis")
        rotation = FreeCAD.Rotation(axis, float(value.get("angle", 0.0)))
    else:
        rotation = current.Rotation
    return FreeCAD.Placement(base, rotation)


def _root(path):
    return path.lstrip(".").split(".")[0].split("[")[0]


def _expressions(obj):
    try:
        return dict(obj.ExpressionEngine)
    except Exception:
        return {}


def _expression_key(exprs, path):
    for key in (path, "." + path.lstrip("."), path.lstrip(".")):
        if key in exprs:
            return key
    return None


def _safe(value, doc, depth=0):
    """Convert a stored value to XML-RPC/JSON-safe data, links as names."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -(2**31) <= value < 2**31 else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _xml_text(value)
    if _is_doc_object(value):
        if value.Document is not doc:
            return {"object": _xml_text(value.Name), "document": _xml_text(value.Document.Name)}
        return _xml_text(value.Name)
    if depth > 4:
        return _xml_text(value)[:200]
    if isinstance(value, FreeCAD.Vector):
        return [round(value.x, 9), round(value.y, 9), round(value.z, 9)]
    if isinstance(value, FreeCAD.Placement):
        return {
            "position": _safe(value.Base, doc, depth + 1),
            "rotation": [round(v, 9) for v in value.Rotation.toEuler()],
        }
    if hasattr(value, "UserString") and hasattr(value, "Unit"):
        return _xml_text(value)
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and _is_doc_object(value[0]) and isinstance(value[1], (str, list, tuple)):
            subs = [value[1]] if isinstance(value[1], str) else list(value[1])
            linked = _safe(value[0], doc, depth + 1)
            if isinstance(linked, dict):
                linked["subelements"] = [_xml_text(s) for s in subs]
                return linked
            return {"object": linked, "subelements": [_xml_text(s) for s in subs]}
        items = [_safe(v, doc, depth + 1) for v in list(value)[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            items.append("<%d more>" % (len(value) - _MAX_ITEMS))
        return items
    if isinstance(value, dict):
        return {_xml_text(k): _safe(v, doc, depth + 1) for k, v in list(value.items())[:_MAX_ITEMS]}
    return _xml_text(value)[:200]


def _read_path(obj, path):
    value = obj
    for part in path.lstrip(".").split("."):
        value = getattr(value, part)
    return value


def _undo_name(obj, names):
    if len(names) <= 3:
        return "Edit %s.%s" % (obj.Label, ", ".join(names))
    return "Edit %s (%d properties)" % (obj.Label, len(names))


def _edit():
    doc = _find_doc(_doc_name)
    obj = doc.getObject(_obj_name)
    if obj is None:
        raise _missing_object(doc, _obj_name)
    if not isinstance(_properties, dict):
        raise _EditError("properties must be an object mapping property names to values")
    prop_names = list(obj.PropertiesList)

    # 1. Check every name before changing anything.
    unknown = []
    for path in _properties:
        if not isinstance(path, str) or _root(path) not in prop_names:
            unknown.append(path)
    if unknown:
        lowered = {p.lower(): p for p in prop_names}
        parts = []
        for path in unknown:
            root = _root(str(path))
            close = difflib.get_close_matches(root, prop_names, n=3, cutoff=0.6)
            if root.lower() in lowered and lowered[root.lower()] not in close:
                close.insert(0, lowered[root.lower()])
            text = repr(path)
            if close:
                text += " (did you mean %s?)" % ", ".join(close)
            view = getattr(obj, "ViewObject", None)
            if view is not None and root in getattr(view, "PropertiesList", []):
                text += " (a display property; use the view tools such as set_object_color)"
            parts.append(text)
        raise _EditError(
            "%s (%s) has no property %s. Nothing was changed."
            % (obj.Name, obj.TypeId, "; ".join(parts))
        )

    # 2. Convert every value before changing anything.
    notes = []
    plan = []  # (path, kind, payload)
    for path, value in _properties.items():
        label = "%s.%s" % (obj.Name, path)
        if isinstance(value, str) and value.startswith("="):
            expression = value[1:].strip()
            plan.append((path, "expression", expression or None))
            continue
        if "." in path.lstrip(".") or path.startswith("."):
            raise _EditError(
                "%s: sub-property paths only take expressions (a value starting "
                "with \"=\"); set the whole property %r instead. Nothing was changed."
                % (label, _root(path))
            )
        ptype = obj.getTypeIdOfProperty(path)
        kind = _link_kind(ptype)
        if kind is not None:
            converted = _convert_link(doc, kind, value, label, notes)
        else:
            current = getattr(obj, path)
            if isinstance(current, FreeCAD.Placement) and not isinstance(value, FreeCAD.Placement):
                converted = _placement(value, current, label)
            elif isinstance(current, FreeCAD.Vector) and isinstance(value, (list, tuple)):
                converted = _vector(value, label)
            elif ptype == "App::PropertyVectorList" and isinstance(value, (list, tuple)):
                converted = [_vector(v, label) for v in value]
            else:
                if (
                    ptype == "App::PropertyEnumeration"
                    and isinstance(value, str)
                    and hasattr(obj, "getEnumerationsOfProperty")
                ):
                    allowed = list(obj.getEnumerationsOfProperty(path) or [])
                    if allowed and value not in allowed:
                        raise _EditError(
                            "%s: %r is not one of %s. Nothing was changed."
                            % (label, value, ", ".join(allowed))
                        )
                converted = value
        plan.append((path, "value", converted))

    if not plan:
        return obj, {}, notes

    # 3. Apply everything in one undo step; undo it all on any failure.
    doc.openTransaction(_undo_name(obj, list(_properties)))
    try:
        for path, action, payload in plan:
            label = "%s.%s" % (obj.Name, path)
            try:
                if action == "expression":
                    if payload is None:
                        if _expression_key(_expressions(obj), path) is not None:
                            obj.setExpression(_expression_key(_expressions(obj), path), None)
                    else:
                        obj.setExpression(path, payload)
                    continue
                exprs = _expressions(obj)
                for key in list(exprs):
                    if _root(key) == path:
                        obj.setExpression(key, None)
                        notes.append(
                            "%s: removed expression %r so the new value is kept"
                            % ("%s.%s" % (obj.Name, key.lstrip(".")), exprs[key])
                        )
                setattr(obj, path, payload)
            except _EditError:
                raise
            except Exception as exc:
                hint = ""
                if "not saved" in str(exc):
                    hint = " (FreeCAD links across documents only after both are saved)"
                raise _EditError(
                    "Could not set %s (%s): %s%s. Nothing was changed."
                    % (label, obj.getTypeIdOfProperty(_root(path)), exc, hint)
                )
        doc.recompute()
        doc.commitTransaction()
    except Exception:
        doc.abortTransaction()
        raise

    # 4. Read back what FreeCAD actually stored.
    changes = {}
    exprs = _expressions(obj)
    for path, action, _payload in plan:
        key = _expression_key(exprs, path)
        try:
            stored = _safe(_read_path(obj, path), doc)
        except Exception as exc:
            stored = "<unreadable: %s>" % _xml_text(exc)
        if action == "expression" or key is not None:
            changes[_xml_text(path)] = {
                "expression": _xml_text(exprs[key]) if key is not None else None,
                "value": stored,
            }
        else:
            changes[_xml_text(path)] = stored
    if hasattr(obj, "isValid") and not obj.isValid():
        status = obj.getStatusString() if hasattr(obj, "getStatusString") else ""
        notes.append("%s is invalid after recompute: %s" % (obj.Name, status or "unknown error"))
    return obj, changes, notes


try:
    _obj, _changes, _notes = _edit()
except _EditError as _exc:
    _result_ = {"error": _xml_text(_exc)}
else:
    _result_ = {
        "name": _xml_text(_obj.Name),
        "label": _xml_text(_obj.Label),
        "type_id": _xml_text(_obj.TypeId),
        "properties": _changes,
        "notes": [_xml_text(n) for n in _notes],
        "visibility": bool(_obj.ViewObject.Visibility) if getattr(_obj, "ViewObject", None) else True,
        "children": [_xml_text(c.Name) for c in _obj.OutList],
        "parents": [_xml_text(p.Name) for p in _obj.InList],
    }
'''

_DELETE_PROGRAM = r"""
def _body_of(obj):
    for parent in obj.InList:
        if parent.TypeId == "PartDesign::Body" and obj in parent.Group:
            return parent
    return None


def _delete():
    doc = _find_doc(_doc_name)
    obj = doc.getObject(_obj_name)
    if obj is None:
        raise _missing_object(doc, _obj_name)
    name, label = obj.Name, obj.Label
    report = {"deleted": name, "label": label, "notes": []}
    body = _body_of(obj)
    doc.openTransaction("Delete " + label)
    try:
        if body is not None:
            old_tip = body.Tip
            previous = getattr(obj, "BaseFeature", None)
            following = [o for o in body.Group if getattr(o, "BaseFeature", None) is obj]
            # Body.removeObject moves the Tip back and relinks the next feature,
            # as deleting in the FreeCAD GUI does.
            body.removeObject(obj)
            if body.Tip is obj or (old_tip is obj and body.Tip is None and previous is not None):
                body.Tip = previous
            report["body"] = body.Name
            report["tip"] = body.Tip.Name if body.Tip is not None else None
            if old_tip is obj:
                if body.Tip is not None:
                    report["notes"].append(
                        "%s was the Tip of %s; the Tip is now %s"
                        % (name, body.Name, body.Tip.Name)
                    )
                else:
                    report["notes"].append(
                        "%s was the Tip of %s and no earlier feature exists; "
                        "the body is now empty" % (name, body.Name)
                    )
            for feature in following:
                base = getattr(feature, "BaseFeature", None)
                report["notes"].append(
                    "%s now builds on %s"
                    % (feature.Name, base.Name if base is not None else "nothing")
                )
        dependents = [
            o.Name for o in obj.InList
            if o is not body and obj not in (getattr(o, "Group", None) or [])
        ]
        doc.removeObject(name)
        doc.recompute()
        doc.commitTransaction()
    except Exception:
        doc.abortTransaction()
        raise
    if dependents:
        report["notes"].append(
            "These objects linked to %s and now have an empty link: %s"
            % (name, ", ".join(sorted(dependents)))
        )
    if body is not None and doc.getObject(report["body"]) is not None:
        shape = getattr(body, "Shape", None)
        report["body_shape_valid"] = bool(
            shape is not None and not shape.isNull() and shape.isValid()
        )
    return report


try:
    _delete_report = _delete()
except _EditError as _exc:
    _result_ = {"error": _xml_text(_exc)}
else:
    _result_ = {
        key: ([_xml_text(n) for n in value] if key == "notes" else
              _xml_text(value) if isinstance(value, str) else value)
        for key, value in _delete_report.items()
    }
"""


@dataclass
class ObjectEditResult(ObjectInfo):
    """Result of an edit: the object plus what FreeCAD stored.

    Attributes:
        notes: Things the caller should know about the edit, such as a value
            that was normalised or an expression that was removed.
    """

    notes: list[str] = field(default_factory=list)


def build_edit_object_code(
    obj_name: str, properties: dict[str, Any], doc_name: str | None
) -> str:
    """Build the FreeCAD-side program that edits one object's properties.

    Args:
        obj_name: Internal name of the object to edit.
        properties: Property names mapped to JSON values (see the
            ``edit_object`` tool for the accepted forms).
        doc_name: Document name, or None for the active document.

    Returns:
        Python source to execute inside FreeCAD.

    Raises:
        ValueError: If properties is not a mapping of JSON data.
    """
    if not isinstance(properties, dict):
        raise ValueError("properties must be a dictionary of property values")
    try:
        properties_json = json.dumps(properties, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"properties must be JSON data: {exc}") from exc
    return (
        "import json\n"
        f"_obj_name = {obj_name!r}\n"
        f"_doc_name = {doc_name!r}\n"
        f"_properties = json.loads({properties_json!r})\n"
        + _COMMON_HELPERS
        + _EDIT_PROGRAM
    )


def build_delete_object_code(obj_name: str, doc_name: str | None) -> str:
    """Build the FreeCAD-side program that deletes one object.

    Args:
        obj_name: Internal name of the object to delete.
        doc_name: Document name, or None for the active document.

    Returns:
        Python source to execute inside FreeCAD.
    """
    return (
        f"_obj_name = {obj_name!r}\n"
        f"_doc_name = {doc_name!r}\n" + _COMMON_HELPERS + _DELETE_PROGRAM
    )


def _payload(result: ExecutionResult, default_error: str) -> dict[str, Any]:
    """Return the program's report, or raise its error as one sentence."""
    if result.success and isinstance(result.result, dict):
        if "error" in result.result:
            raise ValueError(str(result.result["error"]))
        return result.result
    raise ValueError(result.error_traceback or result.stderr or default_error)


def edit_result_from_execution(result: ExecutionResult) -> ObjectEditResult:
    """Turn the edit program's output into an ObjectEditResult.

    Args:
        result: Execution result of :func:`build_edit_object_code`'s program.

    Returns:
        The edited object, with ``properties`` holding the stored values.

    Raises:
        ValueError: If the edit was refused or failed.
    """
    return ObjectEditResult(**_payload(result, "Failed to edit object"))


def delete_result_from_execution(result: ExecutionResult) -> dict[str, Any]:
    """Turn the delete program's output into a report dictionary.

    Args:
        result: Execution result of :func:`build_delete_object_code`'s program.

    Returns:
        Report with ``deleted``, ``label``, ``notes`` and, for PartDesign body
        features, ``body``, ``tip`` and ``body_shape_valid``.

    Raises:
        ValueError: If the object was not found or the delete failed.
    """
    return _payload(result, "Failed to delete object")
