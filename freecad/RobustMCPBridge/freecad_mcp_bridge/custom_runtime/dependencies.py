"""Report on the links from a document to other files.

Read-only: it inspects link properties and checks that paths exist, and
never opens, reads, copies or saves files.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

DEFAULT_MAX_LINKS = 200


def _stored_xlinks(obj: Any, prop: str) -> list[dict[str, Any]]:
    """Read the stored file, object and sub-element of an XLink property.

    An unresolved XLink reads as None in Python, so the stored target is
    taken from the property's saved content instead.

    Args:
        obj: The FreeCAD object owning the property.
        prop: Property name.

    Returns:
        One dictionary per link with ``file``, ``name`` and ``sub``.
    """
    import io
    import re
    import zipfile

    try:
        data = bytes(obj.dumpPropertyContent(prop))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            text = "".join(
                archive.read(name).decode("utf-8", "replace")
                for name in archive.namelist()
            )
    except Exception:
        return []
    found = []
    for element in re.findall(r"<XLink\b[^>]*>", text):
        attrs = {
            key: html.unescape(value)
            for key, value in re.findall(r'(\w+)="([^"]*)"', element)
        }
        found.append(
            {
                "file": attrs.get("file", ""),
                "name": attrs.get("name", ""),
                "sub": attrs.get("sub", ""),
            }
        )
    return found


def _resolve_path(owner_doc: Any, stored: str) -> str:
    """Return the absolute, normalised path for a stored link path."""
    base = Path(getattr(owner_doc, "FileName", "") or "").parent
    path = Path(stored)
    if not path.is_absolute():
        path = base / path
    return os.path.normcase(str(path.resolve()))


def _find_open_document(path: str) -> Any:
    """Return the open document saved at ``path``, or None."""
    import FreeCAD

    for candidate in FreeCAD.listDocuments().values():
        saved = getattr(candidate, "FileName", "") or ""
        if saved and os.path.normcase(str(Path(saved).resolve())) == path:
            return candidate
    return None


def _describe(owner_doc: Any, obj: Any, prop: str, entry: dict[str, Any]) -> dict:
    """Build the report entry for one stored link and judge whether it works."""
    stored = entry["file"]
    resolved = _resolve_path(owner_doc, stored)
    target_doc = _find_open_document(resolved)
    file_exists = Path(resolved).exists()
    object_present: bool | None = None
    if target_doc is not None:
        object_present = target_doc.getObject(entry["name"]) is not None
    if target_doc is None and not file_exists:
        status = "missing_file"
        reason = f"Linked file not found: {resolved}"
    elif target_doc is None:
        status = "document_not_open"
        reason = "Linked file exists but is not open, so the link is not restored"
    elif not object_present:
        status = "missing_object"
        reason = f"Object '{entry['name']}' not found in document '{target_doc.Name}'"
    else:
        status = "resolved"
        reason = None
    result: dict[str, Any] = {
        "object": obj.Name,
        "label": obj.Label,
        "type_id": obj.TypeId,
        "property": prop,
        "stored_path": stored,
        "stored_path_is_relative": not Path(stored).is_absolute(),
        "resolved_path": resolved,
        "target_object": entry["name"],
        "sub_elements": entry["sub"] or None,
        "target_document": target_doc.Name if target_doc is not None else None,
        "target_document_open": target_doc is not None,
        "file_exists": file_exists,
        "object_present": object_present,
        "status": status,
        "resolves": status == "resolved",
    }
    if reason:
        result["reason"] = reason
    return result


def _property_names(obj: Any) -> list[str]:
    """Return the object's property names, or an empty list if unreadable."""
    try:
        return list(obj.PropertiesList)
    except Exception:
        return []


def _property_type(obj: Any, prop: str) -> str:
    """Return a property's type name, or an empty string if unreadable."""
    try:
        return str(obj.getTypeIdOfProperty(prop))
    except Exception:
        return ""


def _foreign_object_links(doc: Any, obj: Any, prop: str) -> list[dict[str, Any]]:
    """Find plain object links (not XLinks) that point into another document."""
    try:
        value = getattr(obj, prop)
    except Exception:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    targets = []
    for item in items:
        candidate = item[0] if isinstance(item, tuple) and item else item
        if (
            hasattr(candidate, "Document")
            and hasattr(candidate, "Name")
            and candidate.Document is not doc
        ):
            targets.append(candidate)
    found = []
    for target in targets:
        file_name = getattr(target.Document, "FileName", "") or ""
        found.append(
            {
                "object": obj.Name,
                "label": obj.Label,
                "type_id": obj.TypeId,
                "property": prop,
                "stored_path": file_name,
                "stored_path_is_relative": False,
                "resolved_path": file_name,
                "target_object": target.Name,
                "sub_elements": None,
                "target_document": target.Document.Name,
                "target_document_open": True,
                "file_exists": bool(file_name) and Path(file_name).exists(),
                "object_present": True,
                "status": "resolved",
                "resolves": True,
            }
        )
    return found


def get_document_dependencies(args: dict[str, Any]) -> dict[str, Any]:
    """List every link from a document to another file, and whether it works.

    Args:
        args: ``doc_name`` (optional, default the active document) and
            ``max_links`` (optional, default 200).

    Returns:
        Dictionary describing the document, the links and a summary.

    Raises:
        ValueError: If the document cannot be found.
    """
    import FreeCAD

    doc_name = args.get("doc_name")
    doc = FreeCAD.ActiveDocument if doc_name is None else None
    if doc_name is not None:
        doc = FreeCAD.listDocuments().get(doc_name)
    if doc is None:
        raise ValueError(
            f"Document not found: {doc_name!r}"
            if doc_name is not None
            else "No active document"
        )
    max_links = int(args.get("max_links", DEFAULT_MAX_LINKS))
    if max_links < 1:
        raise ValueError("max_links must be at least 1")

    own_file = getattr(doc, "FileName", "") or ""
    links: list[dict[str, Any]] = []
    for obj in doc.Objects:
        for prop in _property_names(obj):
            type_id = _property_type(obj, prop)
            if "XLink" in type_id:
                for entry in _stored_xlinks(obj, prop):
                    if entry["file"] and entry["name"]:
                        links.append(_describe(doc, obj, prop, entry))
            elif type_id.startswith("App::PropertyLink"):
                links.extend(_foreign_object_links(doc, obj, prop))

    broken = [
        link for link in links if link["status"] in ("missing_file", "missing_object")
    ]
    not_open = [link for link in links if link["status"] == "document_not_open"]
    files = sorted({link["resolved_path"] for link in links})
    truncated = len(links) > max_links
    summary = {
        "all_resolve": all(link["resolves"] for link in links),
        "link_count": len(links),
        "broken_count": len(broken),
        "not_open_count": len(not_open),
        "linked_files": files,
        "broken": [
            {
                "object": link["object"],
                "property": link["property"],
                "status": link["status"],
                "reason": link["reason"],
            }
            for link in broken
        ],
        "not_open": [
            {"object": link["object"], "property": link["property"]}
            for link in not_open
        ],
    }
    result: dict[str, Any] = {
        "document": doc.Name,
        "document_file": own_file or None,
        "document_saved": bool(own_file),
        "summary": summary,
        "links": links[:max_links],
        "truncated": truncated,
    }
    if not own_file and links:
        result["note"] = (
            "This document has not been saved, so relative link paths cannot "
            "be resolved. Save it before relying on these results."
        )
    return result


OPERATIONS = {"get_document_dependencies": get_document_dependencies}
