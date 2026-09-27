"""Loads a Model from a PBIP-style TMDL `definition/` folder."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .base import ModelLoader
from .tmdl_parser import TmdlNode, parse_tmdl
from ..model import (
    CalculationItem,
    Column,
    Hierarchy,
    Measure,
    Model,
    Partition,
    Relationship,
    Role,
    Table,
    TablePermission,
)

_BRACKET_REF_RE = re.compile(r"^'?([^'\[]+?)'?\[([^\]]+)\]$")
_DOT_REF_RE = re.compile(r"^'([^']+)'\.(.+)$|^([^.\[\]]+)\.(.+)$")


def _clean_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _split_qualified(ref: Optional[str]) -> tuple[str, str]:
    """Split a `Table[Column]` or `Table.Column` (TMDL relationship style) reference."""
    if not ref:
        return "", ""
    ref = ref.strip()
    m = _BRACKET_REF_RE.match(ref)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = _DOT_REF_RE.match(ref)
    if m:
        table = m.group(1) or m.group(3)
        column = m.group(2) or m.group(4)
        return table.strip(), column.strip()
    return ref, ""


def _column_from_node(node: TmdlNode, table_name: str) -> Column:
    is_calculated = node.value is not None or node.is_expression
    return Column(
        name=node.args,
        table=table_name,
        data_type=node.prop("dataType"),
        is_hidden=node.flag("isHidden", False),
        is_calculated=is_calculated,
        is_key=node.flag("isKey", False),
        source_column=node.prop("sourceColumn"),
        expression=node.value if is_calculated else None,
        format_string=_clean_str(node.prop("formatString")),
        summarize_by=node.prop("summarizeBy"),
        display_folder=_clean_str(node.prop("displayFolder")),
        description=_clean_str(node.prop("description")),
        data_category=node.prop("dataCategory"),
        sort_by_column=node.prop("sortByColumn"),
    )


def _measure_from_node(node: TmdlNode, table_name: str) -> Measure:
    return Measure(
        name=node.args,
        table=table_name,
        expression=node.value,
        format_string=_clean_str(node.prop("formatString")),
        is_hidden=node.flag("isHidden", False),
        display_folder=_clean_str(node.prop("displayFolder")),
        description=_clean_str(node.prop("description")),
    )


def _hierarchy_from_node(node: TmdlNode, table_name: str) -> Hierarchy:
    # a level's header is `level <LevelName> = <SourceColumn>`; the column
    # actually referenced is the value, which only differs from the level's
    # own display name when they've been renamed (e.g. `level Month = MonthName`)
    levels = [(lvl.value or lvl.args) for lvl in node.find_all("level")]
    return Hierarchy(
        name=node.args,
        table=table_name,
        is_hidden=node.flag("isHidden", False),
        levels=levels,
    )


def table_from_node(node: TmdlNode) -> Table:
    table = Table(
        name=node.args,
        is_hidden=node.flag("isHidden", False),
        description=_clean_str(node.prop("description")),
    )
    for col_node in node.find_all("column"):
        table.columns.append(_column_from_node(col_node, table.name))
    for m_node in node.find_all("measure"):
        table.measures.append(_measure_from_node(m_node, table.name))
    for h_node in node.find_all("hierarchy"):
        table.hierarchies.append(_hierarchy_from_node(h_node, table.name))
    for p_node in node.find_all("partition"):
        table.partitions.append(Partition(name=p_node.args, mode=_clean_str(p_node.prop("mode"))))
    cg_node = node.find("calculationGroup")
    if cg_node is not None:
        table.is_calculation_group = True
        for ci_node in cg_node.find_all("calculationItem"):
            table.calculation_items.append(
                CalculationItem(name=ci_node.args, expression=ci_node.value)
            )
    return table


def relationship_from_node(node: TmdlNode) -> Relationship:
    from_table, from_column = _split_qualified(node.prop("fromColumn"))
    to_table, to_column = _split_qualified(node.prop("toColumn"))
    return Relationship(
        from_table=from_table,
        from_column=from_column,
        to_table=to_table,
        to_column=to_column,
        is_active=node.flag("isActive", True),
        cross_filtering_behavior=node.prop("crossFilteringBehavior", "single") or "single",
        from_cardinality=node.prop("fromCardinality"),
        to_cardinality=node.prop("toCardinality"),
    )


def role_from_node(node: TmdlNode) -> Role:
    perms = [
        TablePermission(table=tp.args, filter_expression=tp.value)
        for tp in node.find_all("tablePermission")
    ]
    return Role(
        name=node.args,
        model_permission=node.prop("modelPermission"),
        table_permissions=perms,
    )


def _resolve_definition_dir(path: Path) -> Path:
    if (path / "definition").is_dir():
        return path / "definition"
    if path.name == "definition" and path.is_dir():
        return path
    if (path / "tables").is_dir():
        return path
    # maybe path is a .SemanticModel/definition/tables folder itself
    if path.is_dir() and path.name == "tables":
        return path.parent
    raise FileNotFoundError(
        f"Could not find a TMDL 'definition' folder (with a 'tables' subfolder) under {path}"
    )


class TmdlModelLoader(ModelLoader):
    """Loads a semantic model from a PBIP `<Name>.SemanticModel/definition` folder
    (or the `definition` folder itself, or any ancestor containing it)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> Model:
        definition_dir = _resolve_definition_dir(self.path)

        model_name = definition_dir.parent.name
        if model_name.endswith(".SemanticModel"):
            model_name = model_name[: -len(".SemanticModel")]

        culture = None
        model_file = definition_dir / "model.tmdl"
        if model_file.is_file():
            model_nodes = parse_tmdl(model_file.read_text(encoding="utf-8-sig"))
            model_node = next((n for n in model_nodes if n.keyword == "model"), None)
            if model_node is not None:
                culture = model_node.prop("culture")

        tables: list[Table] = []
        tables_dir = definition_dir / "tables"
        if tables_dir.is_dir():
            for tmdl_file in sorted(tables_dir.glob("*.tmdl")):
                nodes = parse_tmdl(tmdl_file.read_text(encoding="utf-8-sig"))
                for node in nodes:
                    if node.keyword == "table":
                        tables.append(table_from_node(node))

        relationships: list[Relationship] = []
        rel_file = definition_dir / "relationships.tmdl"
        if rel_file.is_file():
            nodes = parse_tmdl(rel_file.read_text(encoding="utf-8-sig"))
            for node in nodes:
                if node.keyword == "relationship":
                    relationships.append(relationship_from_node(node))

        roles: list[Role] = []
        roles_file = definition_dir / "roles.tmdl"
        role_files = [roles_file] if roles_file.is_file() else []
        roles_dir = definition_dir / "roles"
        if roles_dir.is_dir():
            role_files.extend(sorted(roles_dir.glob("*.tmdl")))
        for rf in role_files:
            nodes = parse_tmdl(rf.read_text(encoding="utf-8-sig"))
            for node in nodes:
                if node.keyword == "role":
                    roles.append(role_from_node(node))

        return Model(
            name=model_name,
            source_kind="tmdl",
            culture=culture,
            tables=tables,
            relationships=relationships,
            roles=roles,
        )
