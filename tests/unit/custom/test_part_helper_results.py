"""Tests for the Part helper fixes in tools/objects.py.

Covers scale_object (native Part::Scale), extrude_shape (Part::Extrusion with
a solid option), the outline returned by slice_shape and section_shape, the
``parametric`` field on helper results, and the rotation convention stated in
set_placement and rotate_object.
"""

from __future__ import annotations

import inspect
import warnings
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult


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


def _compile(code: str) -> None:
    with warnings.catch_warnings():
        # Upstream's "'Doc' is None" lines warn; see CUSTOM_CHANGES.md.
        warnings.simplefilter("ignore", SyntaxWarning)
        compile(code, "<tool>", "exec")


@pytest.fixture
def tools() -> tuple[dict[str, Any], AsyncMock]:
    """Object tools registered against a mock bridge."""
    from freecad_mcp.tools.objects import register_object_tools

    registered: dict[str, Any] = {}
    mcp = MagicMock()
    mcp.tool = lambda: lambda func: registered.setdefault(func.__name__, func)
    bridge = AsyncMock()
    bridge.execute_python = AsyncMock(return_value=_reply({"name": "Result"}))

    async def get_bridge() -> AsyncMock:
        return bridge

    register_object_tools(mcp, get_bridge)
    return registered, bridge


def _sent_code(bridge: AsyncMock) -> str:
    code = bridge.execute_python.await_args.args[0]
    _compile(code)
    return code


class TestScaleObject:
    """scale_object creates a parametric Part::Scale and checks the result."""

    @pytest.mark.asyncio
    async def test_uniform_uses_part_scale(self, tools):
        registered, bridge = tools
        await registered["scale_object"](object_name="Box", scale=1.1575)
        code = _sent_code(bridge)
        assert 'doc.addObject("Part::Scale", result_name)' in code
        assert "result.Base = obj" in code
        assert "sx, sy, sz = 1.1575, 1.1575, 1.1575" in code
        assert "uniform = True" in code
        assert "use_native = True and native_available" in code
        assert '"Part::Scale" in doc.supportedTypes()' in code
        assert "def _mcp_shape_report" in code
        assert '"expected_volume": expected_volume' in code

    @pytest.mark.asyncio
    async def test_non_uniform_list(self, tools):
        registered, bridge = tools
        await registered["scale_object"](object_name="Box", scale=[2, 1, 0.5])
        code = _sent_code(bridge)
        assert "sx, sy, sz = 2.0, 1.0, 0.5" in code
        assert "uniform = False" in code

    @pytest.mark.asyncio
    async def test_equal_list_is_uniform(self, tools):
        registered, bridge = tools
        await registered["scale_object"](object_name="Box", scale=[3, 3, 3])
        assert "uniform = True" in _sent_code(bridge)

    @pytest.mark.asyncio
    async def test_static_option(self, tools):
        registered, bridge = tools
        await registered["scale_object"](object_name="Box", scale=2.0, parametric=False)
        code = _sent_code(bridge)
        assert "use_native = False and native_available" in code
        # The static path scales exactly for uniform factors.
        assert "scaled_shape.scale(sx)" in code

    @pytest.mark.asyncio
    async def test_returns_bridge_result(self, tools):
        registered, bridge = tools
        reply = {"name": "Box_scaled", "parametric": True, "valid": True}
        bridge.execute_python = AsyncMock(return_value=_reply(reply))
        assert await registered["scale_object"](object_name="Box", scale=2) == reply

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("scale", "message"),
        [
            (0, "positive"),
            (-1.5, "positive"),
            ([1, 0, 1], "positive"),
            (float("nan"), "positive"),
            ([1, 2], "list"),
        ],
    )
    async def test_rejects_bad_factors_before_bridge(self, tools, scale, message):
        registered, bridge = tools
        with pytest.raises(ValueError, match=message):
            await registered["scale_object"](object_name="Box", scale=scale)
        bridge.execute_python.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_raises(self, tools):
        registered, bridge = tools
        bridge.execute_python = AsyncMock(
            return_value=_reply(error="ValueError: Scaling 'Box' produced no shape")
        )
        with pytest.raises(ValueError, match="produced no shape"):
            await registered["scale_object"](object_name="Box", scale=2)


