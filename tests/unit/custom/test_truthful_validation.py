"""Tests that recompute, validation and inspection report only what they establish.

The generated FreeCAD code is executed against small stand-ins for FreeCAD
objects, so the tests check the code that actually runs inside FreeCAD.
"""

import copy
import io
import json
import sys
import warnings
import xmlrpc.client
import zipfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.bridge.object_health import OBJECT_HEALTH_HELPERS
from freecad_mcp.bridge.object_inspection import build_object_inspection_code

# ---------------------------------------------------------------------------
# FreeCAD stand-ins
# ---------------------------------------------------------------------------


class Vector:
    """Minimal FreeCAD.Vector."""

    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        """Store the coordinates."""
        self.x, self.y, self.z = x, y, z


class Rotation:
    """Identity rotation."""

    Axis = Vector(0, 0, 1)
    Angle = 0.0

    def toEuler(self) -> tuple[float, float, float]:
        """Return yaw, pitch and roll."""
        return (0.0, 0.0, 0.0)


class Placement:
    """Translation-only placement."""

    def __init__(self, base: Vector | None = None) -> None:
        """Store the translation."""
        self.Base = base or Vector()
        self.Rotation = Rotation()

    def multiply(self, other: "Placement") -> "Placement":
        """Compose two translations."""
        return Placement(
            Vector(
                self.Base.x + other.Base.x,
                self.Base.y + other.Base.y,
                self.Base.z + other.Base.z,
            )
        )

    def inverse(self) -> "Placement":
        """Invert the translation."""
        return Placement(Vector(-self.Base.x, -self.Base.y, -self.Base.z))

    def isIdentity(self) -> bool:
        """Report whether this placement changes nothing."""
        return (self.Base.x, self.Base.y, self.Base.z) == (0, 0, 0)

    def multVec(self, vector: Vector) -> Vector:
        """Move a point."""
        return Vector(
            vector.x + self.Base.x, vector.y + self.Base.y, vector.z + self.Base.z
        )


class BoundBox:
    """Axis-aligned box."""

    def __init__(self, low: tuple, high: tuple) -> None:
        """Store the corners."""
        self.XMin, self.YMin, self.ZMin = low
        self.XMax, self.YMax, self.ZMax = high
        self.XLength = self.XMax - self.XMin
        self.YLength = self.YMax - self.YMin
        self.ZLength = self.ZMax - self.ZMin

    def isValid(self) -> bool:
        """Report a usable box."""
        return True


class Shape:
    """Minimal TopoShape."""

    def __init__(
        self,
        shape_type: str = "Solid",
        *,
        solids: list | None = None,
        faces: int = 6,
        edges: int = 12,
        vertexes: int = 8,
        area: float = 600.0,
        volume: float = 1000.0,
        center: Vector | None = None,
        box: BoundBox | None = None,
        null: bool = False,
        valid: bool = True,
        infinite: bool = False,
    ) -> None:
        """Store the shape data."""
        self.ShapeType = shape_type
        self._null = null
        self._valid = valid
        self._infinite = infinite
        self.Area = area
        self.Volume = volume
        self.CenterOfMass = center or Vector(5, 5, 5)
        self._box = box or BoundBox((0, 0, 0), (10, 10, 10))
        self.Placement = Placement()
        if solids is None:
            solids = [self] if shape_type == "Solid" else []
        self.Solids = solids
        self.Shells = [SimpleNamespace(isClosed=lambda: True) for _ in solids]
        self.Faces = [SimpleNamespace() for _ in range(faces)]
        self.Edges = [SimpleNamespace() for _ in range(edges)]
        self.Vertexes = [SimpleNamespace() for _ in range(vertexes)]

    def isNull(self) -> bool:
        """Report a null shape."""
        return self._null

    def isValid(self) -> bool:
        """Report geometric validity; FreeCAD raises for null shapes."""
        if self._null:
            raise RuntimeError("BRepCheck_Analyzer::Init() - NULL shape")
        return self._valid

    def isClosed(self) -> bool:
        """Report closure."""
        return bool(self.Solids)

    def isInfinite(self) -> bool:
        """Report an unbounded shape."""
        return self._infinite

    @property
    def BoundBox(self) -> BoundBox:
        """Return the box moved by the shape's placement."""
        box, base = self._box, self.Placement.Base
        return BoundBox(
            (box.XMin + base.x, box.YMin + base.y, box.ZMin + base.z),
            (box.XMax + base.x, box.YMax + base.y, box.ZMax + base.z),
        )

    def copy(self) -> "Shape":
        """Return a copy that can be moved independently."""
        duplicate = copy.copy(self)
        duplicate.Placement = Placement(self.Placement.Base)
        return duplicate


