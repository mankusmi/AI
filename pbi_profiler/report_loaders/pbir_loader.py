"""Loads a ReportModel from a PBIR (Power BI Enhanced Report Format) project
folder: `<Name>.Report/definition/pages/<pageId>/visuals/<visualId>/visual.json`.

This is the report-layer counterpart to the TMDL model loader: both are the
JSON/text folder structures Power BI Desktop writes when a file is saved as
a "Power BI Project" (PBIP). The exact PBIR schema isn't fully published by
Microsoft; the shapes read here (queryState roles/projections, the
Column/Measure/Aggregation/HierarchyLevel field expressions, and the title
object) reflect the community-documented, empirically stable structure. As
with the live loader's DAX INFO functions, reads are defensive: a page or
visual whose JSON doesn't match the expected shape is skipped rather than
aborting the whole load.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .base import ReportLoader
from ._field_expr import extract_field, extract_title
from ..report_model import Page, ReportModel, Visual


def _resolve_entity(expr: dict[str, Any]) -> Optional[str]:
    source_ref = (expr or {}).get("SourceRef") or {}
    return source_ref.get("Entity")


def _visual_from_json(visual_json: dict[str, Any], page_name: str, visual_id: str) -> Visual:
    visual_block = visual_json.get("visual", {})
    query_state = visual_block.get("query", {}).get("queryState", {}) or {}

    fields = []
    for role, role_obj in query_state.items():
        for projection in (role_obj or {}).get("projections", []):
            ref = extract_field(projection.get("field"), _resolve_entity, role=role)
            if ref is not None:
                fields.append(ref)

    return Visual(
        name=visual_json.get("name", visual_id),
        page=page_name,
        visual_type=visual_block.get("visualType"),
        title=extract_title(visual_block.get("objects", {})),
        is_hidden=bool(visual_json.get("isHidden", False)),
        fields=fields,
    )


def _resolve_definition_dir(path: Path) -> Path:
    if (path / "definition" / "pages").is_dir():
        return path / "definition"
    if path.name == "definition" and (path / "pages").is_dir():
        return path
    if path.name == "pages" and path.is_dir():
        return path.parent
    raise FileNotFoundError(
        f"Could not find a PBIR 'definition/pages' folder under {path}"
    )


class PbirReportLoader(ReportLoader):
    """Loads a report from a PBIR `<Name>.Report/definition` folder (or the
    `definition` folder itself, or any ancestor containing it)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> ReportModel:
        definition_dir = _resolve_definition_dir(self.path)
        pages_dir = definition_dir / "pages"

        report_name = definition_dir.parent.name
        if report_name.endswith(".Report"):
            report_name = report_name[: -len(".Report")]

        page_order: list[str] = []
        pages_index_file = pages_dir / "pages.json"
        if pages_index_file.is_file():
            try:
                index = json.loads(pages_index_file.read_text(encoding="utf-8-sig"))
                page_order = index.get("pageOrder", [])
            except (json.JSONDecodeError, OSError):
                pass

        page_dirs = sorted(
            (d for d in pages_dir.iterdir() if d.is_dir()),
            key=lambda d: (page_order.index(d.name) if d.name in page_order else len(page_order), d.name),
        )

        pages: list[Page] = []
        for page_dir in page_dirs:
            page_json_file = page_dir / "page.json"
            page_json: dict[str, Any] = {}
            if page_json_file.is_file():
                try:
                    page_json = json.loads(page_json_file.read_text(encoding="utf-8-sig"))
                except (json.JSONDecodeError, OSError):
                    page_json = {}

            page = Page(
                name=page_json.get("name", page_dir.name),
                display_name=page_json.get("displayName"),
                ordinal=page_order.index(page_dir.name) if page_dir.name in page_order else None,
                is_hidden=str(page_json.get("visibility", "")).lower().startswith("hidden"),
            )

            visuals_dir = page_dir / "visuals"
            if visuals_dir.is_dir():
                for visual_dir in sorted(visuals_dir.iterdir()):
                    visual_json_file = visual_dir / "visual.json"
                    if not visual_json_file.is_file():
                        continue
                    try:
                        visual_json = json.loads(visual_json_file.read_text(encoding="utf-8-sig"))
                    except (json.JSONDecodeError, OSError):
                        continue
                    page.visuals.append(_visual_from_json(visual_json, page.name, visual_dir.name))

            pages.append(page)

        return ReportModel(name=report_name, source_kind="pbir", pages=pages)
