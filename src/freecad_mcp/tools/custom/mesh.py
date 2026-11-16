"""Tools for making a native mesh from a shape and checking mesh quality."""

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import ToolAnnotations

from freecad_mcp.tools.custom.runtime import run_operation

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def register_mesh_tools(mcp: Any, get_bridge: Callable[[], Awaitable[Any]]) -> None:
    """Register the mesh tools.

    Args:
        mcp: The MCPServer instance.
        get_bridge: Async function to get the active bridge.
    """

    @mcp.tool()
    async def create_mesh_from_shape(
        source: str,
        name: str | None = None,
        linear_deflection: float = 0.1,
        angular_deflection: float = 30.0,
        relative: bool = False,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Create a native mesh object from a shape, without files.

        Meshes the source's shape (MeshPart) into a new Mesh::Feature in the
        same document. Mesh points are in global coordinates (the source's
        global placement is applied); the mesh's own Placement is identity.
        It is a static snapshot and will not follow later changes to the
        source. One undo step.

        Args:
            source: Name or label of the object to mesh.
            name: Name for the mesh. Default: "<source>_mesh".
            linear_deflection: Largest distance (mm) between mesh and surface,
                or a fraction of edge size if relative.
            angular_deflection: Largest angle (degrees, up to 90) between
                neighbouring facet normals on curved surfaces.
            relative: Treat linear_deflection as relative to edge size.
            doc_name: Document name. Default: the active document.

        Returns:
            Dictionary with mesh (object name), label, facets, points, edges,
            bounds (mm), settings used and placement policy.
        """
        return await run_operation(
            get_bridge,
            "create_mesh_from_shape",
            {
                "source": source,
                "name": name,
                "linear_deflection": linear_deflection,
                "angular_deflection": angular_deflection,
                "relative": relative,
                "doc_name": doc_name,
            },
            transaction="Create mesh from shape",
            doc_name=doc_name,
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def check_mesh(
        mesh: str,
        tolerance: float = 1e-4,
        check_self_intersections: bool = False,
        max_components: int = 20,
        linear_deflection: float = 0.1,
        angular_deflection: float = 30.0,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Check a mesh for printing problems. Read-only; nothing is repaired.

        Reports components (count and facet sizes), open edges, non-manifold
        edges and points, degenerate and duplicated facets and points,
        winding consistency, closed/solid state, bounds, area (mm²), signed
        enclosed volume (mm³, negative when inverted) and tolerances used,
        plus which repair would apply. Separate components are reported, not
        failed. For a non-mesh shape object, a temporary tessellation is
        checked instead (nothing is added to the document).

        Args:
            mesh: Name or label of a Mesh::Feature or shape object.
            tolerance: Points closer than this (mm) count as one point.
            check_self_intersections: Also look for self-intersections (slow
                on large meshes).
            max_components: Most component sizes to list.
            linear_deflection: Tessellation setting (mm) for shape objects.
            angular_deflection: Tessellation setting (degrees) for shapes.
            doc_name: Document name. Default: the active document.

        Returns:
            Dictionary with the measurements above, issues and
            suggested_repairs lists.
        """
        return await run_operation(
            get_bridge,
            "check_mesh",
            {
                "mesh": mesh,
                "tolerance": tolerance,
                "check_self_intersections": check_self_intersections,
                "max_components": max_components,
                "linear_deflection": linear_deflection,
                "angular_deflection": angular_deflection,
                "doc_name": doc_name,
            },
        )