def _xlink_dump(file_name: str, object_name: str) -> bytearray:
    """Build the zip FreeCAD's dumpPropertyContent returns for an XLink."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "Persistence.xml",
            f'<Content>\n<XLink file="{file_name}" stamp="" name="{object_name}"/>'
            "\n</Content>",
        )
    return bytearray(buffer.getvalue())


class Obj:
    """Minimal DocumentObject."""

    def __init__(
        self,
        name: str,
        type_id: str = "Part::Feature",
        *,
        state: list[str] | None = None,
        status: str = "Valid",
        shape: Shape | None = None,
        properties: dict[str, tuple[str, Any]] | None = None,
        dumps: dict[str, bytearray] | None = None,
        geometry: list[bool] | None = None,
        external: list | None = None,
        placement: Placement | None = None,
        global_placement: Placement | None = None,
    ) -> None:
        """Store the object data."""
        self.Name = self.Label = name
        self.TypeId = type_id
        self.State = state or ["Up-to-date"]
        self._status = status
        if shape is not None:
            self.Shape = shape
        self._types = {}
        for prop, (prop_type, value) in (properties or {}).items():
            self._types[prop] = prop_type
            setattr(self, prop, value)
        self.PropertiesList = list(self._types)
        self._dumps = dumps or {}
        if geometry is not None:
            self.Geometry = [SimpleNamespace() for _ in geometry]
            self._construction = geometry
            self.ExternalGeometry = external or []
        self.Placement = placement or Placement()
        if global_placement is not None:
            self.getGlobalPlacement = lambda: global_placement
        self.InListRecursive: list = []
        self.OutList: list = []
        self.InList: list = []
        self.Document = SimpleNamespace(Name="Doc", FileName="/models/Doc.FCStd")

    def getStatusString(self) -> str:
        """Return FreeCAD's status text."""
        return self._status

    def getTypeIdOfProperty(self, prop: str) -> str:
        """Return a property's type."""
        return self._types[prop]

    def dumpPropertyContent(self, prop: str) -> bytearray:
        """Return the stored property content."""
        return self._dumps[prop]

    def isDerivedFrom(self, type_id: str) -> bool:
        """Match the object's own type only."""
        return type_id in (self.TypeId, "App::DocumentObject")

    def getConstruction(self, index: int) -> bool:
        """Report whether a sketch geometry is construction geometry."""
        return self._construction[index]


def _freecad(objects: list[Obj], open_docs: dict | None = None) -> SimpleNamespace:
    """Build a FreeCAD module stand-in holding one document."""
    by_name = {obj.Name: obj for obj in objects}
    doc = SimpleNamespace(
        Name="Doc",
        FileName="/models/Doc.FCStd",
        Objects=objects,
        getObject=by_name.get,
        recompute=lambda: None,
    )
    for obj in objects:
        obj.Document = doc
    docs = {"Doc": doc, **(open_docs or {})}
    return SimpleNamespace(
        ActiveDocument=doc,
        getDocument=docs.__getitem__,
        listDocuments=lambda: docs,
        getDependentObjects=list,
        Vector=Vector,
    )


def _run(code: str, freecad: SimpleNamespace) -> Any:
    """Execute generated code the way the bridge does and return _result_."""
    namespace: dict[str, Any] = {"FreeCAD": freecad, "App": freecad}
    previous = sys.modules.get("FreeCAD")
    sys.modules["FreeCAD"] = freecad  # type: ignore[assignment]
    try:
        with warnings.catch_warnings():
            # Generated code compares names with "is" (see CUSTOM_CHANGES.md).
            warnings.simplefilter("ignore", SyntaxWarning)
            exec(compile(code, "<generated>", "exec"), namespace)  # noqa: S102
    finally:
        if previous is None:
            del sys.modules["FreeCAD"]
        else:
            sys.modules["FreeCAD"] = previous
    result = namespace["_result_"]
    # Results must cross both transports.
    xmlrpc.client.dumps((result,), allow_none=True)
    json.dumps(result, allow_nan=False)
    return result


