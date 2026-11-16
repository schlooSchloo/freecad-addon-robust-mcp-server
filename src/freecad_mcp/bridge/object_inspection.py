"""Build the common FreeCAD-side object inspection program.

The program runs inside FreeCAD, which need not have the MCP package installed.
Keeping it here ensures all bridge modes apply the same wire-safe conversion.
"""

_INSPECTION_PROGRAM = r"""
import math

doc = FreeCAD.ActiveDocument if _doc_name is None else FreeCAD.getDocument(_doc_name)
if doc is None:
    raise ValueError("No document found")
obj = doc.getObject(_obj_name)
if obj is None:
    raise ValueError("Object not found: " + _obj_name)

# XML-RPC accepts only 32-bit integers and XML 1.0 text. Use the same
# representation for every transport and bound the work and output size.
_remaining_nodes = [512]
_remaining_chars = [2048]
_max_items = 100
_max_depth = 8
# Set when any property name or value had to be shortened or left out.
_truncated = [False]

def _xml_text(value):
    return "".join(
        char if (ord(char) in (9, 10, 13) or 32 <= ord(char) <= 0xD7FF
                 or 0xE000 <= ord(char) <= 0xFFFD
                 or 0x10000 <= ord(char) <= 0x10FFFF) else "\uFFFD"
        for char in str(value)
    )

def _text(value):
    value = str(value)
    limit = min(512, _remaining_chars[0])
    truncated = len(value) > limit
    value = value[:limit]
    _remaining_chars[0] -= len(value)
    value = _xml_text(value)
    if truncated:
        _truncated[0] = True
    return value + ("<truncated>" if truncated else "")

def _mark_truncated(mapping):
    _truncated[0] = True
    key = "<truncated>"
    while key in mapping:
        key += "_"
    mapping[key] = True

def _safe(value, depth=0, active=None):
    if _remaining_nodes[0] <= 0:
        _truncated[0] = True
        return "<truncated>"
    _remaining_nodes[0] -= 1
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value if -(2**31) <= value < 2**31 else _text(value)
    if type(value) is float:
        return value if math.isfinite(value) else _text(value)
    if type(value) is str:
        return _text(value)
    if depth >= _max_depth:
        _truncated[0] = True
        return "<max depth>"
    if active is None:
        active = set()
    if type(value) in (list, tuple, dict):
        identity = id(value)
        if identity in active:
            return "<cycle>"
        active.add(identity)
        try:
            if type(value) is dict:
                result = {}
                for index, (key, item) in enumerate(value.items()):
                    if index >= _max_items or _remaining_nodes[0] <= 0 or _remaining_chars[0] <= 0:
                        _mark_truncated(result)
                        break
                    safe_key = _text(key)
                    if safe_key in result:
                        _mark_truncated(result)
                        break
                    result[safe_key] = _safe(item, depth + 1, active)
                return result
            result = []
            for index, item in enumerate(value):
                if index >= _max_items or _remaining_nodes[0] <= 0 or _remaining_chars[0] <= 0:
                    _truncated[0] = True
                    result.append("<truncated>")
                    break
                result.append(_safe(item, depth + 1, active))
            return result
        finally:
            active.remove(identity)
    try:
        if value.isDerivedFrom("App::DocumentObject"):
            linked_doc = value.Document
            return {
                "name": _text(value.Name),
                "label": _text(value.Label),
                "type_id": _text(value.TypeId),
                "document": _text(linked_doc.Name) if linked_doc else None,
            }
    except (AttributeError, TypeError):
        pass
    return _text(value)

props = {}
if _include_properties:
    _names = list(obj.PropertiesList)
    if _property_names is not None:
        _missing = [name for name in _property_names if name not in _names]
        if _missing:
            raise ValueError(
                "Object " + _xml_text(obj.Name) + " has no properties named: "
                + ", ".join(_xml_text(name) for name in _missing)
            )
        _names = list(dict.fromkeys(_property_names))
    for index, prop in enumerate(_names):
        if index >= _max_items or _remaining_nodes[0] <= 0 or _remaining_chars[0] <= 0:
            _mark_truncated(props)
            break
        try:
            key = _text(prop)
            if key != prop or key in props:
                _mark_truncated(props)
                break
            props[key] = _safe(getattr(obj, prop))
        except Exception:
            props[prop] = "<unreadable>"

def _finite(value):
    value = float(value)
    return value if math.isfinite(value) else None

def _vector(vector):
    return [_finite(vector.x), _finite(vector.y), _finite(vector.z)]

def _placement(placement):
    rotation = placement.Rotation
    return {
        "position": _vector(placement.Base),
        "axis": _vector(rotation.Axis),
        "angle_degrees": _finite(math.degrees(rotation.Angle)),
        "yaw_pitch_roll": [_finite(v) for v in rotation.toEuler()],
    }

def _box(box):
    return {
        "min": [_finite(box.XMin), _finite(box.YMin), _finite(box.ZMin)],
        "max": [_finite(box.XMax), _finite(box.YMax), _finite(box.ZMax)],
        "size": [_finite(box.XLength), _finite(box.YLength), _finite(box.ZLength)],
    }

def _bound_box(shape):
    # The exact box; BoundBox can be loose around curves.
    if hasattr(shape, "optimalBoundingBox"):
        box = _attempt(lambda: shape.optimalBoundingBox(False, False))
        if box is not None and box.isValid():
            return box
    return shape.BoundBox

def _attempt(function, default=None):
    try:
        return function()
    except Exception:
        return default

def _is_unbounded(shape):
    if hasattr(shape, "isInfinite"):
        result = _attempt(shape.isInfinite)
        if result is not None:
            return bool(result)
    box = shape.BoundBox
    return max(abs(box.XMin), abs(box.XMax), abs(box.YMin), abs(box.YMax),
               abs(box.ZMin), abs(box.ZMax)) >= 1e90

def _unbounded_geometry(shape):
    # Position and direction of an infinite datum plane or line.
    faces = list(shape.Faces)
    edges = list(shape.Edges)
    if len(faces) == 1:
        surface = faces[0].Surface
        if hasattr(surface, "Axis") and hasattr(surface, "Position"):
            return {"plane": {"position": _vector(surface.Position),
                              "normal": _vector(surface.Axis)}}
    if not faces and len(edges) == 1:
        curve = edges[0].Curve
        if hasattr(curve, "Direction") and hasattr(curve, "Location"):
            return {"line": {"position": _vector(curve.Location),
                             "direction": _vector(curve.Direction)}}
    return {}

def _shape_details(shape):
    solids = list(shape.Solids)
    shells = list(shape.Shells)
    unbounded = _is_unbounded(shape)
    info = {
        "shape_type": shape.ShapeType,
        "volume": None,
        "area": None,
        "is_valid": shape.isValid(),
        "is_closed": _attempt(shape.isClosed, False) if hasattr(shape, "isClosed") else False,
        "is_solid": shape.ShapeType == "Solid",
        "all_shells_closed": (
            all(_attempt(s.isClosed, False) for s in shells) if shells else None
        ),
        "solid_count": len(solids),
        "shell_count": len(shells),
        "face_count": len(shape.Faces),
        "edge_count": len(shape.Edges),
        "vertex_count": len(shape.Vertexes),
        "unbounded": unbounded,
        "bounding_box": None,
        "global_bounding_box": None,
        "center_of_mass": None,
        "placement": None,
        "global_placement": None,
    }
    local = _attempt(lambda: obj.Placement)
    global_placement = local
    if hasattr(obj, "getGlobalPlacement"):
        global_placement = _attempt(obj.getGlobalPlacement, local)
    if local is not None:
        info["placement"] = _placement(local)
    if global_placement is not None:
        info["global_placement"] = _placement(global_placement)
    if unbounded:
        info["note"] = (
            "Shape is unbounded (an infinite datum plane or line): area, "
            "volume and bounding box are not applicable"
        )
        info.update(_attempt(lambda: _unbounded_geometry(shape), {}))
        return info
    if info["face_count"]:
        info["area"] = _finite(shape.Area)
    if solids:
        volume = sum(solid.Volume for solid in solids)
        info["volume"] = _finite(volume)
        if volume > 0:
            weighted = [0.0, 0.0, 0.0]
            for solid in solids:
                center = solid.CenterOfMass
                weighted[0] += center.x * solid.Volume
                weighted[1] += center.y * solid.Volume
                weighted[2] += center.z * solid.Volume
            info["center_of_mass"] = [_finite(v / volume) for v in weighted]
    info["bounding_box"] = _box(_bound_box(shape))
    info["global_bounding_box"] = info["bounding_box"]
    if local is not None and global_placement is not None:
        parent = global_placement.multiply(local.inverse())
        if not parent.isIdentity():
            def _global_box():
                moved = shape.copy()
                moved.Placement = parent.multiply(moved.Placement)
                return _box(_bound_box(moved))
            info["global_bounding_box"] = _attempt(_global_box)
            if info["global_bounding_box"] is not None and info["center_of_mass"]:
                center = parent.multVec(FreeCAD.Vector(*info["center_of_mass"]))
                info["global_center_of_mass"] = _vector(center)
    return info

shape_info = None
if _include_shape:
    try:
        shape = getattr(obj, "Shape", None)
        if shape is not None and not (hasattr(shape, "isNull") and shape.isNull()):
            shape_info = _shape_details(shape)
    except Exception:
        shape_info = None

_result_ = {
    "name": _xml_text(obj.Name),
    "label": _xml_text(obj.Label),
    "type_id": _xml_text(obj.TypeId),
    "properties": props,
    "shape_info": shape_info,
    "children": [_xml_text(c.Name) for c in obj.OutList] if hasattr(obj, "OutList") else [],
    "parents": [_xml_text(p.Name) for p in obj.InList] if hasattr(obj, "InList") else [],
    "visibility": obj.ViewObject.Visibility if hasattr(obj, "ViewObject") and obj.ViewObject else True,
    "properties_truncated": _truncated[0],
}
"""


