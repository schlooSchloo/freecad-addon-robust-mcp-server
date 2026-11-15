"""edit_object and delete_object against real FreeCAD PartDesign objects."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration

_SETUP = """
import FreeCAD
import Part

def _rect(sketch, x0, y0, x1, y1):
    points = [FreeCAD.Vector(x0, y0, 0), FreeCAD.Vector(x1, y0, 0),
              FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x0, y1, 0)]
    for index in range(4):
        sketch.addGeometry(Part.LineSegment(points[index], points[(index + 1) % 4]))

doc = FreeCAD.newDocument({doc_name!r})
body = doc.addObject("PartDesign::Body", "Body")
xy = [f for f in body.Origin.OriginFeatures if f.Role == "XY_Plane"][0]
sketch = body.newObject("Sketcher::SketchObject", "Sketch")
sketch.AttachmentSupport = [(xy, [""])]
sketch.MapMode = "FlatFace"
_rect(sketch, 0, 0, 10, 10)
pad = body.newObject("PartDesign::Pad", "Pad")
pad.Profile = sketch
pad.Length = 5
sketch2 = body.newObject("Sketcher::SketchObject", "Sketch001")
sketch2.AttachmentSupport = [(xy, [""])]
sketch2.MapMode = "FlatFace"
sketch2.AttachmentOffset = FreeCAD.Placement(FreeCAD.Vector(0, 0, 5), FreeCAD.Rotation())
_rect(sketch2, 2, 2, 4, 4)
pocket = body.newObject("PartDesign::Pocket", "Pocket")
pocket.Profile = sketch2
pocket.Length = 2
plane = body.newObject("PartDesign::Plane", "DatumPlane")
plane.AttachmentSupport = [(xy, [""])]
plane.MapMode = "FlatFace"
loose = body.newObject("Sketcher::SketchObject", "Loose")
doc.addObject("Part::Box", "Box")
doc.addObject("Part::Scale", "Scale")
doc.recompute()
_result_ = body.Tip.Name
"""


@pytest.fixture
async def part_doc() -> AsyncIterator[tuple[XmlRpcBridge, str]]:
    """Create a body with a pad and a pocket; close it afterwards."""
    doc_name = f"McpEditDelete_{uuid4().hex}"
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    try:
        setup = await bridge.execute_python(_SETUP.format(doc_name=doc_name))
        assert setup.success, setup.error_traceback or setup.stderr
        assert setup.result == "Pocket"
        yield bridge, doc_name
    finally:
        try:
            await bridge.execute_python(
                f"FreeCAD.closeDocument({doc_name!r})\n_result_ = True"
            )
        finally:
            await bridge.disconnect()


@pytest.mark.asyncio
async def test_edit_object_sets_links_and_expressions(part_doc) -> None:
    """Link, sub-element and expression values are stored and read back."""
    bridge, doc_name = part_doc
    tip = await bridge.edit_object("Body", {"Tip": "Pad"}, doc_name)
    assert tip.properties == {"Tip": "Pad"}

    attached = await bridge.edit_object(
        "Loose",
        {"AttachmentSupport": ["DatumPlane", []], "MapMode": "FlatFace"},
        doc_name,
    )
    assert attached.properties["AttachmentSupport"] == [
        {"object": "DatumPlane", "subelements": [""]}
    ]
    assert attached.notes

    scaled = await bridge.edit_object("Scale", {"Base": "Box"}, doc_name)
    assert scaled.properties == {"Base": "Box"}

    expr = await bridge.edit_object("Pocket", {"Length": "=Pad.Length / 5"}, doc_name)
    assert expr.properties["Length"]["expression"] == "Pad.Length / 5"
    assert expr.properties["Length"]["value"] == "1.0 mm"

    state = await bridge.execute_python(
        f"doc = FreeCAD.getDocument({doc_name!r})\n"
        "_result_ = [doc.Body.Tip.Name, doc.Loose.AttachmentSupport[0][0].Name,"
        " doc.Scale.Shape.Volume, doc.UndoNames[0]]"
    )
    assert state.result[0] == "Pad"
    assert state.result[1] == "DatumPlane"
    assert state.result[2] == pytest.approx(1000.0)
    assert state.result[3] == "Edit Pocket.Length"


@pytest.mark.asyncio
async def test_edit_object_refuses_unknown_names_without_changes(part_doc) -> None:
    """An unknown name is reported with suggestions and nothing changes."""
    bridge, doc_name = part_doc
    with pytest.raises(ValueError, match="TaperAngle") as caught:
        await bridge.edit_object("Pocket", {"Length": 4, "TaperAngel": 1}, doc_name)
    assert "Traceback" not in str(caught.value)
    state = await bridge.execute_python(
        f"_result_ = FreeCAD.getDocument({doc_name!r}).Pocket.Length.Value"
    )
    assert state.result == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_delete_tip_moves_tip_back(part_doc) -> None:
    """Deleting the last feature leaves the body with the previous solid."""
    bridge, doc_name = part_doc
    report = await bridge.delete_object("Pocket", doc_name)
    assert report["tip"] == "Pad"
    assert report["body_shape_valid"] is True
    state = await bridge.execute_python(
        f"doc = FreeCAD.getDocument({doc_name!r})\n"
        "_result_ = [doc.Body.Tip.Name, doc.Body.Shape.Volume, doc.UndoNames[0]]"
    )
    assert state.result[0] == "Pad"
    assert state.result[1] == pytest.approx(500.0)
    assert state.result[2] == "Delete Pocket"
    with pytest.raises(ValueError, match=r"^Object not found: 'Pocket'"):
        await bridge.delete_object("Pocket", doc_name)
