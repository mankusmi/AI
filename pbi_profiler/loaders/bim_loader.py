"""Loads a Model from a legacy Model.bim / TMSL JSON file.

TMSL (Tabular Model Scripting Language) is the JSON representation used by
Analysis Services Tabular projects and by Tabular Editor's "Model.bim" export.
It is a much simpler read than TMDL since it's just JSON.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import ModelLoader
from ..model import (
    Column,
    Hierarchy,
    Measure,
    Model,
    Partition,
    Relationship,
    Role,
    Table,
    TablePermission,
    CalculationItem,
)

_CALCULATED_COLUMN_TYPES = {"calculated", "calculatedTableColumn"}


def _column_from_json(col: dict[str, Any], table_name: str) -> Column:
    return Column(
        name=col["name"],
        table=table_name,
        data_type=col.get("dataType"),
        is_hidden=bool(col.get("isHidden", False)),
        is_calculated=col.get("type") in _CALCULATED_COLUMN_TYPES,
        is_key=bool(col.get("isKey", False)),
        source_column=col.get("sourceColumn"),
        expression=_join_expr(col.get("expression")),
        format_string=col.get("formatString"),
        summarize_by=col.get("summarizeBy"),
        display_folder=col.get("displayFolder"),
        description=col.get("description"),
        data_category=col.get("dataCategory"),
        sort_by_column=col.get("sortByColumn"),
    )


def _join_expr(expression: Any) -> str | None:
    if expression is None:
        return None
    if isinstance(expression, list):
        return "\n".join(expression)
    return str(expression)


def _measure_from_json(m: dict[str, Any], table_name: str) -> Measure:
    return Measure(
        name=m["name"],
        table=table_name,
        expression=_join_expr(m.get("expression")),
        format_string=m.get("formatString"),
        is_hidden=bool(m.get("isHidden", False)),
        display_folder=m.get("displayFolder"),
        description=m.get("description"),
    )


def _hierarchy_from_json(h: dict[str, Any], table_name: str) -> Hierarchy:
    levels = [lvl.get("column", lvl.get("name", "")) for lvl in h.get("levels", [])]
    return Hierarchy(
        name=h["name"],
        table=table_name,
        is_hidden=bool(h.get("isHidden", False)),
        levels=levels,
    )


def _table_from_json(t: dict[str, Any]) -> Table:
    table = Table(
        name=t["name"],
        is_hidden=bool(t.get("isHidden", False)),
        description=t.get("description"),
    )
    for col in t.get("columns", []):
        table.columns.append(_column_from_json(col, table.name))
    for m in t.get("measures", []):
        table.measures.append(_measure_from_json(m, table.name))
    for h in t.get("hierarchies", []):
        table.hierarchies.append(_hierarchy_from_json(h, table.name))
    for p in t.get("partitions", []):
        table.partitions.append(Partition(name=p.get("name", ""), mode=p.get("mode")))
    calc_group = t.get("calculationGroup")
    if calc_group:
        table.is_calculation_group = True
        for item in calc_group.get("calculationItems", []):
            table.calculation_items.append(
                CalculationItem(name=item.get("name", ""), expression=_join_expr(item.get("expression")))
            )
    return table


def _normalize_cross_filter(value: str | None) -> str:
    if value == "bothDirections":
        return "bothDirections"
    return "single"


def _relationship_from_json(r: dict[str, Any]) -> Relationship:
    return Relationship(
        from_table=r.get("fromTable", ""),
        from_column=r.get("fromColumn", ""),
        to_table=r.get("toTable", ""),
        to_column=r.get("toColumn", ""),
        is_active=bool(r.get("isActive", True)),
        cross_filtering_behavior=_normalize_cross_filter(r.get("crossFilteringBehavior")),
        from_cardinality=r.get("fromCardinality"),
        to_cardinality=r.get("toCardinality"),
    )


def _role_from_json(r: dict[str, Any]) -> Role:
    perms = [
        TablePermission(table=tp.get("name", ""), filter_expression=tp.get("filterExpression"))
        for tp in r.get("tablePermissions", [])
    ]
    return Role(
        name=r["name"],
        model_permission=r.get("modelPermission"),
        table_permissions=perms,
    )


class BimModelLoader(ModelLoader):
    """Loads a semantic model from a Model.bim / TMSL JSON file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> Model:
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        model_json = raw.get("model", raw)

        tables = [_table_from_json(t) for t in model_json.get("tables", [])]
        relationships = [_relationship_from_json(r) for r in model_json.get("relationships", [])]
        roles = [_role_from_json(r) for r in model_json.get("roles", [])]

        return Model(
            name=raw.get("name", self.path.stem),
            source_kind="bim",
            culture=model_json.get("culture"),
            tables=tables,
            relationships=relationships,
            roles=roles,
        )
