"""Tests for the shared edit_object / delete_object programs.

The generated FreeCAD-side code is executed against a small fake FreeCAD
module, so property conversion, all-or-nothing behaviour, read-back, undo
names and the PartDesign Tip handling are checked without FreeCAD.
"""

import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult, ObjectInfo
from freecad_mcp.bridge.embedded import EmbeddedBridge
from freecad_mcp.bridge.object_editing import (
    ObjectEditResult,
    build_delete_object_code,
    build_edit_object_code,
    delete_result_from_execution,
    edit_result_from_execution,
)
from freecad_mcp.bridge.socket import SocketBridge
from freecad_mcp.bridge.xmlrpc import XmlRpcBridge

# ---------------------------------------------------------------------------
# Fake FreeCAD
# ---------------------------------------------------------------------------

_LINK_TYPES = re.compile(r"^App::Property(X?)Link(Sub)?(List)?$")


class Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __eq__(self, other):
        return (self.x, self.y, self.z) == (other.x, other.y, other.z)

    __hash__ = None  # type: ignore[assignment]


class Rotation:
    def __init__(self, *args):
        if len(args) == 3:
            self.ypr = tuple(float(a) for a in args)
        elif len(args) == 2:
            self.ypr = (float(args[1]), 0.0, 0.0)  # axis-angle around z only
        else:
            self.ypr = (0.0, 0.0, 0.0)

    def toEuler(self):
        return self.ypr


class Placement:
    def __init__(self, base=None, rotation=None):
        self.Base = base or Vector()
        self.Rotation = rotation or Rotation()


class Quantity:
    def __init__(self, value, unit="mm"):
        self.Value = float(value)
        self.Unit = unit
        self.UserString = f"{value} {unit}"

    def __str__(self):
        return f"{self.Value} {self.Unit}"


