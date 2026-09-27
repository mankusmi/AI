"""Report-layer profiling: a visual inventory, and a usage signal derived
from visual field references (which feeds into `combine_usage` so the
schema profiler's "unused column/measure" checks can see report usage, not
just usage inside the model's own DAX)."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

from ..report_model import ReportModel
from .dax_deps import UsageInfo


@dataclass
class VisualSummary:
    page: str
    name: str
    visual_type: str | None
    title: str | None
    is_hidden: bool
    field_count: int


@dataclass
class PageSummary:
    name: str
    display_name: str | None
    is_hidden: bool
    visual_count: int


@dataclass
class ReportProfile:
    report_name: str
    source_kind: str
    page_count: int
    visual_count: int
    hidden_page_count: int
    visuals_by_type: dict[str, int]
    pages: list[PageSummary] = field(default_factory=list)
    visuals: list[VisualSummary] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def compute_report_profile(report: ReportModel) -> ReportProfile:
    visuals_by_type: Counter = Counter()
    pages: list[PageSummary] = []
    visuals: list[VisualSummary] = []

    for page in report.pages:
        pages.append(
            PageSummary(
                name=page.name,
                display_name=page.display_name,
                is_hidden=page.is_hidden,
                visual_count=len(page.visuals),
            )
        )
        for v in page.visuals:
            visuals_by_type[v.visual_type or "unknown"] += 1
            visuals.append(
                VisualSummary(
                    page=page.name,
                    name=v.name,
                    visual_type=v.visual_type,
                    title=v.title,
                    is_hidden=v.is_hidden,
                    field_count=len(v.distinct_fields()),
                )
            )

    return ReportProfile(
        report_name=report.name,
        source_kind=report.source_kind,
        page_count=len(report.pages),
        visual_count=len(report.all_visuals()),
        hidden_page_count=sum(1 for p in report.pages if p.is_hidden),
        visuals_by_type=dict(visuals_by_type),
        pages=pages,
        visuals=visuals,
    )


def usage_from_report(report: ReportModel) -> UsageInfo:
    """Every column/measure referenced by any visual on any page, regardless
    of query role -- a category axis, a filter, a tooltip field, etc. are all
    "used" for the purposes of the unused-object checks."""
    usage = UsageInfo()
    for visual in report.all_visuals():
        for f in visual.fields:
            if not f.table or not f.field:
                continue
            if f.kind == "measure":
                usage.referenced_measures.add(f.field)
            else:
                usage.referenced_columns.add((f.table, f.field))
    return usage