def _check_object(obj: Obj, open_docs: dict | None = None) -> dict[str, Any]:
    """Run _mcp_check_object on one object."""
    freecad = _freecad([obj], open_docs)
    code = OBJECT_HEALTH_HELPERS + "\n_result_ = _mcp_check_object(FreeCAD.obj)\n"
    freecad.obj = obj
    return _run(code, freecad)


def _failed_pocket() -> Obj:
    """A pocket whose taper angle makes FreeCAD fail it."""
    return Obj(
        "Pocket",
        "PartDesign::Pocket",
        state=["Touched", "Invalid"],
        status="Extrusion: end face of tapered extrusion is empty",
        shape=Shape(),
    )


def _missing_file_link() -> Obj:
    """An App::Link whose external target file is missing."""
    return Obj(
        "Link",
        "App::Link",
        state=["Touched", "Invalid"],
        status="Link not restored\nLinked object: Box\nLinked file: Parts.FCStd",
        properties={"LinkedObject": ("App::PropertyXLink", None)},
        dumps={"LinkedObject": _xlink_dump("Parts.FCStd", "Box")},
    )


# ---------------------------------------------------------------------------
# Object checks
# ---------------------------------------------------------------------------


class TestCheckObject:
    """_mcp_check_object classification and messages."""

    def test_failed_feature_reports_freecad_message(self) -> None:
        """A broken pocket gives FreeCAD's reason, not an empty list."""
        result = _check_object(_failed_pocket())
        assert result["valid"] is False
        assert result["classification"] == "error"
        assert result["status_message"] == (
            "Extrusion: end face of tapered extrusion is empty"
        )
        assert result["error_messages"][0] == result["status_message"]
        assert any("last successful recompute" in w for w in result["warnings"])

    def test_error_without_message_is_still_explained(self) -> None:
        """An error flag without FreeCAD text still yields a message."""
        obj = Obj("Feature", state=["Invalid"], status="Valid", shape=Shape())
        result = _check_object(obj)
        assert result["error_messages"] == [
            "FreeCAD marked the object as failed but gave no error message"
        ]

    def test_link_to_missing_file_is_diagnosed(self) -> None:
        """The missing file and object are named; no shape is not 'valid'."""
        result = _check_object(_missing_file_link())
        assert result["valid"] is False
        assert result["classification"] == "unresolved_link"
        assert result["shape_valid"] is None
        assert result["has_shape"] is False
        problem = result["link_problems"][0]
        assert problem["problem"] == "missing_file"
        assert problem["linked_object"] == "Box"
        assert problem["linked_file"].endswith("Parts.FCStd")
        assert "Linked file not found" in problem["message"]
        assert "Link not restored" in result["error_messages"][0]

    def test_link_to_closed_document_is_diagnosed(self, tmp_path: Any) -> None:
        """A target file that exists but is not open is reported as such."""
        target = tmp_path / "Parts.FCStd"
        target.write_bytes(b"")
        link = Obj(
            "Link",
            "App::Link",
            state=["Touched", "Invalid"],
            status="Link broken!",
            properties={"LinkedObject": ("App::PropertyXLink", None)},
            dumps={"LinkedObject": _xlink_dump(str(target), "Box")},
        )
        result = _check_object(link)
        assert result["link_problems"][0]["problem"] == "document_not_open"

    def test_link_to_missing_object_in_open_document(self) -> None:
        """An open target document without the object is reported."""
        other = SimpleNamespace(
            Name="Parts", FileName="/models/Parts.FCStd", getObject=lambda _n: None
        )
        result = _check_object(_missing_file_link(), {"Parts": other})
        problem = result["link_problems"][0]
        assert problem["problem"] == "missing_object"
        assert problem["linked_document"] == "Parts"

    def test_link_without_target(self) -> None:
        """An App::Link with nothing linked is not reported as valid."""
        link = Obj(
            "Link",
            "App::Link",
            properties={"LinkedObject": ("App::PropertyXLink", None)},
            dumps={"LinkedObject": _xlink_dump("", "")},
        )
        result = _check_object(link)
        assert result["valid"] is False
        assert result["link_problems"][0]["problem"] == "no_target"

    def test_resolved_link_is_not_read_from_storage(self) -> None:
        """A resolved link is fine and its stored content is not decoded."""
        link = Obj(
            "Link",
            "App::Link",
            shape=Shape(),
            properties={"LinkedObject": ("App::PropertyXLink", object())},
        )
        result = _check_object(link)
        assert result["valid"] is True
        assert result["link_problems"] == []

    @pytest.mark.parametrize(
        ("geometry", "external", "warning"),
        [
            ([], [("Pad", ["Edge1"])], "external (projected)"),
            ([True], [], "construction"),
            ([], [], "Sketch is empty"),
        ],
    )
    def test_sketch_without_output_is_reference_only(
        self, geometry: list[bool], external: list, warning: str
    ) -> None:
        """A sketch with no normal geometry is a reference, not a failure."""
        sketch = Obj(
            "Sketch",
            "Sketcher::SketchObject",
            shape=Shape("Compound", null=True),
            geometry=geometry,
            external=external,
        )
        result = _check_object(sketch)
        assert result["valid"] is True
        assert result["classification"] == "reference_only"
        assert result["shape_valid"] is None
        assert result["output_geometry"] is False
        assert result["error_messages"] == []
        assert any(warning in w for w in result["warnings"])

    def test_sketch_with_profile_is_ok(self) -> None:
        """A sketch with normal geometry is classified normally."""
        sketch = Obj(
            "Sketch",
            "Sketcher::SketchObject",
            shape=Shape("Wire", faces=0, area=0.0),
            geometry=[False, True],
        )
        result = _check_object(sketch)
        assert result["classification"] == "ok"
        assert result["sketch_geometry"] == {
            "normal": 1,
            "construction": 1,
            "external": 0,
        }
        assert result["volume"] is None

    def test_unbounded_plane_has_no_area_or_volume(self) -> None:
        """Infinite datum planes don't report 8e100 areas."""
        plane = Obj(
            "DatumPlane",
            "PartDesign::Plane",
            shape=Shape("Face", infinite=True, area=8e100, volume=-1e168),
        )
        result = _check_object(plane)
        assert result["valid"] is True
        assert result["unbounded"] is True
        assert result["area"] is None
        assert result["volume"] is None

    def test_invalid_shape(self) -> None:
        """Geometrically invalid shapes are still reported."""
        result = _check_object(Obj("Bad", shape=Shape(valid=False)))
        assert result["valid"] is False
        assert result["classification"] == "invalid_shape"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class FakeBridge:
    """Bridge that runs generated code against FreeCAD stand-ins."""

    def __init__(self, freecad: SimpleNamespace) -> None:
        """Store the FreeCAD stand-in."""
        self.freecad = freecad

    async def execute_python(self, code: str) -> ExecutionResult:
        """Execute code and return its _result_."""
        return ExecutionResult(
            success=True,
            result=_run(code, self.freecad),
            stdout="",
            stderr="",
            execution_time_ms=0.0,
        )


