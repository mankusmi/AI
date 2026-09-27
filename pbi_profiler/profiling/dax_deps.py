"""Best-effort static analysis of DAX expressions to find which columns and
measures are referenced from *inside* the model (other measures, calculated
columns, calculation items, RLS filters, relationships, sort-by-columns and
hierarchy levels).

This is intentionally simple regex-based scanning, not a real DAX parser: DAX
syntax is large (nested functions, variables, string literals that can contain
bracket-looking text, etc.) and a full parser is out of scope for a profiling
tool. It is a reasonable proxy for "is this referenced anywhere in the model's
own formulas", with two known blind spots worth calling out wherever this is
surfaced (the rules engine does so):

* Columns/measures used only inside Power BI *report* visuals (not in any
  other DAX expression) will look "unused" here even though a report
  consumes them directly -- this module has no visibility into reports.
* A bare ``[Name]`` reference is ambiguous between a measure and a same-table
  column; both are marked as referenced to avoid false "unused" positives.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..model import Model

_QUALIFIED_REF_RE = re.compile(r"(?:'([^']+)'|([A-Za-z_][A-Za-z0-9_]*))\[([^\[\]]+)\]")
_BARE_REF_RE = re.compile(r"(?<![\w'])\[([^\[\]]+)\]")


def find_references(expression: str) -> tuple[set[tuple[str, str]], set[str]]:
    """Return (qualified_column_refs, bare_name_refs) found in a DAX expression."""
    qualified: set[tuple[str, str]] = set()
    for m in _QUALIFIED_REF_RE.finditer(expression):
        table = m.group(1) or m.group(2)
        column = m.group(3)
        qualified.add((table, column))
    bare: set[str] = set()
    for m in _BARE_REF_RE.finditer(expression):
        bare.add(m.group(1))
    return qualified, bare


@dataclass
class UsageInfo:
    referenced_columns: set[tuple[str, str]] = field(default_factory=set)
    referenced_measures: set[str] = field(default_factory=set)

    def is_column_used(self, table: str, column: str) -> bool:
        return (table, column) in self.referenced_columns

    def is_measure_used(self, name: str) -> bool:
        return name in self.referenced_measures


def combine_usage(*usages: UsageInfo) -> UsageInfo:
    """Merge multiple usage signals (e.g. the model's own DAX plus report
    visual field references) into one. A column/measure counts as used if
    *any* source says so."""
    combined = UsageInfo()
    for usage in usages:
        combined.referenced_columns |= usage.referenced_columns
        combined.referenced_measures |= usage.referenced_measures
    return combined


def analyze_model(model: Model) -> UsageInfo:
    measure_names = {m.name for m in model.all_measures()}
    columns_by_table = {t.name: {c.name for c in t.columns} for t in model.tables}

    usage = UsageInfo()

    def process(expression: str | None, owner_table: str | None) -> None:
        if not expression:
            return
        qualified, bare = find_references(expression)
        usage.referenced_columns.update(qualified)
        for name in bare:
            if name in measure_names:
                usage.referenced_measures.add(name)
            if owner_table and name in columns_by_table.get(owner_table, ()):
                usage.referenced_columns.add((owner_table, name))

    for table in model.tables:
        for measure in table.measures:
            process(measure.expression, table.name)
        for column in table.columns:
            if column.is_calculated:
                process(column.expression, table.name)
        for item in table.calculation_items:
            process(item.expression, table.name)

    for role in model.roles:
        for perm in role.table_permissions:
            process(perm.filter_expression, perm.table)

    for rel in model.relationships:
        usage.referenced_columns.add((rel.from_table, rel.from_column))
        usage.referenced_columns.add((rel.to_table, rel.to_column))

    for table in model.tables:
        for column in table.columns:
            if column.sort_by_column:
                usage.referenced_columns.add((table.name, column.sort_by_column))
        for hierarchy in table.hierarchies:
            for level in hierarchy.levels:
                usage.referenced_columns.add((table.name, level))

    return usage
