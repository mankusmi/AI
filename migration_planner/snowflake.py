"""Pre-match shopping-list columns against a Snowflake catalog export.

Export the catalog once (any role that can see the schemas):

    SELECT table_catalog, table_schema, table_name, column_name, data_type
    FROM <db>.information_schema.columns WHERE table_schema <> 'INFORMATION_SCHEMA';

Matching is by normalised name (case/underscore/space-insensitive). It only
*suggests*: rows with `match` of "exact" or "column only" still need a human check.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path


def _n(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_catalog(path: str | Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        rd.fieldnames = [(f or "").strip().lower() for f in rd.fieldnames or []]
        need = {"table_schema", "table_name", "column_name"}
        if need - set(rd.fieldnames):
            raise ValueError(f"catalog CSV needs columns: {', '.join(sorted(need))} (plus table_catalog, data_type)")
        return [{k: (v or "").strip() for k, v in r.items()} for r in rd]


def apply_catalog(rows: list[dict], catalog: list[dict]) -> None:
    """Fill snowflake_* fields on shopping-list rows in place."""
    by_col: dict[str, list[dict]] = {}
    for c in catalog:
        by_col.setdefault(_n(c["column_name"]), []).append(c)
    for r in rows:
        r.update(snowflake_database="", snowflake_schema="", snowflake_table="", snowflake_column="",
                 snowflake_actual_type="", match="", candidates="")
        if r["kind"] != "column":
            continue
        cands = by_col.get(_n(r["pbi_column"]), [])
        if not cands:
            r["match"] = "none"
            continue
        exact = [c for c in cands if _n(c["table_name"]) == _n(r["pbi_table"])]
        pick, label = (exact, "exact") if len(exact) == 1 else (cands, "column only")
        if len(pick) == 1:
            c = pick[0]
            r.update(snowflake_database=c.get("table_catalog", ""), snowflake_schema=c["table_schema"],
                     snowflake_table=c["table_name"], snowflake_column=c["column_name"],
                     snowflake_actual_type=c.get("data_type", ""), match=label)
        else:
            r["match"] = f"ambiguous ({len(pick)})"
            r["candidates"] = "; ".join(f"{c['table_schema']}.{c['table_name']}.{c['column_name']}" for c in pick[:8])
