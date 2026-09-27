"""Backend-agnostic representation of a Power BI / Analysis Services tabular semantic model.

Every loader (TMDL, BIM/TMSL, live REST) builds one of these `Model` objects, so
profiling, rule-checking, and reporting code never has to know where the model
came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Column:
    name: str
    table: str
    data_type: Optional[str] = None
    is_hidden: bool = False
    is_calculated: bool = False
    is_key: bool = False
    source_column: Optional[str] = None
    expression: Optional[str] = None
    format_string: Optional[str] = None
    summarize_by: Optional[str] = None
    display_folder: Optional[str] = None
    description: Optional[str] = None
    data_category: Optional[str] = None
    sort_by_column: Optional[str] = None

    @property
    def qualified_name(self) -> str:
        return f"{self.table}[{self.name}]"


@dataclass
class Measure:
    name: str
    table: str
    expression: Optional[str] = None
    format_string: Optional[str] = None
    is_hidden: bool = False
    display_folder: Optional[str] = None
    description: Optional[str] = None

    @property
    def qualified_name(self) -> str:
        return f"{self.table}[{self.name}]"


@dataclass
class Hierarchy:
    name: str
    table: str
    is_hidden: bool = False
    levels: list[str] = field(default_factory=list)


@dataclass
class CalculationItem:
    name: str
    expression: Optional[str] = None


@dataclass
class Partition:
    name: str
    mode: Optional[str] = None


@dataclass
class Table:
    name: str
    is_hidden: bool = False
    description: Optional[str] = None
    is_calculation_group: bool = False
    columns: list[Column] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    hierarchies: list[Hierarchy] = field(default_factory=list)
    partitions: list[Partition] = field(default_factory=list)
    calculation_items: list[CalculationItem] = field(default_factory=list)

    def get_column(self, name: str) -> Optional[Column]:
        return next((c for c in self.columns if c.name == name), None)

    def get_measure(self, name: str) -> Optional[Measure]:
        return next((m for m in self.measures if m.name == name), None)


@dataclass
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True
    cross_filtering_behavior: str = "single"  # "single" | "bothDirections"
    from_cardinality: Optional[str] = None
    to_cardinality: Optional[str] = None


@dataclass
class TablePermission:
    table: str
    filter_expression: Optional[str] = None


@dataclass
class Role:
    name: str
    model_permission: Optional[str] = None
    table_permissions: list[TablePermission] = field(default_factory=list)


@dataclass
class Model:
    name: str = "Model"
    source_kind: str = "unknown"  # "tmdl" | "bim" | "live"
    culture: Optional[str] = None
    tables: list[Table] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)

    def get_table(self, name: str) -> Optional[Table]:
        return next((t for t in self.tables if t.name == name), None)

    def all_columns(self) -> list[Column]:
        return [c for t in self.tables for c in t.columns]

    def all_measures(self) -> list[Measure]:
        return [m for t in self.tables for m in t.measures]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
