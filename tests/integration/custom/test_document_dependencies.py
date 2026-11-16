"""get_document_dependencies against real FreeCAD documents saved to a temp folder."""

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from freecad_mcp.bridge.xmlrpc import XmlRpcBridge
from freecad_mcp.tools.custom.runtime import run_operation
from tests.integration.auth_support import integration_credentials

pytestmark = pytest.mark.integration

# The documents are built and saved with the FreeCAD API directly, not with
# the tools under test.
_SETUP = """
import os
import FreeCAD

folder = {folder!r}
os.makedirs(os.path.join(folder, "sub"))
target = FreeCAD.newDocument({target!r})
target.addObject("Part::Box", "Box")
target.recompute()
target.saveAs(os.path.join(folder, "target.FCStd"))
owner = FreeCAD.newDocument({owner!r})
owner.saveAs(os.path.join(folder, "sub", "owner.FCStd"))
link = owner.addObject("App::Link", "BoxLink")
link.LinkedObject = target.getObject("Box")
body = owner.addObject("PartDesign::Body", "Body")
binder = body.newObject("PartDesign::SubShapeBinder", "Binder")
binder.Support = [(target.getObject("Box"), ("Face1",))]
owner.addObject("Part::Box", "LocalBox")
owner.recompute()
owner.save()
_result_ = [target.Name, owner.Name]
"""


class Ctx:
    def __init__(self, bridge: XmlRpcBridge, folder: str, target: str, owner: str):
        self.bridge = bridge
        self.folder = folder
        self.target = target
        self.owner = owner

    async def deps(self, **extra: Any) -> dict[str, Any]:
        async def get_bridge() -> XmlRpcBridge:
            return self.bridge

        return await run_operation(
            get_bridge,
            "get_document_dependencies",
            {"doc_name": self.owner, **extra},
        )

    async def run(self, code: str) -> Any:
        result = await self.bridge.execute_python(code + "\n_result_ = True")
        assert result.success, result.error_traceback or result.stderr


@pytest.fixture
async def ctx() -> AsyncIterator[Ctx]:
    suffix = uuid4().hex[:8]
    target, owner = f"DepTarget_{suffix}", f"DepOwner_{suffix}"
    bridge = XmlRpcBridge(**integration_credentials())
    await bridge.connect()
    with tempfile.TemporaryDirectory() as raw_folder:
        folder = str(Path(raw_folder).resolve())  # noqa: ASYNC240
        try:
            setup = await bridge.execute_python(
                _SETUP.format(folder=folder, target=target, owner=owner)
            )
            assert setup.success, setup.error_traceback or setup.stderr
            yield Ctx(bridge, folder, target, owner)
        finally:
            try:
                await bridge.execute_python(
                    "for _n in list(FreeCAD.listDocuments()):\n"
                    f"    if _n.endswith({suffix!r}):\n"
                    "        FreeCAD.closeDocument(_n)\n"
                    "_result_ = True"
                )
            finally:
                await bridge.disconnect()


def _by_object(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f"{link['object']}.{link['property']}": link for link in report["links"]}


async def test_links_resolve_when_target_is_open(ctx: Ctx) -> None:
    report = await ctx.deps()
    links = _by_object(report)
    assert set(links) == {"BoxLink.LinkedObject", "Binder.Support"}
    link = links["BoxLink.LinkedObject"]
    assert link["stored_path"] in ("../target.FCStd", "..\\target.FCStd")
    assert link["stored_path_is_relative"] is True
    assert link["resolved_path"] == os.path.normcase(
        os.path.join(ctx.folder, "target.FCStd")
    )
    assert link["target_object"] == "Box"
    assert link["target_document"] == ctx.target
    assert link["file_exists"] is True
    assert link["object_present"] is True
    assert link["status"] == "resolved"
    assert links["Binder.Support"]["sub_elements"] == "Face1"
    summary = report["summary"]
    assert summary["all_resolve"] is True
    assert summary["link_count"] == 2
    assert summary["broken"] == []
    assert report["document_saved"] is True
    assert report["truncated"] is False


async def test_read_only(ctx: Ctx) -> None:
    state = (
        "d = FreeCAD.getDocument(%r)\n"
        "_result_ = [len(d.Objects), d.isTouched(), len(FreeCAD.listDocuments())]"
    )
    before = await ctx.bridge.execute_python(state % ctx.owner)
    await ctx.deps()
    after = await ctx.bridge.execute_python(state % ctx.owner)
    assert before.success and after.success
    assert before.result == after.result


async def test_closed_target_is_reported_as_not_open(ctx: Ctx) -> None:
    await ctx.run(f"FreeCAD.closeDocument({ctx.target!r})")
    report = await ctx.deps()
    links = _by_object(report)
    assert {link["status"] for link in links.values()} == {"document_not_open"}
    assert links["BoxLink.LinkedObject"]["file_exists"] is True
    assert links["BoxLink.LinkedObject"]["target_document"] is None
    assert links["BoxLink.LinkedObject"]["resolves"] is False
    assert report["summary"]["all_resolve"] is False
    assert report["summary"]["broken_count"] == 0
    assert report["summary"]["not_open_count"] == 2


async def test_renamed_file_is_reported_as_missing(ctx: Ctx) -> None:
    await ctx.run(f"FreeCAD.closeDocument({ctx.target!r})")
    os.rename(
        os.path.join(ctx.folder, "target.FCStd"),
        os.path.join(ctx.folder, "renamed.FCStd"),
    )
    report = await ctx.deps()
    link = _by_object(report)["BoxLink.LinkedObject"]
    assert link["status"] == "missing_file"
    assert link["file_exists"] is False
    assert "not found" in link["reason"]
    assert report["summary"]["broken_count"] == 2
    assert report["summary"]["all_resolve"] is False
    assert report["summary"]["broken"][0]["status"] == "missing_file"


async def test_missing_object_in_target_file(ctx: Ctx) -> None:
    """Replace the target file with one lacking the linked object, then reopen."""
    await ctx.run(
        f"FreeCAD.closeDocument({ctx.owner!r})\n"
        f"FreeCAD.closeDocument({ctx.target!r})\n"
        "import os\n"
        f"_new = FreeCAD.newDocument({ctx.target!r})\n"
        "_new.addObject('Part::Cylinder', 'Cyl')\n_new.recompute()\n"
        f"_new.saveAs(os.path.join({ctx.folder!r}, 'target.FCStd'))\n"
        f"FreeCAD.openDocument(os.path.join({ctx.folder!r}, 'sub', 'owner.FCStd'))"
    )
    ctx.owner = (
        await ctx.bridge.execute_python(
            "_result_ = [n for n in FreeCAD.listDocuments() if n.startswith('DepOwner')][0]"
        )
    ).result
    report = await ctx.deps()
    link = _by_object(report)["BoxLink.LinkedObject"]
    assert link["status"] == "missing_object"
    assert link["object_present"] is False
    assert link["target_document_open"] is True
    assert report["summary"]["broken_count"] >= 1


async def test_max_links_truncates(ctx: Ctx) -> None:
    report = await ctx.deps(max_links=1)
    assert len(report["links"]) == 1
    assert report["truncated"] is True
    assert report["summary"]["link_count"] == 2


async def test_unknown_document_is_an_error(ctx: Ctx) -> None:
    async def get_bridge() -> XmlRpcBridge:
        return ctx.bridge

    with pytest.raises(ValueError, match="Document not found"):
        await run_operation(
            get_bridge, "get_document_dependencies", {"doc_name": "NoSuchDoc"}
        )
