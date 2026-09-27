"""Loads a ReportModel from the older, single-file report layout format: a
JSON blob historically embedded as the `Report/Layout` part inside a .pbix
package (this loader takes a path to that JSON already extracted to its own
file, not a .pbix archive). Superseded by PBIR as Power BI Desktop's default
project format, but still what many existing Analysis Services / Tabular
Editor-era report exports and older `.pbix`-derived tooling produce.

Each visual is a `visualContainer` whose own `config` field is itself a
*stringified* JSON blob (`singleVisual.projections` for role->field
assignment, `singleVisual.prototypeQuery` for the field expressions, with
table references resolved through a `From`-clause alias rather than PBIR's
direct entity name).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .base import ReportLoader
from ._field_expr import extract_field, extract_title
from ..report_model import Page, ReportModel, Visual


def _read_text_flexible(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _visual_from_container(container: dict[str, Any], page_name: str, index: int) -> Optional[Visual]:
    config_str = container.get("config")
    if not config_str:
        return None
    try:
        config = json.loads(config_str)
    except json.JSONDecodeError:
        return None

    single_visual = config.get("singleVisual")
    if single_visual is None:
        # a grouped visual container, or a shape we don't recognize
        return None

    prototype_query = single_visual.get("prototypeQuery", {})
    alias_to_entity = {
        f.get("Name"): f.get("Entity") for f in prototype_query.get("From", []) if f.get("Name")
    }

    def resolve_entity(expr: dict[str, Any]) -> Optional[str]:
        source_ref = (expr or {}).get("SourceRef") or {}
        return alias_to_entity.get(source_ref.get("Source"))

    select_by_name = {
        sel.get("Name"): sel for sel in prototype_query.get("Select", []) if sel.get("Name")
    }

    fields = []
    for role, refs in (single_visual.get("projections") or {}).items():
        for ref in refs:
            select_entry = select_by_name.get(ref.get("queryRef"))
            if select_entry is None:
                continue
            field_ref = extract_field(select_entry, resolve_entity, role=role)
            if field_ref is not None:
                fields.append(field_ref)

    return Visual(
        name=config.get("name", f"visual{index}"),
        page=page_name,
        visual_type=single_visual.get("visualType"),
        title=extract_title(single_visual.get("vcObjects", {})),
        is_hidden=False,
        fields=fields,
    )


class LegacyLayoutReportLoader(ReportLoader):
    """Loads a report from a standalone legacy `Layout` JSON file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> ReportModel:
        layout = json.loads(_read_text_flexible(self.path))

        pages: list[Page] = []
        for ordinal, section in enumerate(layout.get("sections", [])):
            page = Page(
                name=section.get("name", f"Page{ordinal}"),
                display_name=section.get("displayName"),
                ordinal=ordinal,
                is_hidden=section.get("visibility") == 1,
            )
            for i, container in enumerate(section.get("visualContainers", [])):
                visual = _visual_from_container(container, page.name, i)
                if visual is not None:
                    page.visuals.append(visual)
            pages.append(page)

        return ReportModel(name=self.path.stem, source_kind="legacy-layout", pages=pages)
