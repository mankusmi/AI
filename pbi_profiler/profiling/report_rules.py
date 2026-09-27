"""Report-level best-practice rules: same `Finding` shape as the model-level
rule engine (`rules.py`), but checking the report's pages/visuals instead of
(or, for `broken-field-reference`, in cross-reference with) the semantic
model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..model import Model
from ..report_model import ReportModel
from .rules import Finding, SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING

_NO_TITLE_NEEDED_TYPES = {"slicer", "textbox", "image", "actionButton", "shape", "basicShape"}
_HIGH_FIELD_COUNT_THRESHOLD = 8


@dataclass
class ReportRuleContext:
    report: ReportModel
    model: Optional[Model] = None


@dataclass
class ReportRule:
    id: str
    name: str
    category: str
    severity: str
    description: str
    check: Callable[..., list[Finding]]  # (ReportRuleContext, ReportRule) -> list[Finding]
    requires_model: bool = False


def _finding(rule: ReportRule, object_type: str, object_name: str, message: str) -> Finding:
    return Finding(
        rule_id=rule.id,
        severity=rule.severity,
        category=rule.category,
        object_type=object_type,
        object_name=object_name,
        message=message,
    )


def _check_missing_title(ctx: ReportRuleContext, rule: ReportRule) -> list[Finding]:
    findings = []
    for page in ctx.report.pages:
        for v in page.visuals:
            if v.is_hidden or (v.visual_type or "") in _NO_TITLE_NEEDED_TYPES:
                continue
            if not v.title:
                findings.append(
                    _finding(
                        rule,
                        "visual",
                        f"{page.name}/{v.name}",
                        f"Visual ({v.visual_type or 'unknown type'}) has no title.",
                    )
                )
    return findings


def _check_empty_page(ctx: ReportRuleContext, rule: ReportRule) -> list[Finding]:
    return [
        _finding(rule, "page", page.name, "Page has no visuals.")
        for page in ctx.report.pages
        if not page.visuals
    ]


def _check_high_field_count(ctx: ReportRuleContext, rule: ReportRule) -> list[Finding]:
    findings = []
    for page in ctx.report.pages:
        for v in page.visuals:
            n = len(v.distinct_fields())
            if n > _HIGH_FIELD_COUNT_THRESHOLD:
                findings.append(
                    _finding(
                        rule,
                        "visual",
                        f"{page.name}/{v.name}",
                        f"Visual uses {n} distinct fields; consider simplifying for readability and query performance.",
                    )
                )
    return findings


def _check_duplicate_visuals(ctx: ReportRuleContext, rule: ReportRule) -> list[Finding]:
    findings = []
    for page in ctx.report.pages:
        seen: dict[tuple, str] = {}
        for v in page.visuals:
            key = (v.visual_type, frozenset(v.distinct_fields()))
            if key in seen:
                findings.append(
                    _finding(
                        rule,
                        "visual",
                        f"{page.name}/{v.name}",
                        f"Looks like a duplicate of visual '{seen[key]}' on the same page "
                        "(same visual type and same fields).",
                    )
                )
            else:
                seen[key] = v.name
    return findings


def _check_broken_field_reference(ctx: ReportRuleContext, rule: ReportRule) -> list[Finding]:
    if ctx.model is None:
        return []
    findings = []
    measure_names = {m.name for m in ctx.model.all_measures()}
    for page in ctx.report.pages:
        for v in page.visuals:
            for f in v.fields:
                if not f.table:
                    continue
                object_name = f"{page.name}/{v.name}: {f.qualified_name}"
                table = ctx.model.get_table(f.table)
                if table is None:
                    findings.append(
                        _finding(rule, "visual", object_name, f"References table '{f.table}' which doesn't exist in the model.")
                    )
                    continue
                if not f.field:
                    continue
                if f.kind == "measure":
                    if f.field not in measure_names:
                        findings.append(
                            _finding(rule, "visual", object_name, f"References measure '{f.field}' which doesn't exist in the model.")
                        )
                elif table.get_column(f.field) is None:
                    findings.append(
                        _finding(
                            rule,
                            "visual",
                            object_name,
                            f"References column '{f.field}' which doesn't exist on table '{f.table}'.",
                        )
                    )
    return findings


ALL_REPORT_RULES: list[ReportRule] = [
    ReportRule(
        "visual-missing-title",
        "Visual missing title",
        "documentation",
        SEVERITY_INFO,
        "Visible visuals (of types that usually need one) should have a title.",
        _check_missing_title,
    ),
    ReportRule(
        "empty-page",
        "Empty page",
        "maintainability",
        SEVERITY_WARNING,
        "Pages with no visuals.",
        _check_empty_page,
    ),
    ReportRule(
        "high-field-count-visual",
        "Visual uses many fields",
        "performance",
        SEVERITY_WARNING,
        f"Visuals using more than {_HIGH_FIELD_COUNT_THRESHOLD} distinct fields.",
        _check_high_field_count,
    ),
    ReportRule(
        "duplicate-visual",
        "Possible duplicate visual",
        "maintainability",
        SEVERITY_INFO,
        "Visuals on the same page with the same type and the same fields.",
        _check_duplicate_visuals,
    ),
    ReportRule(
        "broken-field-reference",
        "Broken field reference",
        "data-quality",
        SEVERITY_ERROR,
        "Visual field references pointing at a table/column/measure that doesn't exist in the model.",
        _check_broken_field_reference,
        requires_model=True,
    ),
]


def run_report_rules(
    report: ReportModel,
    model: Optional[Model] = None,
    rules: Optional[list[ReportRule]] = None,
) -> list[Finding]:
    ctx = ReportRuleContext(report=report, model=model)
    findings: list[Finding] = []
    for rule in rules or ALL_REPORT_RULES:
        if rule.requires_model and model is None:
            continue
        findings.extend(rule.check(ctx, rule))
    return findings


def list_report_rules() -> list[dict]:
    return [
        {
            "id": r.id,
            "name": r.name,
            "category": r.category,
            "severity": r.severity,
            "description": r.description,
            "requires_model": r.requires_model,
        }
        for r in ALL_REPORT_RULES
    ]
