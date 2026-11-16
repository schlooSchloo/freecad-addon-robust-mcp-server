"""PartDesign reference tools: binders, cross-body projection, attached datums.

A sketch or datum in one PartDesign body can't use another body's geometry
directly ("out of allowed scope"). The usual fix is a SubShapeBinder in the
target body, then projecting or attaching to the binder. These tools do that
in one call each, as a single undo step. The FreeCAD-side code is in the
workbench's ``custom_runtime/references.py``.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from freecad_mcp.tools.custom.runtime import run_operation


def _check_triple(values: list[float] | None, label: str) -> None:
    """Raise ValueError unless ``values`` is None or three numbers."""
    if values is not None and len(values) != 3:
        raise ValueError(f"{label} must be a list of three numbers")


def register_references_tools(
    mcp: Any, get_bridge: Callable[[], Awaitable[Any]]
) -> None:
    """Register the binder, projection and attached datum tools.

    Args:
        mcp: The MCPServer instance.
        get_bridge: Async function returning the active bridge.
    """

    @mcp.tool()
    async def create_subshape_binder(
        body_name: str,
        sources: list[str],
        name: str | None = None,
        bind_mode: Literal["synchronized", "frozen", "detached"] = "synchronized",
        relative: bool = True,
        make_face: bool = True,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Bind objects or sub-elements from anywhere into a PartDesign body.

        Creates a PartDesign::SubShapeBinder, the usual way to use another
        body's geometry (sketches, attachments, pads). Sources may be in
        other bodies or, if both documents are saved, other documents.

        Args:
            body_name: Body to put the binder in.
            sources: "Object" (whole shape), "Object.Edge3", or
                "Document#Object.Face2". A body means its tip shape.
            name: Binder name. Defaults to "Binder".
            bind_mode: synchronized (follows changes), frozen (keeps this
                shape until refreshed by hand) or detached (static copy,
                links removed).
            relative: Keep the source's global position, compensating both
                bodies' placements (default). False copies the geometry in
                the source's own coordinates, unpositioned.
            make_face: Make faces from closed wires (FreeCAD default).
            doc_name: Document of the body. Active document if None.

        Returns:
            name, label, support (links made), bind_mode, shape (type,
            counts, area mm², volume mm³, global bound_box),
            source_bound_box and matches_source_position.
        """
        if not sources:
            raise ValueError("Give at least one source")
        return await run_operation(
            get_bridge,
            "create_subshape_binder",
            {
                "body": body_name,
                "sources": sources,
                "name": name,
                "bind_mode": bind_mode,
                "relative": relative,
                "make_face": make_face,
                "doc_name": doc_name,
            },
            transaction=f"Create binder in {body_name}",
            doc_name=doc_name,
        )

    @mcp.tool()
    async def project_into_sketch(
        sketch_name: str,
        source_object: str,
        elements: list[str],
        defining: bool = False,
        intersection: bool = False,
        reuse_binder: bool = True,
        max_geometry: int = 50,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Project edges, faces or vertices of any object into a sketch.

        If the source is outside the sketch's body, links it through a
        SubShapeBinder in that body (reusing a live one that already holds
        the elements, else creating a hidden one before the sketch), which
        avoids FreeCAD's "out of allowed scope" error.

        Args:
            sketch_name: Target sketch.
            source_object: Object holding the elements, e.g. "Pad" or
                "Document#Pad".
            elements: Sub-elements of the source, e.g. ["Edge3", "Face2"].
            defining: Make the projected edges part of the sketch profile
                instead of construction lines (FreeCAD 1.1+).
            intersection: Use where the element cuts the sketch plane
                instead of its projection (FreeCAD 1.1+).
            reuse_binder: Reuse a matching existing binder.
            max_geometry: Most geometry entries to describe.
            doc_name: Document of the sketch. Active document if None.

        Returns:
            route ("direct" or "binder"), binder (name, created, element
            map), projected (per element: geo_ids for constraints and each
            geometry in sketch coordinates, mm), already_projected and
            truncated.
        """
        if not elements:
            raise ValueError("Give at least one element, e.g. ['Edge3']")
        if max_geometry < 1:
            raise ValueError("max_geometry must be at least 1")
        return await run_operation(
            get_bridge,
            "project_into_sketch",
            {
                "sketch": sketch_name,
                "source": source_object,
                "elements": elements,
                "defining": defining,
                "intersection": intersection,
                "reuse_binder": reuse_binder,
                "max_geometry": max_geometry,
                "doc_name": doc_name,
            },
            transaction=f"Project {source_object} into {sketch_name}",
            doc_name=doc_name,
        )

    @mcp.tool()
    async def create_attached_datum(
        body_name: str,
        attach_to: list[str],
        mode: str | None = None,
        kind: Literal["plane", "line", "point"] = "plane",
        offset: list[float] | None = None,
        rotation: list[float] | None = None,
        path_parameter: float = 0.0,
        reverse: bool = False,
        name: str | None = None,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Create a datum plane, line or point attached to body geometry.

        Uses FreeCAD's attachment modes, e.g. FlatFace, NormalToEdge,
        ThreePointsPlane, TangentPlane, ObjectXY (planes); TwoPointLine,
        Tangent (lines); Vertex, OnEdge (points). An unsuitable mode is
        refused with the modes that fit. References must be in the same
        body: bind other bodies' geometry first with create_subshape_binder.

        Args:
            body_name: Body for the datum.
            attach_to: Ordered references: "Pad.Edge3", "Binder.Face1",
                "DatumPlane", or origin roles "XY_Plane", "Z_Axis", "Origin".
            mode: Attachment mode. FreeCAD's best fit if None.
            kind: plane, line or point.
            offset: [x, y, z] mm in the attached coordinate system.
            rotation: [yaw, pitch, roll] degrees about Z, Y, X (as
                set_placement), applied in the attached coordinate system.
            path_parameter: Position along an edge for edge modes, 0 = start,
                1 = end.
            reverse: Flip the attachment. NormalToEdge normals point against
                the edge direction unless reversed.
            name: Datum name.
            doc_name: Document. Active document if None.

        Returns:
            name, mode, attached_to, global position (mm), normal and x_axis
            (plane) or direction (line), and in_body values if the body is
            moved.
        """
        _check_triple(offset, "offset")
        _check_triple(rotation, "rotation")
        if not attach_to:
            raise ValueError("Give at least one reference to attach to")
        return await run_operation(
            get_bridge,
            "create_attached_datum",
            {
                "body": body_name,
                "attach_to": attach_to,
                "mode": mode,
                "kind": kind,
                "offset": offset,
                "rotation": rotation,
                "path_parameter": path_parameter,
                "reverse": reverse,
                "name": name,
                "doc_name": doc_name,
            },
            transaction=f"Create datum {kind} in {body_name}",
            doc_name=doc_name,
        )
