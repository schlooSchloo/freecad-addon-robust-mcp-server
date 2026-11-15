"""Tests for create_sketch's plane lookup.

The lookup runs inside FreeCAD, so these tests run the FreeCAD-side helper
(``SKETCH_PLANE_CODE``) against small stand-ins for FreeCAD objects, and
check that the tool sends it.
"""

from __future__ import annotations

import warnings
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools.custom.shape_snippets import SKETCH_PLANE_CODE


def _reply(result: Any = None, error: str | None = None) -> ExecutionResult:
    """Build a bridge reply: success with a result, or failure with an error."""
    return ExecutionResult(
        success=error is None,
        result=result,
        stdout="",
        stderr="",
        execution_time_ms=1.0,
        error_traceback=error,
    )


class FakeShape:
    """Stand-in for a Part shape with a number of faces."""

    def __init__(self, faces: int = 0) -> None:
        self.Faces = [object()] * faces

    def isNull(self) -> bool:
        return not self.Faces


class FakeObject:
    """Stand-in for a FreeCAD document object."""

    def __init__(
        self,
        name: str,
        types: tuple[str, ...],
        label: str | None = None,
        role: str | None = None,
        parent: FakeObject | None = None,
    ) -> None:
        self.Name = name
        self.Label = label or name
        self.TypeId = types[0]
        self._types = types
        self._parent = parent
        if role is not None:
            self.Role = role

    def isDerivedFrom(self, type_name: str) -> bool:
        return type_name in self._types

    def getParentGeoFeatureGroup(self) -> FakeObject | None:
        return self._parent


class FakeOrigin:
    def __init__(self, features: list[FakeObject]) -> None:
        self.OriginFeatures = features


class FakeBody(FakeObject):
    """Stand-in for a PartDesign Body with origin planes named by suffix."""

    def __init__(self, name: str, suffix: str = "", faces: int = 0) -> None:
        super().__init__(name, ("PartDesign::Body",))
        self.Shape = FakeShape(faces)
        planes = [
            FakeObject(f"{role}{suffix}", ("App::Plane",), role=role, parent=self)
            for role in ("XY_Plane", "XZ_Plane", "YZ_Plane")
        ]
        axis = FakeObject(f"X_Axis{suffix}", ("App::Line",), role="X_Axis")
        self.Origin = FakeOrigin([axis, *planes])


class FakeDocument:
    def __init__(self, objects: list[FakeObject]) -> None:
        self.Objects = objects

    def getObject(self, name: str) -> FakeObject | None:
        return next((o for o in self.Objects if o.Name == name), None)

    def getObjectsByLabel(self, label: str) -> list[FakeObject]:
        return [o for o in self.Objects if o.Label == label]


@pytest.fixture
def resolve() -> Any:
    """The FreeCAD-side resolver function, loaded from the code text."""
    namespace: dict[str, Any] = {}
    exec(compile(SKETCH_PLANE_CODE, "<snippet>", "exec"), namespace)  # noqa: S102
    return namespace["_mcp_resolve_sketch_plane"]


@pytest.fixture
def two_bodies() -> tuple[FakeDocument, FakeBody, FakeBody, FakeObject]:
    """A document with Body, Body001 (numbered planes) and a datum plane."""
    body = FakeBody("Body", faces=6)
    body2 = FakeBody("Body001", suffix="001")
    datum = FakeObject(
        "DatumPlane",
        ("PartDesign::Plane", "Part::Datum"),
        label="MyDatum",
        parent=body2,
    )
    pad = FakeObject("Pad", ("PartDesign::Pad",), parent=body)
    objects: list[FakeObject] = [body, body2, datum, pad]
    for b in (body, body2):
        objects += b.Origin.OriginFeatures
    return FakeDocument(objects), body, body2, datum


