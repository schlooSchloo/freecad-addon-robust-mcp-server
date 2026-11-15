"""Check recompute, validation and inspection reports against real FreeCAD."""

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from freecad_mcp.bridge.base import FreecadBridge
from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools.documents import register_document_tools
from freecad_mcp.tools.objects import register_object_tools
from freecad_mcp.tools.validation import register_validation_tools
from freecad_mcp.tools.view import register_view_tools
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration


def _register(bridge: FreecadBridge) -> dict[str, Callable[..., Awaitable[Any]]]:
    """Register the production tools with a live bridge."""
    mcp = MagicMock()
    registered: dict[str, Callable[..., Awaitable[Any]]] = {}

    def tool(**_kwargs: Any) -> Callable[[Any], Any]:
        def wrapper(func: Any) -> Any:
            registered[func.__name__] = func
            return func

        return wrapper

    async def get_bridge() -> FreecadBridge:
        return bridge

    mcp.tool = tool
    for register in (
        register_document_tools,
        register_object_tools,
        register_validation_tools,
        register_view_tools,
    ):
        register(mcp, get_bridge)
    return registered


_SETUP = """
import Part
doc = FreeCAD.newDocument({doc_name!r})
body = doc.addObject("PartDesign::Body", "Body")
xy = [o for o in body.Origin.OriginFeatures if o.Role == "XY_Plane"][0]
sketch = body.newObject("Sketcher::SketchObject", "Sketch")
# FreeCAD 1.0 renamed Support to AttachmentSupport.
support = "AttachmentSupport" if hasattr(sketch, "AttachmentSupport") else "Support"
setattr(sketch, support, [(xy, "")])
sketch.MapMode = "FlatFace"
corners = [(0, 0), (20, 0), (20, 20), (0, 20)]
for i in range(4):
    a, b = corners[i], corners[(i + 1) % 4]
    sketch.addGeometry(Part.LineSegment(FreeCAD.Vector(*a, 0), FreeCAD.Vector(*b, 0)))
pad = body.newObject("PartDesign::Pad", "Pad")
pad.Profile = sketch
pad.Length = 10
doc.recompute()

hole = body.newObject("Sketcher::SketchObject", "HoleSketch")
setattr(hole, support, [(pad, "Face6")])
hole.MapMode = "FlatFace"
hole.addGeometry(Part.Circle(FreeCAD.Vector(10, 10, 0), FreeCAD.Vector(0, 0, 1), 3))
pocket = body.newObject("PartDesign::Pocket", "Pocket")
pocket.Profile = hole
pocket.Length = 8
pocket.TaperAngle = -60

reference = body.newObject("Sketcher::SketchObject", "Reference")
setattr(reference, support, [(pad, "Face6")])
reference.MapMode = "FlatFace"
reference.addExternal("Pad", "Edge1")

plane = body.newObject("PartDesign::Plane", "DatumPlane")
setattr(plane, support, [(xy, "")])
plane.MapMode = "FlatFace"
doc.recompute()
_result_ = True
"""


@pytest.mark.asyncio
async def test_failed_pocket_reference_sketch_and_datum_plane() -> None:
    """Failures are reported with FreeCAD's reason; references are not."""
    doc_name = f"McpTruthful_{uuid4().hex}"
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    try:
        setup = await bridge.execute_python(_SETUP.format(doc_name=doc_name))
        assert setup.success, setup.error_traceback
        tools = _register(bridge)

        recompute = await tools["recompute"](doc_name=doc_name)
        assert recompute["success"] is False
        failed = {entry["name"]: entry for entry in recompute["invalid_objects"]}
        assert "tapered extrusion" in failed["Pocket"]["message"]

        document = await tools["recompute_document"](doc_name=doc_name)
        assert document["success"] is False
        assert document["invalid_objects"][0]["message"]

        pocket = await tools["validate_object"]("Pocket", doc_name=doc_name)
        assert pocket["valid"] is False
        assert pocket["status_message"] == failed["Pocket"]["message"]

        reference = await tools["validate_object"]("Reference", doc_name=doc_name)
        assert reference["valid"] is True
        assert reference["classification"] == "reference_only"
        assert reference["shape_valid"] is None

        health = await tools["validate_document"](doc_name=doc_name)
        assert health["reference_only_objects"] == ["Reference"]
        assert [p["name"] for p in health["problems"]] == ["Pocket"]

        plane = await tools["inspect_object"](
            "DatumPlane", doc_name=doc_name, include_properties=False
        )
        info = plane["shape_info"]
        assert info["unbounded"] is True
        assert info["area"] is None and info["volume"] is None
        assert info["plane"]["normal"] == pytest.approx([0.0, 0.0, 1.0])

        pad = await tools["inspect_object"](
            "Pad", doc_name=doc_name, include_properties=False
        )
        info = pad["shape_info"]
        assert info["volume"] == pytest.approx(4000.0)
        assert info["center_of_mass"] == pytest.approx([10.0, 10.0, 5.0])
        assert info["bounding_box"]["size"] == pytest.approx([20.0, 20.0, 10.0])
    finally:
        await bridge.execute_python(
            f"if {doc_name!r} in FreeCAD.listDocuments():\n"
            f"    FreeCAD.closeDocument({doc_name!r})\n_result_ = True"
        )
        await bridge.disconnect()