class TestExtrudeShape:
    """extrude_shape uses Part::Extrusion and reports the result shape."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("solid", [None, True, False])
    async def test_builds_part_extrusion(self, tools, solid):
        registered, bridge = tools
        await registered["extrude_shape"](
            object_name="Wire", direction=[0, 3, 4], solid=solid
        )
        code = _sent_code(bridge)
        assert 'doc.addObject("Part::Extrusion", result_name)' in code
        assert "result.Base = obj" in code
        assert "result.LengthFwd = 5.0" in code
        assert "FreeCAD.Vector(0.0, 3.0, 4.0).normalize()" in code
        assert f"solid = {solid!r}" in code
        assert "solid = profile_closed" in code
        assert '"solid_count": report["solid_count"]' in code
        assert '"parametric": True' in code

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("direction", "message"),
        [([0, 0, 0], "not be zero"), ([0, 1], "three numbers")],
    )
    async def test_rejects_bad_direction(self, tools, direction, message):
        registered, bridge = tools
        with pytest.raises(ValueError, match=message):
            await registered["extrude_shape"](object_name="Wire", direction=direction)
        bridge.execute_python.assert_not_awaited()

    def test_default_documented(self, tools):
        registered, _ = tools
        doc = inspect.getdoc(registered["extrude_shape"]) or ""
        assert "solid=None (default)" in doc
        assert "open wire" in doc


class TestSliceOutline:
    """slice_shape and section_shape return outline geometry."""

    @pytest.mark.asyncio
    async def test_slice_sends_outline_with_cap(self, tools):
        registered, bridge = tools
        await registered["slice_shape"](
            object_name="Box",
            plane_point=[0, 0, 5],
            plane_normal=[0, 0, 1],
            max_edges=7,
        )
        code = _sent_code(bridge)
        assert "def _mcp_outline" in code
        assert "outline = _mcp_outline(section_shape, 7)" in code
        assert '"outline": outline' in code
        # Existing fields are kept.
        assert '"type_id": result.TypeId' in code

    @pytest.mark.asyncio
    async def test_section_passes_cap(self, tools):
        registered, bridge = tools
        await registered["section_shape"](
            object_name="Box", plane="XZ", offset=2.0, max_edges=3
        )
        code = _sent_code(bridge)
        assert "outline = _mcp_outline(section_shape, 3)" in code
        assert "FreeCAD.Vector(0.0, 2.0, 0.0)" in code

    @pytest.mark.asyncio
    async def test_default_cap_is_200(self, tools):
        registered, bridge = tools
        await registered["section_shape"](object_name="Box")
        assert "_mcp_outline(section_shape, 200)" in _sent_code(bridge)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"max_edges": -1}, "max_edges"),
            ({"plane_normal": [0, 0, 0]}, "not be zero"),
            ({"plane_point": [0, 0]}, "three numbers"),
        ],
    )
    async def test_rejects_bad_arguments(self, tools, kwargs, message):
        registered, bridge = tools
        arguments = {
            "object_name": "Box",
            "plane_point": [0, 0, 0],
            "plane_normal": [0, 0, 1],
            **kwargs,
        }
        with pytest.raises(ValueError, match=message):
            await registered["slice_shape"](**arguments)
        bridge.execute_python.assert_not_awaited()

    def test_outline_helper_caps_and_reports_truncation(self):
        from freecad_mcp.tools.custom.shape_snippets import OUTLINE_CODE

        assert '"truncated": returned < total' in OUTLINE_CODE
        assert '"radius"' in OUTLINE_CODE
        assert '"center"' in OUTLINE_CODE
        _compile(OUTLINE_CODE)


STATIC_HELPERS = {
    "copy_object": {"object_name": "Box"},
    "mirror_object": {"object_name": "Box"},
    "shell_object": {"object_name": "Box", "thickness": 1.0},
    "offset_3d": {"object_name": "Box", "offset": 1.0},
    "make_compound": {"object_names": ["A", "B"]},
    "explode_compound": {"object_name": "Box"},
    "fuse_all": {"object_names": ["A", "B"]},
    "common_all": {"object_names": ["A", "B"]},
    "make_face": {"object_name": "Wire"},
    "revolve_shape": {
        "object_name": "Wire",
        "axis_point": [0, 0, 0],
        "axis_direction": [0, 0, 1],
    },
    "part_loft": {"profile_names": ["A", "B"]},
    "part_sweep": {"profile_name": "A", "spine_name": "B"},
    "slice_shape": {
        "object_name": "Box",
        "plane_point": [0, 0, 0],
        "plane_normal": [0, 0, 1],
    },
}


class TestParametricField:
    """Each source-based helper says whether its result is a static copy."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("name", "kwargs"), sorted(STATIC_HELPERS.items()))
    async def test_static_helpers_report_false(self, tools, name, kwargs):
        registered, bridge = tools
        await registered[name](**kwargs)
        assert '"parametric": False' in _sent_code(bridge)
        doc = inspect.getdoc(registered[name]) or ""
        assert "won't update when the source changes" in doc

    @pytest.mark.asyncio
    async def test_boolean_operation_reports_true(self, tools):
        registered, bridge = tools
        await registered["boolean_operation"](
            operation="cut", object1_name="A", object2_name="B"
        )
        assert '"parametric": True' in _sent_code(bridge)
        doc = inspect.getdoc(registered["boolean_operation"]) or ""
        assert "updates" in doc

    @pytest.mark.parametrize("name", ["scale_object", "extrude_shape"])
    def test_parametric_helpers_document_it(self, tools, name):
        registered, _ = tools
        doc = inspect.getdoc(registered[name]) or ""
        assert "updates when the source changes" in doc


class TestRotationDocs:
    """The rotation convention is stated where the AI client reads it."""

    def test_set_placement_states_axes_and_absolute(self, tools):
        registered, _ = tools
        doc = inspect.getdoc(registered["set_placement"]) or ""
        assert "FreeCAD.Rotation(yaw, pitch, roll)" in doc
        assert "yaw about Z, pitch about Y, roll about X" in doc
        assert "Absolute" in doc

    def test_rotate_object_states_incremental(self, tools):
        registered, _ = tools
        doc = inspect.getdoc(registered["rotate_object"]) or ""
        assert "Incremental" in doc
        assert "[yaw, pitch, roll]" in doc
