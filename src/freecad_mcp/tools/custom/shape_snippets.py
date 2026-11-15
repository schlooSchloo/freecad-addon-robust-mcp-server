"""FreeCAD-side helper code shared by several object and sketch tools.

Tools build FreeCAD Python code as text and send it to the bridge. The
constants here hold helper functions, as code text, that a tool prepends to
its own code. They contain no user values: tools pass user strings to these
helpers as ``{value!r}`` literals in their own code, so the generated code
stays injection-safe.

Kept on the fork's ``custom`` branch, apart from upstream's tool modules, so
that upstream merges rarely conflict.

- SHAPE_REPORT_CODE: ``_mcp_shape_report(shape)`` summarises a shape (type,
  validity, closed, solid count, volume).
- OUTLINE_CODE: ``_mcp_outline(shape, max_edges)`` lists a shape's wires and
  edges (curve type, end points, length, centre and radius for circles).
- SKETCH_PLANE_CODE: ``_mcp_resolve_sketch_plane(doc, body, plane)`` finds the
  object a new sketch should attach to.
"""

SHAPE_REPORT_CODE = '''
def _mcp_shape_report(shape):
    """Summarise a shape for a tool result."""
    report = {
        "shape_type": None,
        "is_null": True,
        "valid": False,
        "closed": None,
        "solid_count": 0,
        "shell_count": 0,
        "volume": None,
    }
    if shape is None or shape.isNull():
        return report
    solids = shape.Solids
    report["shape_type"] = shape.ShapeType
    report["is_null"] = False
    report["valid"] = bool(shape.isValid())
    report["solid_count"] = len(solids)
    report["shell_count"] = len(shape.Shells)
    if solids:
        report["volume"] = float(sum(s.Volume for s in solids))
        report["closed"] = all(sh.isClosed() for s in solids for sh in s.Shells)
    else:
        try:
            report["closed"] = bool(shape.isClosed())
        except Exception:
            report["closed"] = None
    return report
'''

OUTLINE_CODE = '''
def _mcp_vec(v):
    return [round(v.x, 6), round(v.y, 6), round(v.z, 6)]


def _mcp_edge_info(edge):
    """Describe one edge: curve type, end points, length, circle details."""
    import Part

    curve = edge.Curve
    info = {
        "curve_type": type(curve).__name__,
        "length": round(edge.Length, 6),
        "closed": bool(edge.isClosed()),
    }
    if edge.Vertexes:
        info["start"] = _mcp_vec(edge.Vertexes[0].Point)
        info["end"] = _mcp_vec(edge.Vertexes[-1].Point)
    if isinstance(curve, Part.Circle):
        info["curve_type"] = "Circle" if edge.isClosed() else "ArcOfCircle"
        info["center"] = _mcp_vec(curve.Center)
        info["radius"] = round(curve.Radius, 6)
        info["axis"] = _mcp_vec(curve.Axis)
    elif isinstance(curve, Part.Ellipse):
        info["curve_type"] = "Ellipse" if edge.isClosed() else "ArcOfEllipse"
        info["center"] = _mcp_vec(curve.Center)
        info["major_radius"] = round(curve.MajorRadius, 6)
        info["minor_radius"] = round(curve.MinorRadius, 6)
        info["axis"] = _mcp_vec(curve.Axis)
    return info


def _mcp_outline(shape, max_edges):
    """List a shape's wires and edges, capped at max_edges edges in total."""
    wires = list(shape.Wires)
    wired_edges = sum(len(w.Edges) for w in wires)
    loose = []
    if wired_edges < len(shape.Edges):
        # Edges that belong to no wire (rare for sections) are listed apart.
        seen = [e for w in wires for e in w.Edges]
        loose = [e for e in shape.Edges if not any(e.isSame(s) for s in seen)]
    total = wired_edges + len(loose)
    budget = max_edges
    wire_list = []
    for index, wire in enumerate(wires):
        if budget <= 0:
            break
        edges = []
        ordered = getattr(wire, "OrderedEdges", None) or wire.Edges
        for edge in ordered:
            if budget <= 0:
                break
            edges.append(_mcp_edge_info(edge))
            budget -= 1
        wire_list.append({
            "index": index,
            "closed": bool(wire.isClosed()),
            "length": round(wire.Length, 6),
            "edge_count": len(wire.Edges),
            "edges": edges,
        })
    loose_list = []
    for edge in loose:
        if budget <= 0:
            break
        loose_list.append(_mcp_edge_info(edge))
        budget -= 1
    returned = max_edges - budget
    outline = {
        "wire_count": len(wires),
        "edge_count": total,
        "edges_returned": returned,
        "truncated": returned < total,
        "wires": wire_list,
    }
    if loose:
        outline["loose_edges"] = loose_list
    if returned < total:
        outline["note"] = (
            "Only the first " + str(returned) + " of " + str(total)
            + " edges are listed; raise max_edges to see more."
        )
    return outline
'''