class FakeObject:
    """Document object with typed properties and an expression engine."""

    def __init__(self, doc, name, type_id="Part::Feature", **props):
        self.__dict__["_props"] = {}
        self.__dict__["_types"] = {}
        self.__dict__["_exprs"] = {}
        self.__dict__["Name"] = name
        self.__dict__["Label"] = name
        self.__dict__["TypeId"] = type_id
        self.__dict__["Document"] = doc
        self.__dict__["ViewObject"] = None
        self.__dict__["fail_on"] = set()
        for key, (ptype, value) in props.items():
            self._types[key] = ptype
            self._props[key] = value
        doc.objects.append(self)

    def __getattr__(self, name):
        props = self.__dict__["_props"]
        if name in props:
            return props[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name not in self._props:
            self.__dict__[name] = value
            return
        if name in self.fail_on:
            raise ValueError("rejected by FreeCAD")
        ptype = self._types[name]
        match = _LINK_TYPES.match(ptype)
        if match and match.group(2) and match.group(3):
            # FreeCAD drops sub-list entries with no sub-elements
            value = [(o, tuple(s)) for o, s in value if len(s)]
        elif match and not match.group(1) and isinstance(value, str):
            raise TypeError("Type must be App.DocumentObject or None, not str")
        self._props[name] = value

    @property
    def PropertiesList(self):
        return list(self._props)

    @property
    def ExpressionEngine(self):
        return list(self._exprs.items())

    @property
    def InList(self):
        return [o for o in self.Document.objects if self in o.links()]

    @property
    def OutList(self):
        return self.links()

    def links(self):
        found = []

        def walk(value):
            if isinstance(value, FakeObject):
                found.append(value)
            elif isinstance(value, list | tuple):
                for item in value:
                    walk(item)

        for value in self._props.values():
            walk(value)
        return found

    def isDerivedFrom(self, type_id):
        return type_id == "App::DocumentObject"

    def getTypeIdOfProperty(self, name):
        return self._types[name]

    def getEnumerationsOfProperty(self, name):
        return (
            ["Length", "UpToFace"]
            if self._types[name].endswith("Enumeration")
            else None
        )

    def setExpression(self, path, expression):
        if expression is None:
            self._exprs.pop(path, None)
        else:
            self._exprs[path if "." not in path else "." + path.lstrip(".")] = (
                expression
            )

    def isValid(self):
        return True


class FakeBody(FakeObject):
    def __init__(self, doc, name):
        super().__init__(
            doc,
            name,
            "PartDesign::Body",
            Group=("App::PropertyLinkList", []),
            Tip=("App::PropertyLink", None),
            BaseFeature=("App::PropertyLink", None),
        )
        self.__dict__["Shape"] = MagicMock(isNull=lambda: False, isValid=lambda: True)

    def removeObject(self, feature):
        group = self.Group
        index = group.index(feature)
        previous = getattr(feature, "BaseFeature", None)
        for later in group[index + 1 :]:
            if getattr(later, "BaseFeature", None) is feature:
                later.BaseFeature = previous
        if self.Tip is feature:
            self.Tip = previous
        self.Group = [o for o in group if o is not feature]


class FakeDocument:
    def __init__(self, name):
        self.Name = name
        self.objects: list[FakeObject] = []
        self.transactions: list[tuple[str, str]] = []
        self.recomputed = 0

    @property
    def Objects(self):
        return list(self.objects)

    def getObject(self, name):
        return next((o for o in self.objects if o.Name == name), None)

    def getObjectsByLabel(self, label):
        return [o for o in self.objects if o.Label == label]

    def openTransaction(self, name):
        self.transactions.append(("open", name))

    def commitTransaction(self):
        self.transactions.append(("commit", ""))

    def abortTransaction(self):
        self.transactions.append(("abort", ""))

    def recompute(self):
        self.recomputed += 1

    def removeObject(self, name):
        self.objects.remove(self.getObject(name))


class FakeFreeCAD:
    Vector = Vector
    Rotation = Rotation
    Placement = Placement

    def __init__(self, *docs):
        self.docs = {d.Name: d for d in docs}
        self.ActiveDocument = docs[0] if docs else None

    def listDocuments(self):
        return dict(self.docs)


def _part_doc() -> tuple[FakeFreeCAD, FakeDocument]:
    doc = FakeDocument("Doc")
    other = FakeDocument("Other")
    FakeObject(other, "Cyl", "Part::Cylinder")
    body = FakeBody(doc, "Body")
    sketch = FakeObject(
        doc,
        "Sketch",
        "Sketcher::SketchObject",
        AttachmentSupport=("App::PropertyLinkSubList", []),
        Placement=("App::PropertyPlacement", Placement()),
    )
    pad = FakeObject(
        doc,
        "Pad",
        "PartDesign::Pad",
        Profile=("App::PropertyLinkSub", None),
        UpToFace=("App::PropertyLinkSub", None),
        BaseFeature=("App::PropertyLink", None),
        Length=("App::PropertyLength", Quantity(5)),
        Type=("App::PropertyEnumeration", "Length"),
        Direction=("App::PropertyVector", Vector(0, 0, 1)),
    )
    pocket = FakeObject(
        doc,
        "Pocket",
        "PartDesign::Pocket",
        BaseFeature=("App::PropertyLink", pad),
        Length=("App::PropertyLength", Quantity(2)),
        TaperAngle=("App::PropertyAngle", Quantity(0, "deg")),
    )
    FakeObject(doc, "DatumPlane", "PartDesign::Plane")
    FakeObject(doc, "Link", "App::Link", LinkedObject=("App::PropertyXLink", None))
    body.Group = [sketch, pad, pocket]
    body.Tip = pocket
    return FakeFreeCAD(doc, other), doc


def _run(code: str, freecad: FakeFreeCAD) -> dict[str, Any]:
    namespace: dict[str, Any] = {"FreeCAD": freecad}
    exec(compile(code, "<generated>", "exec"), namespace)  # noqa: S102
    result = namespace["_result_"]
    json.dumps(result)  # must be wire-safe
    return result


def _edit(freecad, obj, props, doc="Doc"):
    return _run(build_edit_object_code(obj, props, doc), freecad)


# ---------------------------------------------------------------------------
# edit_object program
# ---------------------------------------------------------------------------


def test_link_property_takes_an_object_name() -> None:
    freecad, doc = _part_doc()
    result = _edit(freecad, "Body", {"Tip": "Pad"})
    assert doc.getObject("Body").Tip is doc.getObject("Pad")
    assert result["properties"] == {"Tip": "Pad"}
    assert doc.transactions == [("open", "Edit Body.Tip"), ("commit", "")]


def test_link_property_null_clears_and_label_resolves() -> None:
    freecad, doc = _part_doc()
    doc.getObject("Pad").__dict__["Label"] = "Base pad"
    _edit(freecad, "Pocket", {"BaseFeature": "Base pad"})
    assert doc.getObject("Pocket").BaseFeature is doc.getObject("Pad")
    result = _edit(freecad, "Pocket", {"BaseFeature": None})
    assert result["properties"] == {"BaseFeature": None}


def test_empty_subelement_list_means_whole_object() -> None:
    freecad, doc = _part_doc()
    result = _edit(freecad, "Sketch", {"AttachmentSupport": ["DatumPlane", []]})
    stored = doc.getObject("Sketch").AttachmentSupport
    assert stored == [(doc.getObject("DatumPlane"), ("",))]
    assert result["properties"]["AttachmentSupport"] == [
        {"object": "DatumPlane", "subelements": [""]}
    ]
    assert any('[""]' in note for note in result["notes"])


@pytest.mark.parametrize(
    "value",
    [
        "DatumPlane",
        ["DatumPlane", [""]],
        {"object": "DatumPlane", "subelements": [""]},
        [{"object": "DatumPlane"}],
    ],
)
def test_subelement_list_forms(value: Any) -> None:
    freecad, _doc = _part_doc()
    result = _edit(freecad, "Sketch", {"AttachmentSupport": value})
    assert result["properties"]["AttachmentSupport"] == [
        {"object": "DatumPlane", "subelements": [""]}
    ]
    assert result["notes"] == []


def test_link_sub_reads_back_object_and_subelements() -> None:
    freecad, doc = _part_doc()
    result = _edit(freecad, "Pad", {"UpToFace": ["Pocket", ["Face6"]]})
    assert doc.getObject("Pad").UpToFace == (doc.getObject("Pocket"), ["Face6"])
    assert result["properties"]["UpToFace"] == {
        "object": "Pocket",
        "subelements": ["Face6"],
    }


def test_xlink_accepts_another_document() -> None:
    freecad, doc = _part_doc()
    result = _edit(
        freecad, "Link", {"LinkedObject": {"object": "Cyl", "document": "Other"}}
    )
    assert doc.getObject("Link").LinkedObject is freecad.docs["Other"].getObject("Cyl")
    assert result["properties"]["LinkedObject"] == {
        "object": "Cyl",
        "document": "Other",
    }


def test_expression_set_and_clear() -> None:
    freecad, doc = _part_doc()
    pocket = doc.getObject("Pocket")
    result = _edit(freecad, "Pocket", {"Length": "=Pad.Length / 2"})
    assert pocket._exprs == {"Length": "Pad.Length / 2"}
    assert result["properties"]["Length"]["expression"] == "Pad.Length / 2"
    assert doc.transactions[0] == ("open", "Edit Pocket.Length")
    result = _edit(freecad, "Pocket", {"Length": "="})
    assert pocket._exprs == {}
    assert result["properties"]["Length"]["expression"] is None


def test_expression_on_subproperty_path() -> None:
    freecad, doc = _part_doc()
    result = _edit(freecad, "Sketch", {"Placement.Base.z": "=Pad.Length"})
    assert doc.getObject("Sketch")._exprs == {".Placement.Base.z": "Pad.Length"}
    assert result["properties"]["Placement.Base.z"]["expression"] == "Pad.Length"


def test_plain_value_removes_existing_expression() -> None:
    freecad, doc = _part_doc()
    pocket = doc.getObject("Pocket")
    pocket.setExpression("Length", "Pad.Length")
    result = _edit(freecad, "Pocket", {"Length": 3})
    assert pocket._exprs == {}
    assert pocket.Length == 3
    assert any("removed expression" in note for note in result["notes"])


def test_subproperty_path_without_expression_is_refused() -> None:
    freecad, doc = _part_doc()
    result = _edit(freecad, "Sketch", {"Placement.Base.z": 4})
    assert "only take expressions" in result["error"]
    assert doc.transactions == []


def test_unknown_property_lists_close_matches_and_changes_nothing() -> None:
    freecad, doc = _part_doc()
    result = _edit(freecad, "Pocket", {"Length": 9, "TaperAngel": 2})
    assert "TaperAngel" in result["error"]
    assert "TaperAngle" in result["error"]
    assert "Nothing was changed" in result["error"]
    assert doc.getObject("Pocket").Length.Value == 2
    assert doc.transactions == []


def test_bad_enumeration_lists_allowed_values() -> None:
    freecad, _ = _part_doc()
    result = _edit(freecad, "Pad", {"Type": "Sideways"})
    assert "Length, UpToFace" in result["error"]


def test_missing_link_target_changes_nothing() -> None:
    freecad, doc = _part_doc()
    result = _edit(freecad, "Pad", {"Length": 7, "Profile": "Sketc"})
    assert "Object not found: 'Sketc'" in result["error"]
    assert "Sketch" in result["error"]
    assert doc.getObject("Pad").Length.Value == 5
    assert doc.transactions == []


def test_failure_while_setting_aborts_the_transaction() -> None:
    freecad, doc = _part_doc()
    doc.getObject("Pad").fail_on.add("Type")
    result = _edit(freecad, "Pad", {"Length": 7, "Type": "UpToFace"})
    assert result["error"].startswith("Could not set Pad.Type")
    assert doc.transactions == [("open", "Edit Pad.Length, Type"), ("abort", "")]


def test_placement_and_vector_forms() -> None:
    freecad, doc = _part_doc()
    sketch = doc.getObject("Sketch")
    result = _edit(
        freecad,
        "Sketch",
        {"Placement": {"position": [1, 2, 3], "rotation": [90, 0, 0]}},
    )
    assert result["properties"]["Placement"] == {
        "position": [1.0, 2.0, 3.0],
        "rotation": [90.0, 0.0, 0.0],
    }
    _edit(freecad, "Sketch", {"Placement": {"position": [4, 5, 6]}})
    assert sketch.Placement.Rotation.toEuler() == (90.0, 0.0, 0.0)
    _edit(freecad, "Sketch", {"Placement": [[0, 0, 1], [0, 0, 0]]})
    assert sketch.Placement.Base == Vector(0, 0, 1)
    result = _edit(freecad, "Pad", {"Direction": [1, 0, 0]})
    assert result["properties"]["Direction"] == [1.0, 0.0, 0.0]
    assert "vector" in _edit(freecad, "Pad", {"Direction": [1, 0]})["error"]


def test_many_properties_get_a_short_undo_name() -> None:
    freecad, doc = _part_doc()
    _edit(
        freecad,
        "Pad",
        {"Length": 1, "Type": "Length", "Direction": [0, 0, 1], "BaseFeature": None},
    )
    assert doc.transactions[0] == ("open", "Edit Pad (4 properties)")


def test_unknown_object_and_document_errors() -> None:
    freecad, _ = _part_doc()
    assert (
        "Object not found: 'Pockt'" in _edit(freecad, "Pockt", {"Length": 1})["error"]
    )
    assert "Pocket" in _edit(freecad, "Pockt", {"Length": 1})["error"]
    error = _edit(freecad, "Pad", {"Length": 1}, doc="Nope")["error"]
    assert error.startswith("Document not found: 'Nope'")


def test_build_edit_code_rejects_non_json_values() -> None:
    with pytest.raises(ValueError, match="JSON"):
        build_edit_object_code("Box", {"Length": float("nan")}, None)
    with pytest.raises(ValueError, match="dictionary"):
        build_edit_object_code("Box", [("Length", 1)], None)  # type: ignore[arg-type]


def test_names_are_passed_as_data() -> None:
    hostile = "x'); import os; os.system('echo') #"
    code = build_edit_object_code(hostile, {hostile: hostile}, hostile)
    compile(code, "<generated>", "exec")
    freecad, _ = _part_doc()
    result = _run(code, freecad)
    assert result["error"].startswith("Document not found")
    result = _run(build_delete_object_code(hostile, "Doc"), freecad)
    assert result["error"].startswith("Object not found")


# ---------------------------------------------------------------------------
# delete_object program
# ---------------------------------------------------------------------------


def test_deleting_the_tip_moves_it_to_the_previous_feature() -> None:
    freecad, doc = _part_doc()
    body = doc.getObject("Body")
    result = _run(build_delete_object_code("Pocket", "Doc"), freecad)
    assert body.Tip is doc.getObject("Pad")
    assert doc.getObject("Pocket") is None
    assert result["body"] == "Body"
    assert result["tip"] == "Pad"
    assert result["body_shape_valid"] is True
    assert any("the Tip is now Pad" in note for note in result["notes"])
    assert doc.transactions == [("open", "Delete Pocket"), ("commit", "")]
    assert doc.recomputed == 1


def test_deleting_a_middle_feature_relinks_the_next_one() -> None:
    freecad, doc = _part_doc()
    result = _run(build_delete_object_code("Pad", "Doc"), freecad)
    assert doc.getObject("Body").Tip is doc.getObject("Pocket")
    assert any("Pocket now builds on nothing" in note for note in result["notes"])


def test_deleting_a_linked_object_reports_dependents() -> None:
    freecad, doc = _part_doc()
    doc.getObject("Pad").Profile = (doc.getObject("Sketch"), [])
    result = _run(build_delete_object_code("Sketch", "Doc"), freecad)
    assert any("empty link: Pad" in note for note in result["notes"])


def test_deleting_a_missing_object_is_a_clean_error() -> None:
    freecad, doc = _part_doc()
    result = _run(build_delete_object_code("Pockt", "Doc"), freecad)
    assert result == {
        "error": "Object not found: 'Pockt' in document 'Doc'. Did you mean: Pocket?"
    }
    assert doc.transactions == []


# ---------------------------------------------------------------------------
# Bridges and tools
# ---------------------------------------------------------------------------


def _execution(result: Any, success: bool = True, traceback: str | None = None):
    return ExecutionResult(
        success=success,
        result=result,
        stdout="",
        stderr="",
        execution_time_ms=0,
        error_traceback=traceback,
    )


def test_result_parsers() -> None:
    info = edit_result_from_execution(
        _execution(
            {
                "name": "Box",
                "label": "Box",
                "type_id": "Part::Box",
                "properties": {"Length": "5.0 mm"},
                "notes": ["n"],
            }
        )
    )
    assert isinstance(info, ObjectEditResult)
    assert info.properties == {"Length": "5.0 mm"}
    assert info.notes == ["n"]
    with pytest.raises(ValueError, match=r"^Object not found: 'X'$"):
        delete_result_from_execution(_execution({"error": "Object not found: 'X'"}))
    with pytest.raises(ValueError, match="Traceback"):
        delete_result_from_execution(_execution(None, False, "Traceback ..."))


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge_class", [XmlRpcBridge, SocketBridge, EmbeddedBridge])
async def test_bridges_send_the_shared_programs(bridge_class: type) -> None:
    bridge = bridge_class()
    bridge.execute_python = AsyncMock(return_value=_execution({"error": "Nope"}))
    try:
        with pytest.raises(ValueError, match=r"^Nope$"):
            await bridge.edit_object("Pad", {"Length": 3}, "Doc")
        code = bridge.execute_python.call_args.args[0]
        assert code == build_edit_object_code("Pad", {"Length": 3}, "Doc")
        with pytest.raises(ValueError, match=r"^Nope$"):
            await bridge.delete_object("Pad", "Doc")
        code = bridge.execute_python.call_args.args[0]
        assert code == build_delete_object_code("Pad", "Doc")
    finally:
        await bridge.disconnect()


@pytest.fixture
def object_tools() -> tuple[dict[str, Any], AsyncMock]:
    from freecad_mcp.tools.objects import register_object_tools

    mcp = MagicMock()
    tools: dict[str, Any] = {}

    def tool_decorator():
        def wrapper(func):
            tools[func.__name__] = func
            return func

        return wrapper

    mcp.tool = tool_decorator
    bridge = AsyncMock()

    async def get_bridge():
        return bridge

    register_object_tools(mcp, get_bridge)
    return tools, bridge


@pytest.mark.asyncio
async def test_edit_tool_returns_stored_values_and_notes(object_tools) -> None:
    tools, bridge = object_tools
    bridge.edit_object = AsyncMock(
        return_value=ObjectEditResult(
            name="Body",
            label="Body",
            type_id="PartDesign::Body",
            properties={"Tip": "Pad"},
            notes=["note"],
        )
    )
    result = await tools["edit_object"]("Body", {"Tip": "Pad"}, "Doc")
    assert result["properties"] == {"Tip": "Pad"}
    assert result["notes"] == ["note"]
    bridge.edit_object = AsyncMock(
        return_value=ObjectInfo(name="Box", label="Box", type_id="Part::Box")
    )
    assert (await tools["edit_object"]("Box", {}))["notes"] == []


@pytest.mark.asyncio
async def test_delete_tool_merges_the_report(object_tools) -> None:
    tools, bridge = object_tools
    bridge.delete_object = AsyncMock(
        return_value={"deleted": "Pocket", "tip": "Pad", "body": "Body", "notes": []}
    )
    result = await tools["delete_object"]("Pocket", "Doc")
    assert result == {
        "success": True,
        "deleted": "Pocket",
        "tip": "Pad",
        "body": "Body",
        "notes": [],
    }
