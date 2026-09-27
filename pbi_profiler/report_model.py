"""Backend-agnostic representation of the *report* layer of a Power BI file --
pages and the visuals on them -- as opposed to `model.py`, which represents
the semantic *model* (dataset) a report queries.

Two loaders build this: `PbirReportLoader` (the modern PBIR
`<Name>.Report/definition` folder) and `LegacyLayoutReportLoader` (the older
single embedded `Layout` JSON blob). Both normalize into these same
dataclasses so visual inventory, usage cross-checking and report-level rules
never need to know which format the report came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class FieldRef:
    """A single field referenced by a visual, resolved as best as possible to
    a (table, field) pair from the semantic model. `role` is the visual's
    query role for this field (e.g. "Category", "Y", "Values", "Tooltips")
    when the source format exposes one. `kind` distinguishes a plain column
    from a measure, an aggregated column, or a hierarchy level."""

    table: Optional[str]
    field: Optional[str]
    kind: str = "column"  # "column" | "measure" | "aggregation" | "hierarchy_level" | "unknown"
    role: Optional[str] = None

    @property
    def qualified_name(self) -> str:
        if self.table and self.field:
            return f"{self.table}[{self.field}]"
        return self.field or "?"


@dataclass
class Visual:
    name: str
    page: str
    visual_type: Optional[str] = None
    title: Optional[str] = None
    is_hidden: bool = False
    fields: list[FieldRef] = field(default_factory=list)

    def distinct_fields(self) -> list[tuple[Optional[str], Optional[str]]]:
        seen = []
        for f in self.fields:
            key = (f.table, f.field)
            if key not in seen:
                seen.append(key)
        return seen


@dataclass
class Page:
    name: str
    display_name: Optional[str] = None
    ordinal: Optional[int] = None
    is_hidden: bool = False
    visuals: list[Visual] = field(default_factory=list)


@dataclass
class ReportModel:
    name: str = "Report"
    source_kind: str = "unknown"  # "pbir" | "legacy-layout"
    pages: list[Page] = field(default_factory=list)

    def all_visuals(self) -> list[Visual]:
        return [v for p in self.pages for v in p.visuals]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