def _names_literal(property_names: list[str] | None) -> str:
    """Return a safe Python literal for the property name filter.

    Args:
        property_names: Property names, or None for all properties.

    Returns:
        ``None`` or a list literal of strings.

    Raises:
        TypeError: A property name is not a string.
    """
    if property_names is None:
        return "None"
    names = list(property_names)
    if not all(isinstance(name, str) for name in names):
        raise TypeError("property_names must be a list of strings")
    return repr(names)


def build_object_inspection_code(
    obj_name: str,
    doc_name: str | None,
    *,
    include_properties: bool = True,
    include_shape: bool = True,
    property_names: list[str] | None = None,
) -> str:
    """Build the FreeCAD-side program for inspecting one object.

    Args:
        obj_name: Internal object name.
        doc_name: Document name, or None for the active document.
        include_properties: Read and serialize object properties.
        include_shape: Read shape details.
        property_names: Read only these properties (in this order). The
            program raises ValueError naming any the object doesn't have.

    Returns:
        Python source to execute inside FreeCAD.
    """
    return (
        f"_obj_name = {obj_name!r}\n"
        f"_doc_name = {doc_name!r}\n"
        f"_include_properties = {include_properties!r}\n"
        f"_include_shape = {include_shape!r}\n"
        f"_property_names = {_names_literal(property_names)}\n" + _INSPECTION_PROGRAM
    )