def _tools(register: Any, freecad: SimpleNamespace) -> dict[str, Any]:
    """Register tools against a FakeBridge and return them by name."""
    mcp = MagicMock()
    tools: dict[str, Any] = {}

    def tool(**_kwargs: Any) -> Any:
        def wrapper(func: Any) -> Any:
            tools[func.__name__] = func
            return func

        return wrapper

    mcp.tool = tool
    bridge = FakeBridge(freecad)

    async def get_bridge() -> FakeBridge:
        return bridge

    register(mcp, get_bridge)
    return tools


def _view_tools(freecad: SimpleNamespace) -> dict[str, Any]:
    from freecad_mcp.tools.view import register_view_tools

    return _tools(register_view_tools, freecad)


def _document_tools(freecad: SimpleNamespace) -> dict[str, Any]:
    from freecad_mcp.tools.documents import register_document_tools

    return _tools(register_document_tools, freecad)


def _validation_tools(freecad: SimpleNamespace) -> dict[str, Any]:
    from freecad_mcp.tools.validation import register_validation_tools

    return _tools(register_validation_tools, freecad)


def _pocket_document() -> SimpleNamespace:
    """A document whose pocket failed, with a pad built on it."""
    pocket = _failed_pocket()
    pad = Obj("Pad001", "PartDesign::Pad", shape=Shape())
    pocket.InListRecursive = [pad]
    freecad = _freecad([pocket, pad])
    for obj in (pocket, pad):
        obj.touch = lambda: None
    return freecad


