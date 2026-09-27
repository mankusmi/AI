"""Data-level profiling: row counts, distinct counts, null rates and min/max
per column, computed by running DAX queries against a live model through a
`QueryExecutor`. Not available for purely local TMDL/BIM sources, since those
don't carry any actual data -- only a `LiveModelLoader`'s executor (or any
other `QueryExecutor`) can provide it.

Each table is profiled with a single batched DAX query (one `ROW()` with an
aliased measure per statistic) to minimize round trips against the service.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from ..loaders.base import QueryExecutor
from ..model import Model, Table

_NO_MIN_MAX_TYPES = {"binary"}


@dataclass
class ColumnDataProfile:
    table: str
    column: str
    distinct_count: Optional[int] = None
    null_count: Optional[int] = None
    null_percentage: Optional[float] = None
    min_value: Any = None
    max_value: Any = None


@dataclass
class TableDataProfile:
    table: str
    row_count: Optional[int] = None
    columns: list[ColumnDataProfile] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class DataProfile:
    tables: list[TableDataProfile] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _quote_table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _col_ref(table: str, column: str) -> str:
    return f"{_quote_table(table)}[{column}]"


def _pick(row: dict[str, Any], alias: str) -> Any:
    for candidate in (alias, f"[{alias}]"):
        if candidate in row:
            return row[candidate]
    return None


def _build_table_query(table: Table) -> tuple[str, dict[str, tuple[str, str]]]:
    table_ref = _quote_table(table.name)
    exprs = [f'"RowCount", COUNTROWS({table_ref})']
    alias_map: dict[str, tuple[str, str]] = {}

    for i, col in enumerate(table.columns):
        ref = _col_ref(table.name, col.name)
        distinct_alias, null_alias = f"c{i}_distinct", f"c{i}_nulls"
        exprs.append(f'"{distinct_alias}", DISTINCTCOUNT({ref})')
        exprs.append(f'"{null_alias}", COUNTROWS(FILTER({table_ref}, ISBLANK({ref})))')
        alias_map[distinct_alias] = (col.name, "distinct")
        alias_map[null_alias] = (col.name, "nulls")

        if (col.data_type or "").lower() not in _NO_MIN_MAX_TYPES:
            min_alias, max_alias = f"c{i}_min", f"c{i}_max"
            exprs.append(f'"{min_alias}", MIN({ref})')
            exprs.append(f'"{max_alias}", MAX({ref})')
            alias_map[min_alias] = (col.name, "min")
            alias_map[max_alias] = (col.name, "max")

    query = "EVALUATE\nROW(\n\t" + ",\n\t".join(exprs) + "\n)"
    return query, alias_map


def _profile_table(executor: QueryExecutor, table: Table) -> TableDataProfile:
    if not table.columns:
        try:
            rows = executor.run_dax(f"EVALUATE ROW(\"RowCount\", COUNTROWS({_quote_table(table.name)}))")
            row_count = int(_pick(rows[0], "RowCount")) if rows else None
        except Exception as exc:
            return TableDataProfile(table=table.name, error=str(exc))
        return TableDataProfile(table=table.name, row_count=row_count)

    query, alias_map = _build_table_query(table)
    try:
        rows = executor.run_dax(query)
    except Exception as exc:
        # Batched query failed (e.g. an unsupported aggregation for some
        # column's data type) -- fall back to at least reporting row count.
        try:
            fallback_rows = executor.run_dax(
                f"EVALUATE ROW(\"RowCount\", COUNTROWS({_quote_table(table.name)}))"
            )
            row_count = int(_pick(fallback_rows[0], "RowCount")) if fallback_rows else None
        except Exception:
            return TableDataProfile(table=table.name, error=str(exc))
        return TableDataProfile(
            table=table.name,
            row_count=row_count,
            error=f"per-column stats failed: {exc}",
        )

    if not rows:
        return TableDataProfile(table=table.name, error="query returned no rows")

    row = rows[0]
    row_count_raw = _pick(row, "RowCount")
    row_count = int(row_count_raw) if row_count_raw is not None else None

    stats: dict[str, dict[str, Any]] = {}
    for alias, (col_name, stat) in alias_map.items():
        stats.setdefault(col_name, {})[stat] = _pick(row, alias)

    columns_profile = []
    for col in table.columns:
        s = stats.get(col.name, {})
        null_count = s.get("nulls")
        null_pct = None
        if null_count is not None and row_count:
            null_pct = round((null_count / row_count) * 100, 2)
        columns_profile.append(
            ColumnDataProfile(
                table=table.name,
                column=col.name,
                distinct_count=s.get("distinct"),
                null_count=null_count,
                null_percentage=null_pct,
                min_value=s.get("min"),
                max_value=s.get("max"),
            )
        )

    return TableDataProfile(table=table.name, row_count=row_count, columns=columns_profile)


def compute_data_profile(model: Model, executor: QueryExecutor) -> DataProfile:
    tables = [_profile_table(executor, t) for t in model.tables if not t.is_calculation_group]
    return DataProfile(tables=tables)
