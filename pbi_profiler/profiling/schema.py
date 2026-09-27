"""Metadata/schema-level profiling: shape and hygiene of the model definition
itself, no data access required."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Optional

from ..model import Model
from .dax_deps import UsageInfo, analyze_model

_DEFAULT_NAME_RE = re.compile(r"^(Table|Column|Measure)\s?\d*$", re.IGNORECASE)


@dataclass
class TableSchemaProfile:
    name: str
    is_hidden: bool
    has_description: bool
    column_count: int
    calculated_column_count: int
    measure_count: int
    hierarchy_count: int
    is_calculation_group: bool
    partition_modes: list[str] = field(default_factory=list)


@dataclass
class SchemaProfile:
    table_count: int
    column_count: int
    calculated_column_count: int
    measure_count: int
    relationship_count: int
    bidirectional_relationship_count: int
    inactive_relationship_count: int
    hierarchy_count: int
    role_count: int
    calculation_group_count: int
    data_type_distribution: dict[str, int]
    tables: list[TableSchemaProfile]
    missing_description_tables: list[str]
    missing_description_measures: list[str]
    missing_description_visible_columns: list[str]
    unused_visible_columns: list[str]
    unused_measures: list[str]
    measures_without_format_string: list[str]
    naming_issues: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _has_naming_issue(name: str) -> Optional[str]:
    if name != name.strip():
        return "leading/trailing whitespace in name"
    if _DEFAULT_NAME_RE.match(name.strip()):
        return "looks like an auto-generated default name"
    return None


def compute_schema_profile(model: Model, usage: Optional[UsageInfo] = None) -> SchemaProfile:
    if usage is None:
        usage = analyze_model(model)

    table_profiles: list[TableSchemaProfile] = []
    data_types: Counter = Counter()
    missing_desc_tables: list[str] = []
    missing_desc_measures: list[str] = []
    missing_desc_columns: list[str] = []
    unused_columns: list[str] = []
    unused_measures: list[str] = []
    measures_no_format: list[str] = []
    naming_issues: list[str] = []

    total_columns = 0
    total_calc_columns = 0
    total_measures = 0
    total_hierarchies = 0
    total_calc_groups = 0

    for table in model.tables:
        calc_col_count = sum(1 for c in table.columns if c.is_calculated)
        table_profiles.append(
            TableSchemaProfile(
                name=table.name,
                is_hidden=table.is_hidden,
                has_description=bool(table.description),
                column_count=len(table.columns),
                calculated_column_count=calc_col_count,
                measure_count=len(table.measures),
                hierarchy_count=len(table.hierarchies),
                is_calculation_group=table.is_calculation_group,
                partition_modes=[p.mode for p in table.partitions if p.mode],
            )
        )

        total_columns += len(table.columns)
        total_calc_columns += calc_col_count
        total_measures += len(table.measures)
        total_hierarchies += len(table.hierarchies)
        if table.is_calculation_group:
            total_calc_groups += 1

        if not table.is_hidden and not table.description:
            missing_desc_tables.append(table.name)
        if issue := _has_naming_issue(table.name):
            naming_issues.append(f"Table '{table.name}': {issue}")

        for column in table.columns:
            data_types[column.data_type or "unknown"] += 1
            if not column.is_hidden:
                if not column.description:
                    missing_desc_columns.append(column.qualified_name)
                if not usage.is_column_used(table.name, column.name):
                    unused_columns.append(column.qualified_name)
            if issue := _has_naming_issue(column.name):
                naming_issues.append(f"Column '{column.qualified_name}': {issue}")

        for measure in table.measures:
            if not measure.description:
                missing_desc_measures.append(measure.qualified_name)
            if not measure.format_string:
                measures_no_format.append(measure.qualified_name)
            if not measure.is_hidden and not usage.is_measure_used(measure.name):
                unused_measures.append(measure.qualified_name)
            if issue := _has_naming_issue(measure.name):
                naming_issues.append(f"Measure '{measure.qualified_name}': {issue}")

    bidirectional = sum(
        1 for r in model.relationships if r.cross_filtering_behavior == "bothDirections"
    )
    inactive = sum(1 for r in model.relationships if not r.is_active)

    return SchemaProfile(
        table_count=len(model.tables),
        column_count=total_columns,
        calculated_column_count=total_calc_columns,
        measure_count=total_measures,
        relationship_count=len(model.relationships),
        bidirectional_relationship_count=bidirectional,
        inactive_relationship_count=inactive,
        hierarchy_count=total_hierarchies,
        role_count=len(model.roles),
        calculation_group_count=total_calc_groups,
        data_type_distribution=dict(data_types),
        tables=table_profiles,
        missing_description_tables=missing_desc_tables,
        missing_description_measures=missing_desc_measures,
        missing_description_visible_columns=missing_desc_columns,
        unused_visible_columns=unused_columns,
        unused_measures=unused_measures,
        measures_without_format_string=measures_no_format,
        naming_issues=naming_issues,
    )
