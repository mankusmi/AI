"""A small best-practice-analyzer-style rule engine, in the spirit of Tabular
Editor's BPA: a fixed list of `Rule`s, each a pure function over the model
(plus optional data profile) that returns zero or more `Finding`s.

Rules that need per-column statistics (row counts, null %, distinct counts)
are marked `requires_data=True` and are skipped automatically when no
`DataProfile` was computed (e.g. profiling an offline TMDL/BIM file).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from ..model import Model
from .dax_deps import UsageInfo, analyze_model
from .data import DataProfile

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_AGGREGATION_CALL_RE = re.compile(
    r"\b(SUM|SUMX|AVERAGE|AVERAGEX|CALCULATE|COUNTROWS|COUNT|COUNTX|DISTINCTCOUNT|MIN|MAX)\s*\(",
    re.IGNORECASE,
)
# a '/' that isn't immediately preceded by another '/' (comment) -- a rough
# proxy for "raw division", refined by simply checking DIVIDE(...) isn't used
_RAW_DIVISION_RE = re.compile(r"(?<!/)/(?!/)")


@dataclass
class Finding:
    rule_id: str
    severity: str
    category: str
    object_type: str
    object_name: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RuleContext:
    model: Model
    usage: UsageInfo
    data_profile: Optional[DataProfile] = None

    def data_for(self, table: str, column: str):
        if not self.data_profile:
            return None
        for t in self.data_profile.tables:
            if t.table == table:
                for c in t.columns:
                    if c.column == column:
                        return c
        return None

    def row_count(self, table: str) -> Optional[int]:
        if not self.data_profile:
            return None
        for t in self.data_profile.tables:
            if t.table == table:
                return t.row_count
        return None


@dataclass
class Rule:
    id: str
    name: str
    category: str
    severity: str
    description: str
    check: Callable[..., list[Finding]]  # (RuleContext, Rule) -> list[Finding]
    requires_data: bool = False


def _finding(rule: Rule, object_type: str, object_name: str, message: str) -> Finding:
    return Finding(
        rule_id=rule.id,
        severity=rule.severity,
        category=rule.category,
        object_type=object_type,
        object_name=object_name,
        message=message,
    )


# --------------------------------------------------------------------------
# Metadata-only rules
# --------------------------------------------------------------------------


def _check_missing_table_description(ctx: RuleContext, rule: Rule) -> list[Finding]:
    return [
        _finding(rule, "table", t.name, "Visible table has no description.")
        for t in ctx.model.tables
        if not t.is_hidden and not t.description
    ]


def _check_missing_measure_description(ctx: RuleContext, rule: Rule) -> list[Finding]:
    return [
        _finding(rule, "measure", m.qualified_name, "Visible measure has no description.")
        for t in ctx.model.tables
        for m in t.measures
        if not m.is_hidden and not m.description
    ]


def _check_missing_column_description(ctx: RuleContext, rule: Rule) -> list[Finding]:
    return [
        _finding(rule, "column", c.qualified_name, "Visible column has no description.")
        for t in ctx.model.tables
        for c in t.columns
        if not c.is_hidden and not c.description
    ]


def _check_unused_visible_column(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []
    for t in ctx.model.tables:
        for c in t.columns:
            if not c.is_hidden and not ctx.usage.is_column_used(t.name, c.name):
                findings.append(
                    _finding(
                        rule,
                        "column",
                        c.qualified_name,
                        "Visible column isn't referenced by any measure, calculated column, "
                        "relationship, sort-by-column or hierarchy in the model. It may only be "
                        "used directly in reports (not visible to this static analysis), or it "
                        "may be a candidate for hiding/removal.",
                    )
                )
    return findings


def _check_unused_measure(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []
    for t in ctx.model.tables:
        for m in t.measures:
            if not m.is_hidden and not ctx.usage.is_measure_used(m.name):
                findings.append(
                    _finding(
                        rule,
                        "measure",
                        m.qualified_name,
                        "Measure isn't referenced by any other measure or RLS filter. It may "
                        "still be used directly in reports (not visible to this static "
                        "analysis) -- verify before removing.",
                    )
                )
    return findings


def _check_bidirectional_relationship(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []
    for r in ctx.model.relationships:
        if r.cross_filtering_behavior == "bothDirections":
            name = f"{r.from_table}[{r.from_column}] -> {r.to_table}[{r.to_column}]"
            findings.append(
                _finding(
                    rule,
                    "relationship",
                    name,
                    "Bidirectional cross-filtering can cause ambiguous filter propagation and "
                    "hurt query performance; confirm it's actually required.",
                )
            )
    return findings


def _check_inactive_relationship(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []
    for r in ctx.model.relationships:
        if not r.is_active:
            name = f"{r.from_table}[{r.from_column}] -> {r.to_table}[{r.to_column}]"
            findings.append(
                _finding(
                    rule,
                    "relationship",
                    name,
                    "Inactive relationship: only takes effect where a measure explicitly "
                    "activates it with USERELATIONSHIP. Confirm that happens somewhere, "
                    "otherwise this relationship is dead weight.",
                )
            )
    return findings


def _check_measure_missing_format_string(ctx: RuleContext, rule: Rule) -> list[Finding]:
    return [
        _finding(rule, "measure", m.qualified_name, "Visible measure has no format string.")
        for t in ctx.model.tables
        for m in t.measures
        if not m.is_hidden and not m.format_string
    ]


def _check_naive_division(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []
    for t in ctx.model.tables:
        for m in t.measures:
            expr = m.expression or ""
            if _RAW_DIVISION_RE.search(expr) and "DIVIDE(" not in expr.upper():
                findings.append(
                    _finding(
                        rule,
                        "measure",
                        m.qualified_name,
                        "Expression uses '/' directly instead of DIVIDE(...); this raises an "
                        "error (or returns infinity) when the denominator is 0 or blank.",
                    )
                )
    return findings


def _check_calculated_column_aggregation(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []
    for t in ctx.model.tables:
        for c in t.columns:
            if c.is_calculated and c.expression and _AGGREGATION_CALL_RE.search(c.expression):
                findings.append(
                    _finding(
                        rule,
                        "column",
                        c.qualified_name,
                        "Calculated column expression contains an aggregation function; this "
                        "is computed and stored per-row at refresh time. Consider a measure "
                        "instead if a per-row stored value isn't actually required.",
                    )
                )
    return findings


def _check_isolated_table(ctx: RuleContext, rule: Rule) -> list[Finding]:
    connected = set()
    for r in ctx.model.relationships:
        connected.add(r.from_table)
        connected.add(r.to_table)
    findings = []
    for t in ctx.model.tables:
        if t.is_calculation_group or t.is_hidden:
            continue
        if t.name not in connected:
            findings.append(
                _finding(
                    rule,
                    "table",
                    t.name,
                    "Table has no relationships to any other table in the model.",
                )
            )
    return findings


def _check_calculation_group_without_items(ctx: RuleContext, rule: Rule) -> list[Finding]:
    return [
        _finding(rule, "table", t.name, "Calculation group has no calculation items.")
        for t in ctx.model.tables
        if t.is_calculation_group and not t.calculation_items
    ]


def _check_naming_issues(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []

    def issue(name: str) -> Optional[str]:
        if name != name.strip():
            return "leading/trailing whitespace in name"
        if re.match(r"^(Table|Column|Measure)\s?\d*$", name.strip(), re.IGNORECASE):
            return "looks like an auto-generated default name"
        return None

    for t in ctx.model.tables:
        if msg := issue(t.name):
            findings.append(_finding(rule, "table", t.name, f"Naming issue: {msg}."))
        for c in t.columns:
            if msg := issue(c.name):
                findings.append(_finding(rule, "column", c.qualified_name, f"Naming issue: {msg}."))
        for m in t.measures:
            if msg := issue(m.name):
                findings.append(_finding(rule, "measure", m.qualified_name, f"Naming issue: {msg}."))
    return findings


# --------------------------------------------------------------------------
# Data-dependent rules
# --------------------------------------------------------------------------


def _check_empty_table(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []
    for t in ctx.model.tables:
        if t.is_calculation_group:
            continue
        row_count = ctx.row_count(t.name)
        if row_count is not None and row_count == 0:
            findings.append(_finding(rule, "table", t.name, "Table has 0 rows."))
    return findings


def _check_high_null_percentage(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []
    for t in ctx.model.tables:
        for c in t.columns:
            if c.is_hidden:
                continue
            data = ctx.data_for(t.name, c.name)
            if data and data.null_percentage is not None and data.null_percentage > 90:
                findings.append(
                    _finding(
                        rule,
                        "column",
                        c.qualified_name,
                        f"{data.null_percentage}% of values are blank/null.",
                    )
                )
    return findings


def _check_potential_key_column(ctx: RuleContext, rule: Rule) -> list[Finding]:
    findings = []
    for t in ctx.model.tables:
        row_count = ctx.row_count(t.name)
        if not row_count:
            continue
        for c in t.columns:
            if c.is_hidden or c.is_key:
                continue
            data = ctx.data_for(t.name, c.name)
            if data and data.distinct_count is not None and data.distinct_count == row_count:
                findings.append(
                    _finding(
                        rule,
                        "column",
                        c.qualified_name,
                        "Every value is unique (distinct count equals row count); this looks "
                        "like a key/ID column. Consider hiding it if it's not meant for "
                        "report-level grouping.",
                    )
                )
    return findings


ALL_RULES: list[Rule] = [
    Rule(
        "missing-table-description",
        "Missing table description",
        "documentation",
        SEVERITY_INFO,
        "Visible tables should have a description.",
        _check_missing_table_description,
    ),
    Rule(
        "missing-measure-description",
        "Missing measure description",
        "documentation",
        SEVERITY_INFO,
        "Visible measures should have a description.",
        _check_missing_measure_description,
    ),
    Rule(
        "missing-column-description",
        "Missing column description",
        "documentation",
        SEVERITY_INFO,
        "Visible columns should have a description.",
        _check_missing_column_description,
    ),
    Rule(
        "unused-visible-column",
        "Unused visible column",
        "maintainability",
        SEVERITY_WARNING,
        "Visible columns not referenced anywhere in the model's own DAX/relationships.",
        _check_unused_visible_column,
    ),
    Rule(
        "unused-measure",
        "Unused measure",
        "maintainability",
        SEVERITY_INFO,
        "Measures not referenced by any other measure or RLS filter.",
        _check_unused_measure,
    ),
    Rule(
        "bidirectional-relationship",
        "Bidirectional relationship",
        "performance",
        SEVERITY_WARNING,
        "Bidirectional cross-filtering relationships.",
        _check_bidirectional_relationship,
    ),
    Rule(
        "inactive-relationship",
        "Inactive relationship",
        "maintainability",
        SEVERITY_INFO,
        "Inactive relationships that require USERELATIONSHIP to take effect.",
        _check_inactive_relationship,
    ),
    Rule(
        "measure-missing-format-string",
        "Measure missing format string",
        "documentation",
        SEVERITY_INFO,
        "Visible measures should have an explicit format string.",
        _check_measure_missing_format_string,
    ),
    Rule(
        "naive-division",
        "Unsafe division",
        "performance",
        SEVERITY_WARNING,
        "Measures using '/' instead of DIVIDE(...) risk divide-by-zero errors.",
        _check_naive_division,
    ),
    Rule(
        "calculated-column-aggregation",
        "Calculated column uses aggregation",
        "performance",
        SEVERITY_WARNING,
        "Calculated columns that aggregate are computed and stored per-row at refresh time.",
        _check_calculated_column_aggregation,
    ),
    Rule(
        "isolated-table",
        "Isolated table",
        "maintainability",
        SEVERITY_WARNING,
        "Tables with no relationships to the rest of the model.",
        _check_isolated_table,
    ),
    Rule(
        "calculation-group-without-items",
        "Empty calculation group",
        "maintainability",
        SEVERITY_WARNING,
        "Calculation groups with no calculation items.",
        _check_calculation_group_without_items,
    ),
    Rule(
        "naming-issue",
        "Naming issue",
        "naming",
        SEVERITY_INFO,
        "Default/auto-generated names or stray whitespace in object names.",
        _check_naming_issues,
    ),
    Rule(
        "empty-table",
        "Empty table",
        "data-quality",
        SEVERITY_WARNING,
        "Tables with zero rows.",
        _check_empty_table,
        requires_data=True,
    ),
    Rule(
        "high-null-percentage",
        "High null percentage",
        "data-quality",
        SEVERITY_WARNING,
        "Visible columns with more than 90% blank/null values.",
        _check_high_null_percentage,
        requires_data=True,
    ),
    Rule(
        "potential-key-column",
        "Potential key column",
        "data-quality",
        SEVERITY_INFO,
        "Visible, non-key columns whose distinct count equals the table's row count.",
        _check_potential_key_column,
        requires_data=True,
    ),
]


def run_rules(
    model: Model,
    data_profile: Optional[DataProfile] = None,
    usage: Optional[UsageInfo] = None,
    rules: Optional[list[Rule]] = None,
) -> list[Finding]:
    if usage is None:
        usage = analyze_model(model)
    ctx = RuleContext(model=model, usage=usage, data_profile=data_profile)
    findings: list[Finding] = []
    for rule in rules or ALL_RULES:
        if rule.requires_data and data_profile is None:
            continue
        findings.extend(rule.check(ctx, rule))
    return findings


def list_rules() -> list[dict]:
    return [
        {
            "id": r.id,
            "name": r.name,
            "category": r.category,
            "severity": r.severity,
            "description": r.description,
            "requires_data": r.requires_data,
        }
        for r in ALL_RULES
    ]