class TestResolveSketchPlane:
    """The resolver finds planes by role, name or label, or raises."""

    @pytest.mark.parametrize("plane", ["XZ_Plane", "XZ", "xz_plane"])
    def test_numbered_origin_plane_found_by_role(self, resolve, two_bodies, plane):
        doc, _, body2, _ = two_bodies
        support, sub, kind, role = resolve(doc, body2, plane)
        assert support.Name == "XZ_Plane001"
        assert (sub, kind, role) == ("", "origin_plane", "XZ_Plane")

    def test_first_body_keeps_unnumbered_plane(self, resolve, two_bodies):
        doc, body, _, _ = two_bodies
        support, _, _, _ = resolve(doc, body, "YZ_Plane")
        assert support.Name == "YZ_Plane"

    def test_falls_back_to_name_without_role(self, resolve):
        body = FakeBody("Body002", suffix="002")
        for feature in body.Origin.OriginFeatures:
            del feature.Role
        doc = FakeDocument([body])
        support, _, kind, _ = resolve(doc, body, "XY_Plane")
        assert support.Name == "XY_Plane002"
        assert kind == "origin_plane"

    @pytest.mark.parametrize("plane", ["DatumPlane", "MyDatum"])
    def test_datum_plane_by_name_or_label(self, resolve, two_bodies, plane):
        doc, _, body2, datum = two_bodies
        support, sub, kind, role = resolve(doc, body2, plane)
        assert support is datum
        assert (sub, kind, role) == ("", "datum_plane", None)

    def test_standalone_standard_plane_is_placed(self, resolve, two_bodies):
        doc, _, _, _ = two_bodies
        assert resolve(doc, None, "XZ_Plane") == (
            None,
            "",
            "standard_plane",
            "XZ_Plane",
        )

    def test_standalone_can_attach_to_datum(self, resolve, two_bodies):
        doc, _, _, datum = two_bodies
        support, _, kind, _ = resolve(doc, None, "DatumPlane")
        assert support is datum
        assert kind == "datum_plane"

    def test_body_face(self, resolve, two_bodies):
        doc, body, _, _ = two_bodies
        assert resolve(doc, body, "Face6") == (body, "Face6", "body_face", None)

    def test_missing_face_raises(self, resolve, two_bodies):
        doc, body, _, _ = two_bodies
        with pytest.raises(ValueError, match="has no Face7"):
            resolve(doc, body, "Face7")

    def test_face_without_body_raises(self, resolve, two_bodies):
        doc, _, _, _ = two_bodies
        with pytest.raises(ValueError, match="needs body_name"):
            resolve(doc, None, "Face1")

    def test_unknown_name_lists_available_planes(self, resolve, two_bodies):
        doc, _, body2, _ = two_bodies
        with pytest.raises(ValueError, match="No plane named 'Nope'") as caught:
            resolve(doc, body2, "Nope")
        assert "XZ_Plane (XZ_Plane001)" in str(caught.value)
        assert "DatumPlane" in str(caught.value)

    def test_non_plane_object_raises(self, resolve, two_bodies):
        doc, body, _, _ = two_bodies
        with pytest.raises(ValueError, match="is a PartDesign::Pad, not a plane"):
            resolve(doc, body, "Pad")

    def test_plane_of_other_body_raises_with_hint(self, resolve, two_bodies):
        doc, body, _, _ = two_bodies
        with pytest.raises(ValueError, match="belongs to 'Body001'") as caught:
            resolve(doc, body, "XZ_Plane001")
        assert "plane='XZ_Plane'" in str(caught.value)

    def test_datum_of_other_body_raises(self, resolve, two_bodies):
        doc, body, _, _ = two_bodies
        with pytest.raises(ValueError, match="same body"):
            resolve(doc, body, "DatumPlane")

    def test_duplicate_label_raises(self, resolve):
        a = FakeObject("P1", ("PartDesign::Plane",), label="Same")
        b = FakeObject("P2", ("PartDesign::Plane",), label="Same")
        with pytest.raises(ValueError, match="Several objects"):
            resolve(FakeDocument([a, b]), None, "Same")


class TestCreateSketchTool:
    """create_sketch sends the resolver and reports what it attached to."""

    @pytest.fixture
    def tools(self) -> tuple[dict[str, Any], AsyncMock]:
        from freecad_mcp.tools.partdesign import register_partdesign_tools

        registered: dict[str, Any] = {}
        mcp = MagicMock()
        mcp.tool = lambda: lambda func: registered.setdefault(func.__name__, func)
        bridge = AsyncMock()

        async def get_bridge() -> AsyncMock:
            return bridge

        register_partdesign_tools(mcp, get_bridge)
        return registered, bridge

    @pytest.mark.asyncio
    async def test_sends_resolver_and_returns_attachment(self, tools):
        registered, bridge = tools
        reply = {
            "name": "Sketch",
            "attached_to": {"name": "XZ_Plane001", "role": "XZ_Plane"},
            "attachment_kind": "origin_plane",
        }
        bridge.execute_python = AsyncMock(return_value=_reply(reply))
        result = await registered["create_sketch"](
            body_name="Body001", plane="XZ_Plane", doc_name="Doc"
        )
        assert result == reply
        code = bridge.execute_python.await_args.args[0]
        assert "def _mcp_resolve_sketch_plane" in code
        assert "_mcp_resolve_sketch_plane(doc, body, 'XZ_Plane')" in code
        # The old exact-name lookup is gone.
        assert "body.Origin.getObject" not in code
        with warnings.catch_warnings():
            # Upstream's "'Doc' is None" lines warn; see CUSTOM_CHANGES.md.
            warnings.simplefilter("ignore", SyntaxWarning)
            compile(code, "<create_sketch>", "exec")

    @pytest.mark.asyncio
    async def test_failure_raises(self, tools):
        registered, bridge = tools
        bridge.execute_python = AsyncMock(
            return_value=_reply(error="ValueError: No plane named 'X'")
        )
        with pytest.raises(ValueError, match="No plane named"):
            await registered["create_sketch"](body_name="Body", plane="X")
