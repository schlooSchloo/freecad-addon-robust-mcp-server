"""Unit tests for spreadsheet readers/writers (generated code run on a fake sheet)."""

import json
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from freecad_mcp.bridge.base import ExecutionResult
from freecad_mcp.tools.spreadsheet import register_spreadsheet_tools


class FakeQuantity:
    """Stand-in for FreeCAD.Quantity (not JSON-serialisable)."""

    Value = 40.0
    Unit = object()
    UserString = "40.00 mm"

    def getUserPreferred(self) -> tuple[str, float, str]:
        return ("40.00 mm", 1.0, "mm")


class FakeSheet:
    """Minimal Spreadsheet::Sheet double."""

    Name = "S"
    PropertiesList: ClassVar[list[str]] = []

    def __init__(self) -> None:
        self.contents = {"A1": "=40 mm", "B4": "5"}
        self.aliases = {"B4": "ballJointRad"}
        self.values: dict[str, Any] = {"A1": FakeQuantity(), "B4": 5}
        self.fail_alias = False

    def set(self, cell: str, text: str) -> None:
        self.contents[cell] = text

    def get(self, cell: str) -> Any:
        if cell not in self.values:
            raise ValueError("Invalid cell address or property: " + cell)
        return self.values[cell]

    def getContents(self, cell: str) -> str:
        return self.contents.get(cell, "")

    def getAlias(self, cell: str) -> str | None:
        if self.fail_alias:
            raise RuntimeError("boom")
        return self.aliases.get(cell)

    def getUsedCells(self) -> list[str]:
        return list(self.contents)


def _run(code: str, sheet: FakeSheet) -> dict[str, Any]:
    doc = MagicMock()
    doc.getObject.return_value = sheet
    freecad = MagicMock()
    freecad.ActiveDocument = doc
    freecad.getDocument.return_value = doc
    ns: dict[str, Any] = {"FreeCAD": freecad}
    exec(code, ns)  # noqa: S102
    return ns["_result_"]


class TestSpreadsheetReaders:
    @pytest.fixture
    def tools(self) -> tuple[dict[str, Callable[..., Awaitable[Any]]], list[str]]:
        mcp = MagicMock()
        tools: dict[str, Callable[..., Awaitable[Any]]] = {}

        def deco(**_kw: Any) -> Callable[..., Any]:
            def wrap(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Any]:
                tools[fn.__name__] = fn
                return fn

            return wrap

        mcp.tool = deco
        codes: list[str] = []
        bridge = MagicMock()

        async def execute(code: str, **_kw: Any) -> ExecutionResult:
            codes.append(code)
            return ExecutionResult(
                success=True,
                result={"ok": True},
                stdout="",
                stderr="",
                execution_time_ms=1,
            )

        bridge.execute_python = AsyncMock(side_effect=execute)

        async def get_bridge() -> Any:
            return bridge

        register_spreadsheet_tools(mcp, get_bridge)
        return tools, codes

    async def test_get_cell_reports_alias(self, tools: Any) -> None:
        fns, codes = tools
        await fns["spreadsheet_get_cell"]("S", "B4", doc_name="d")
        res = _run(codes[-1], FakeSheet())
        assert res["alias"] == "ballJointRad"
        assert res["computed"] == 5

    async def test_get_cell_no_alias_is_none(self, tools: Any) -> None:
        fns, codes = tools
        await fns["spreadsheet_get_cell"]("S", "A1", doc_name="d")
        res = _run(codes[-1], FakeSheet())
        assert res["alias"] is None
        assert res["computed"] == {"value": 40.0, "unit": "mm", "text": "40.00 mm"}
        json.dumps(res)

    async def test_get_cell_alias_failure_raises(self, tools: Any) -> None:
        fns, codes = tools
        await fns["spreadsheet_get_cell"]("S", "B4", doc_name="d")
        sheet = FakeSheet()
        sheet.fail_alias = True
        with pytest.raises(RuntimeError):
            _run(codes[-1], sheet)

    async def test_get_aliases(self, tools: Any) -> None:
        fns, codes = tools
        await fns["spreadsheet_get_aliases"]("S", doc_name="d")
        res = _run(codes[-1], FakeSheet())
        assert res["aliases"] == {"ballJointRad": "B4"}
        assert res["count"] == 1

    async def test_get_aliases_failure_raises(self, tools: Any) -> None:
        fns, codes = tools
        await fns["spreadsheet_get_aliases"]("S", doc_name="d")
        sheet = FakeSheet()
        sheet.fail_alias = True
        with pytest.raises(RuntimeError):
            _run(codes[-1], sheet)

    async def test_set_cell_quantity_is_json_safe(self, tools: Any) -> None:
        fns, codes = tools
        await fns["spreadsheet_set_cell"]("S", "A1", "=40mm", doc_name="d")
        res = _run(codes[-1], FakeSheet())
        assert res["success"] is True
        assert res["computed"]["unit"] == "mm"
        json.dumps(res)

    async def test_set_cell_readback_failure_is_not_failure(self, tools: Any) -> None:
        fns, codes = tools
        await fns["spreadsheet_set_cell"]("S", "Z9", "x", doc_name="d")
        res = _run(codes[-1], FakeSheet())
        assert res["success"] is True
        assert res["computed"] is None
        assert "computed_error" in res

    def test_docstring_states_angle_bracket_quoting(self, tools: Any) -> None:
        fns, _ = tools
        assert "<<" in (fns["spreadsheet_set_cell"].__doc__ or "")