class TestRecomputeTools:
    """recompute and recompute_document report failures."""

    @pytest.mark.asyncio
    async def test_recompute_reports_failure_with_reason(self) -> None:
        """The recompute tool no longer always reports success."""
        result = await _view_tools(_pocket_document())["recompute"](doc_name="Doc")
        assert result["success"] is False
        assert result["touch_count"] == 2
        failed = result["invalid_objects"][0]
        assert failed["name"] == "Pocket"
        assert failed["message"] == "Extrusion: end face of tapered extrusion is empty"
        assert failed["dependents"] == ["Pad001"]
        assert "tapered extrusion" in result["error"]

    @pytest.mark.asyncio
    async def test_recompute_success(self) -> None:
        """A healthy document recomputes successfully."""
        box = Obj("Box", shape=Shape())
        box.touch = lambda: None  # type: ignore[attr-defined]
        result = await _view_tools(_freecad([box]))["recompute"](doc_name="Doc")
        assert result["success"] is True
        assert result["invalid_objects"] == []
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_recompute_document_reports_reason(self) -> None:
        """recompute_document includes FreeCAD's message."""
        result = await _document_tools(_pocket_document())["recompute_document"](
            doc_name="Doc"
        )
        assert result["success"] is False
        assert result["invalid_objects"][0]["message"].startswith("Extrusion")
        assert result["invalid_objects_truncated"] is False

    @pytest.mark.asyncio
    async def test_recompute_document_success(self) -> None:
        """No failures means success and no error."""
        result = await _document_tools(_freecad([Obj("Box", shape=Shape())]))[
            "recompute_document"
        ](doc_name="Doc")
        assert result == {
            "success": True,
            "invalid_objects": [],
            "invalid_objects_truncated": False,
            "error": None,
        }

    @pytest.mark.asyncio
    async def test_failure_list_is_capped(self) -> None:
        """Many failures are listed up to the cap with a truncation flag."""
        objects = [
            Obj(f"F{i}", state=["Invalid"], status="broken", shape=Shape())
            for i in range(60)
        ]
        result = await _document_tools(_freecad(objects))["recompute_document"](
            doc_name="Doc"
        )
        assert len(result["invalid_objects"]) == 50
        assert result["invalid_objects_truncated"] is True
        assert result["error"].startswith("60 object(s) failed")


