"""SQL console and data profiling helpers over a DuckDB cursor."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import uuid

import duckdb

READ_ONLY_TYPES = {duckdb.StatementType.SELECT, duckdb.StatementType.EXPLAIN}
NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UBIGINT", "UINTEGER", "USMALLINT",
           "UTINYINT", "UHUGEINT", "DOUBLE", "FLOAT", "REAL", "DECIMAL")


def jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            return str(v)
        return v
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return f"<BLOB {len(b):,} bytes>"
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, uuid.UUID):
        return str(v)
    return str(v)


def run_sql(cur, sql: str, limit: int = 1000, allow_write: bool = False) -> dict:
    """Run exactly one statement. Without ``allow_write`` only SELECT-like statements are accepted."""
    stmts = cur.extract_statements(sql)
    if len(stmts) != 1:
        raise ValueError("Enter exactly one SQL statement")
    if not allow_write and stmts[0].type not in READ_ONLY_TYPES:
        raise PermissionError(f"{stmts[0].type.name} statements need 'Allow changes' ticked "
                              "(only SELECT / DESCRIBE / SHOW / SUMMARIZE / EXPLAIN run by default)")
    cur.execute(sql)
    if cur.description is None:
        return {"columns": [], "types": [], "rows": [], "truncated": False, "message": "OK"}
    cols = [d[0] for d in cur.description]
    types = [str(d[1]) for d in cur.description]
    rows = cur.fetchmany(limit + 1)
    truncated = len(rows) > limit
    return {"columns": cols, "types": types, "rows": [[jsonable(v) for v in r] for r in rows[:limit]],
            "truncated": truncated}


def csv_text(cur, sql: str, max_rows: int = 1_000_000) -> str:
    import csv
    import io
    cur.execute(sql)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([d[0] for d in cur.description])
    n = 0
    while True:
        chunk = cur.fetchmany(10_000)
        if not chunk or n >= max_rows:
            break
        for r in chunk:
            w.writerow(["" if v is None else str(jsonable(v)) for v in r])
        n += len(chunk)
    return out.getvalue()


def schema(cur) -> list[dict]:
    rows = cur.execute("""
        SELECT t.table_name, t.table_type, c.column_name, c.data_type
        FROM information_schema.tables t JOIN information_schema.columns c USING (table_schema, table_name)
        WHERE t.table_schema = 'main' ORDER BY t.table_type, t.table_name, c.ordinal_position""").fetchall()
    out: dict[str, dict] = {}
    for name, ttype, col, typ in rows:
        o = out.setdefault(name, {"name": name, "kind": "view" if ttype == "VIEW" else "table", "columns": []})
        o["columns"].append({"name": col, "type": typ})
    for name, o in out.items():
        if o["kind"] == "table":
            o["rows"] = cur.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
    return list(out.values())


def profile_query(cur, sql: str, top_n: int = 5, max_columns: int = 200) -> dict:
    """Per-column profile (nulls, distinct, min/max, numeric spread, string lengths, top values) of a SELECT."""
    stmts = cur.extract_statements(sql)
    if len(stmts) != 1 or stmts[0].type != duckdb.StatementType.SELECT:
        raise ValueError("Profiling needs a single SELECT statement or a table name")
    view = "_prof_" + uuid.uuid4().hex[:8]
    cur.execute(f"CREATE TEMP VIEW {view} AS {sql.strip().rstrip(';')}")
    try:
        cols = cur.execute(f"SELECT column_name, column_type FROM (DESCRIBE {view})").fetchall()
        n = cur.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        result = {"rows": n, "columns": [], "truncated_columns": len(cols) > max_columns}
        for name, typ in cols[:max_columns]:
            q = '"' + name.replace('"', '""') + '"'
            base = typ.split("(")[0].upper()
            info = {"name": name, "type": typ}
            if base == "BLOB" or typ.endswith("]") or typ.startswith(("STRUCT", "MAP")):
                info.update(skipped=True)
                result["columns"].append(info)
                continue
            nn, distinct = cur.execute(f"SELECT count({q}), count(DISTINCT {q}) FROM {view}").fetchone()
            info.update(non_null=nn, nulls=n - nn, null_pct=round(100 * (n - nn) / n, 2) if n else 0,
                        distinct=distinct, distinct_pct=round(100 * distinct / nn, 2) if nn else 0)
            lo, hi = cur.execute(f"SELECT min({q})::VARCHAR, max({q})::VARCHAR FROM {view}").fetchone()
            info.update(min=lo, max=hi)
            if base in NUMERIC and nn:
                mean, sd, p25, med, p75 = cur.execute(
                    f"SELECT avg({q})::DOUBLE, stddev_samp({q})::DOUBLE, quantile_cont({q}, 0.25), "
                    f"quantile_cont({q}, 0.5), quantile_cont({q}, 0.75) FROM {view}").fetchone()
                info.update(mean=mean, std=sd, p25=p25, median=med, p75=p75,
                            zeros=cur.execute(f"SELECT count(*) FROM {view} WHERE {q} = 0").fetchone()[0],
                            negatives=cur.execute(f"SELECT count(*) FROM {view} WHERE {q} < 0").fetchone()[0])
            elif base == "VARCHAR" and nn:
                mn, mx, avg, blank = cur.execute(
                    f"SELECT min(length({q})), max(length({q})), avg(length({q})), "
                    f"count(*) FILTER (WHERE trim({q}) = '') FROM {view}").fetchone()
                info.update(min_len=mn, max_len=mx, avg_len=avg, blank=blank)
            if nn:
                info["top"] = [{"value": v, "count": c, "pct": round(100 * c / n, 2)} for v, c in cur.execute(
                    f"SELECT {q}::VARCHAR, count(*) c FROM {view} WHERE {q} IS NOT NULL "
                    f"GROUP BY 1 ORDER BY c DESC, 1 LIMIT {int(top_n)}").fetchall()]
            result["columns"].append({k: jsonable(v) for k, v in info.items()})
        return result
    finally:
        cur.execute(f"DROP VIEW IF EXISTS {view}")