SKETCH_PLANE_CODE = '''
_MCP_PLANE_ROLES = {
    "XY_PLANE": "XY_Plane",
    "XY": "XY_Plane",
    "XZ_PLANE": "XZ_Plane",
    "XZ": "XZ_Plane",
    "YZ_PLANE": "YZ_Plane",
    "YZ": "YZ_Plane",
}


def _mcp_is_plane_like(obj):
    """True for origin planes and PartDesign or Part datum planes."""
    for type_name in ("App::Plane", "PartDesign::Plane", "Part::DatumPlane"):
        try:
            if obj.isDerivedFrom(type_name):
                return True
        except Exception:
            # Part::DatumPlane is not a known type in every FreeCAD version.
            pass
    return False


def _mcp_origin_planes(body):
    origin = getattr(body, "Origin", None)
    features = list(getattr(origin, "OriginFeatures", None) or []) if origin else []
    return [f for f in features if f.isDerivedFrom("App::Plane")]


def _mcp_origin_plane(body, role):
    """Find a body's origin plane by its role, whatever it is numbered."""
    planes = _mcp_origin_planes(body)
    for feature in planes:
        if getattr(feature, "Role", None) == role:
            return feature
    for feature in planes:
        suffix = feature.Name[len(role):]
        if feature.Name.startswith(role) and (suffix == "" or suffix.isdigit()):
            return feature
    return None


def _mcp_available_planes(doc, body):
    names = []
    if body is not None:
        names += [
            str(getattr(f, "Role", f.Name)) + " (" + f.Name + ")"
            for f in _mcp_origin_planes(body)
        ]
    others = [
        o.Name for o in doc.Objects
        if _mcp_is_plane_like(o) and not o.isDerivedFrom("App::Plane")
    ]
    names += others[:20]
    return ", ".join(names) if names else "none"


def _mcp_resolve_sketch_plane(doc, body, plane):
    """Return (support_object, sub_element, kind, role) for a new sketch.

    support_object is None for a standalone sketch on a standard plane, which
    is placed rather than attached. Raises ValueError if nothing matches.
    """
    import re

    role = _MCP_PLANE_ROLES.get(plane.strip().upper())
    if role is not None:
        if body is None:
            return None, "", "standard_plane", role
        feature = _mcp_origin_plane(body, role)
        if feature is None:
            raise ValueError(
                "Body " + repr(body.Name) + " has no " + role + " origin plane"
            )
        return feature, "", "origin_plane", role
    if re.fullmatch(r"Face[0-9]+", plane):
        if body is None:
            raise ValueError(
                "Attaching a sketch to " + plane + " needs body_name: the face "
                "is looked up on that body's shape."
            )
        face_count = len(body.Shape.Faces) if not body.Shape.isNull() else 0
        if not 1 <= int(plane[4:]) <= face_count:
            raise ValueError(
                "Body " + repr(body.Name) + " has no " + plane + " (it has "
                + str(face_count) + " faces)"
            )
        return body, plane, "body_face", None
    target = doc.getObject(plane)
    if target is None:
        matches = doc.getObjectsByLabel(plane)
        if len(matches) > 1:
            raise ValueError(
                "Several objects have the label " + repr(plane)
                + "; use the object name instead: "
                + ", ".join(m.Name for m in matches)
            )
        target = matches[0] if matches else None
    if target is None or not _mcp_is_plane_like(target):
        if target is None:
            problem = "No plane named " + repr(plane) + "."
        else:
            problem = repr(plane) + " is a " + target.TypeId + ", not a plane."
        raise ValueError(
            problem
            + " Use XY_Plane, XZ_Plane, YZ_Plane, FaceN (with body_name) or "
            "the name of a datum plane. Available planes: "
            + _mcp_available_planes(doc, body)
        )
    if body is not None:
        parent = target.getParentGeoFeatureGroup()
        if parent is not None and parent.Name != body.Name:
            hint = ""
            target_role = getattr(target, "Role", None)
            if target_role:
                hint = (
                    " To use this body's own " + str(target_role) + ", pass plane="
                    + repr(str(target_role)) + "."
                )
            raise ValueError(
                repr(target.Name) + " belongs to " + repr(parent.Name)
                + ", not to body " + repr(body.Name) + ". A sketch in a body "
                "can only attach to geometry in the same body." + hint
            )
    target_role = None
    if target.isDerivedFrom("App::Plane"):
        target_role = getattr(target, "Role", None) or None
    kind = "origin_plane" if target_role else "datum_plane"
    return target, "", kind, target_role
'''