class TestValidationTools:
    """validate_object, validate_document and undo_if_invalid."""

    @pytest.mark.asyncio
    async def test_validate_object_keeps_existing_keys(self) -> None:
        """Existing result keys remain, with FreeCAD's message added."""
        result = await _validation_tools(_freecad([_failed_pocket()]))[
            "validate_object"
        ](object_name="Pocket", doc_name="Doc")
        for key in (
            "valid",
            "object_name",
            "shape_valid",
            "has_errors",
            "state",
            "recompute_needed",
            "volume",
            "area",
            "error_messages",
            "warnings",
        ):
            assert key in result
        assert result["object_name"] == "Pocket"
        assert result["error_messages"] == [
            "Extrusion: end face of tapered extrusion is empty"
        ]

    @pytest.mark.asyncio
    async def test_validate_object_missing_object(self) -> None:
        """A missing object is reported without claiming a valid shape."""
        result = await _validation_tools(_freecad([]))["validate_object"](
            object_name="Nope", doc_name="Doc"
        )
        assert result["valid"] is False
        assert result["shape_valid"] is None
        assert "not found" in result["error_messages"][0]

    @pytest.mark.asyncio
    async def test_validate_document_separates_references_from_failures(
        self,
    ) -> None:
        """Reference sketches are listed apart; failures carry reasons."""
        sketch = Obj(
            "SkExt",
            "Sketcher::SketchObject",
            shape=Shape("Compound", null=True),
            geometry=[],
            external=[("Pad", ["Edge1"])],
        )
        freecad = _freecad([_failed_pocket(), sketch, _missing_file_link()])
        result = await _validation_tools(freecad)["validate_document"](doc_name="Doc")
        assert result["valid"] is False
        assert result["valid_objects"] == 1
        assert result["invalid_objects"] == []
        assert result["objects_with_errors"] == ["Pocket", "Link"]
        assert result["unresolved_links"] == ["Link"]
        assert result["reference_only_objects"] == ["SkExt"]
        problems = {p["name"]: p for p in result["problems"]}
        assert set(problems) == {"Pocket", "Link"}
        assert problems["Pocket"]["status_message"].startswith("Extrusion")
        assert problems["Link"]["link_problems"][0]["problem"] == "missing_file"
        assert "tapered extrusion" in result["summary"]

    @pytest.mark.asyncio
    async def test_validate_document_healthy_with_reference_sketch(self) -> None:
        """A reference-only sketch doesn't make the document invalid."""
        sketch = Obj(
            "SkExt",
            "Sketcher::SketchObject",
            shape=Shape("Compound", null=True),
            geometry=[],
            external=[("Pad", ["Edge1"])],
        )
        result = await _validation_tools(_freecad([sketch]))["validate_document"](
            doc_name="Doc"
        )
        assert result["valid"] is True
        assert result["problems"] == []

    @pytest.mark.asyncio
    async def test_undo_if_invalid_ignores_reference_sketch(self) -> None:
        """A null-shaped reference sketch no longer triggers an undo."""
        sketch = Obj(
            "SkExt",
            "Sketcher::SketchObject",
            shape=Shape("Compound", null=True),
            geometry=[],
            external=[("Pad", ["Edge1"])],
        )
        result = await _validation_tools(_freecad([sketch]))["undo_if_invalid"](
            doc_name="Doc"
        )
        assert result["was_valid"] is True
        assert result["undone"] is False

    @pytest.mark.asyncio
    async def test_undo_if_invalid_reports_reasons(self) -> None:
        """Failures found before undoing carry FreeCAD's message."""
        freecad = _freecad([_failed_pocket()])
        freecad.ActiveDocument.undo = lambda: None
        result = await _validation_tools(freecad)["undo_if_invalid"](doc_name="Doc")
        assert result["undone"] is True
        assert result["problems"][0]["error_messages"][0].startswith("Extrusion")


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def _inspect(obj: Obj, **options: Any) -> dict[str, Any]:
    """Execute the inspection program against one object."""
    return _run(
        build_object_inspection_code("Object", "Doc", **options), _freecad_for(obj)
    )


def _freecad_for(obj: Obj) -> SimpleNamespace:
    """A FreeCAD stand-in whose document returns obj for any name."""
    freecad = _freecad([obj])
    freecad.ActiveDocument.getObject = lambda _name: obj
    return freecad


class TestInspection:
    """inspect_object shape details and property handling."""

    def test_box_details(self) -> None:
        """Bounding box, centre of mass, counts and placement are reported."""
        box = Obj("Box", "Part::Box", shape=Shape())
        info = _inspect(box, include_properties=False)["shape_info"]
        assert info["volume"] == 1000.0
        assert info["area"] == 600.0
        assert info["is_solid"] is True
        assert info["solid_count"] == 1
        assert info["shell_count"] == 1
        assert (info["face_count"], info["edge_count"], info["vertex_count"]) == (
            6,
            12,
            8,
        )
        assert info["bounding_box"] == {
            "min": [0, 0, 0],
            "max": [10, 10, 10],
            "size": [10, 10, 10],
        }
        assert info["center_of_mass"] == [5.0, 5.0, 5.0]
        assert info["global_placement"]["position"] == [0.0, 0.0, 0.0]

    def test_compound_of_two_solids(self) -> None:
        """Centre of mass is volume-weighted across the solids."""
        first = Shape(volume=1000.0, center=Vector(5, 5, 5))
        second = Shape(volume=1000.0, center=Vector(25, 5, 5))
        compound = Shape(
            "Compound",
            solids=[first, second],
            faces=12,
            edges=24,
            vertexes=16,
            box=BoundBox((0, 0, 0), (30, 10, 10)),
        )
        info = _inspect(Obj("Cmp", shape=compound), include_properties=False)[
            "shape_info"
        ]
        assert info["is_solid"] is False
        assert info["solid_count"] == 2
        assert info["volume"] == 2000.0
        assert info["center_of_mass"] == [15.0, 5.0, 5.0]
        assert info["all_shells_closed"] is True

    def test_global_box_inside_moved_container(self) -> None:
        """A container's placement moves the global box and centre."""
        box = Obj(
            "Box3",
            shape=Shape(),
            global_placement=Placement(Vector(100, 0, 0)),
        )
        info = _inspect(box, include_properties=False)["shape_info"]
        assert info["bounding_box"]["min"] == [0, 0, 0]
        assert info["global_bounding_box"]["min"] == [100, 0, 0]
        assert info["global_center_of_mass"] == [105.0, 5.0, 5.0]

    def test_unbounded_datum_plane(self) -> None:
        """Infinite planes give null size values and their position and normal."""
        face = SimpleNamespace(
            Surface=SimpleNamespace(Axis=Vector(0, 0, 1), Position=Vector(0, 0, 5))
        )
        shape = Shape("Face", faces=1, edges=0, vertexes=0, infinite=True, area=8e100)
        shape.Faces = [face]
        info = _inspect(Obj("DatumPlane", "PartDesign::Plane", shape=shape))[
            "shape_info"
        ]
        assert info["unbounded"] is True
        assert info["area"] is None
        assert info["volume"] is None
        assert info["bounding_box"] is None
        assert info["plane"] == {"position": [0, 0, 5], "normal": [0, 0, 1]}
        assert "unbounded" in info["note"]

    def test_non_solid_has_null_volume(self) -> None:
        """Wires and faces don't report a volume; wires have no area."""
        wire = Shape("Wire", faces=0, edges=4, vertexes=4, area=0.0, volume=0.0)
        info = _inspect(Obj("Sketch", shape=wire), include_properties=False)[
            "shape_info"
        ]
        assert info["volume"] is None
        assert info["area"] is None
        assert info["center_of_mass"] is None

    def test_property_names_filter(self) -> None:
        """Only the requested properties are read, in order."""
        obj = Obj(
            "Box",
            properties={
                "Length": ("App::PropertyLength", 10.0),
                "Width": ("App::PropertyLength", 5.0),
            },
        )
        result = _inspect(obj, include_shape=False, property_names=["Width"])
        assert result["properties"] == {"Width": 5.0}
        assert result["properties_truncated"] is False

    def test_property_names_unknown_raises(self) -> None:
        """Unknown property names are an error, not silently skipped."""
        obj = Obj("Box", properties={"Length": ("App::PropertyLength", 10.0)})
        with pytest.raises(ValueError, match="no properties named: Nope"):
            _inspect(obj, include_shape=False, property_names=["Nope"])

    def test_property_names_must_be_strings(self) -> None:
        """Non-string names are rejected before any code is built."""
        with pytest.raises(TypeError):
            build_object_inspection_code(
                "Box",
                "Doc",
                property_names=[1],  # type: ignore[list-item]
            )

    def test_truncation_is_flagged(self) -> None:
        """Cut property lists and values set properties_truncated."""
        many = {f"P{i}": ("App::PropertyString", "x") for i in range(150)}
        result = _inspect(Obj("Big", properties=many), include_shape=False)
        assert result["properties_truncated"] is True
        long_value = {"Text": ("App::PropertyString", "x" * 900)}
        result = _inspect(Obj("Long", properties=long_value), include_shape=False)
        assert result["properties_truncated"] is True

    def test_default_code_unchanged_for_bridges(self) -> None:
        """The filter defaults to None, so existing calls build the same code."""
        code = build_object_inspection_code("Box", "Doc")
        assert "_property_names = None\n" in code
