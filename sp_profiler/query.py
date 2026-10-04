"""SQL console and data profiling helpers over a DuckDB cursor."""
from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import re
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


# Functions that read files / environment / run dynamic SQL. Blocked in read-only mode (best effort: this is
# defence in depth for a single-user local tool, not a sandbox; "allow changes" lifts it).
BLOCKED_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_json", "read_json_auto", "read_json_objects", "read_json_objects_auto",
    "read_ndjson", "read_ndjson_auto", "read_ndjson_objects", "read_parquet", "parquet_scan", "parquet_metadata",
    "parquet_schema", "parquet_file_metadata", "parquet_kv_metadata", "read_blob", "read_text", "glob", "sniff_csv",
    "csv_scan", "json_scan", "iceberg_scan", "delta_scan", "read_xlsx", "st_read", "getenv", "which_secret",
    "duckdb_secrets", "query", "query_table"}
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRINGS = re.compile(r"'(?:[^']|'')*'")
_FILE_SOURCE = re.compile(r"""\b(?:from|join|describe|summarize)\s*(?:'|"[^"]*[./\\][^"]*")""", re.I)


def check_sql(cur, sql: str, allow_write: bool = False) -> None:
    """Raise unless ``sql`` is a single statement that is allowed to run."""
    stmts = cur.extract_statements(sql)
    if len(stmts) != 1:
        raise ValueError("Enter exactly one SQL statement")
    if allow_write:
        return
    if stmts[0].type not in READ_ONLY_TYPES:
        raise PermissionError(f"{stmts[0].type.name} statements need 'Allow changes' ticked "
                              "(only SELECT / DESCRIBE / SHOW / SUMMARIZE / EXPLAIN run by default)")
    text = _COMMENTS.sub(" ", sql)
    if _FILE_SOURCE.search(text):
        raise PermissionError("Reading files from SQL needs 'Allow changes' ticked")
    bare = _STRINGS.sub("''", text).replace('"', "")
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", bare):
        if m.group(1).lower() in BLOCKED_FUNCTIONS:
            raise PermissionError(f"{m.group(1)}() reads files or the environment and needs 'Allow changes' ticked")


def run_sql(cur, sql: str, limit: int = 1000, allow_write: bool = False) -> dict:
    """Run exactly one statement. Without ``allow_write`` only SELECT-like statements are accepted."""
    check_sql(cur, sql, allow_write)
    cur.execute(sql)
    if cur.description is None:
        return {"columns": [], "types": [], "rows": [], "truncated": False, "message": "OK"}
    cols = [d[0] for d in cur.description]
    types = [str(d[1]) for d in cur.description]
    rows = cur.fetchmany(limit + 1)
    truncated = len(rows) > limit
    return {"columns": cols, "types": types, "rows": [[jsonable(v) for v in r] for r in rows[:limit]],
            "truncated": truncated}


def csv_chunks(cur, sql: str, allow_write: bool = False, max_rows: int = 5_000_000, chunk: int = 5000):
    """Run ``sql`` (same checks as the console) and return a generator of CSV text chunks, streamed not buffered.

    The query is executed before this returns, so errors surface before any bytes are sent.
    """
    check_sql(cur, sql, allow_write)
    cur.execute(sql)
    if cur.description is None:
        raise ValueError("That statement returns no rows")
    names = [d[0] for d in cur.description]

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(names)
        yield "\ufeff" + buf.getvalue()           # BOM so Excel reads UTF-8
        sent = 0
        while sent < max_rows:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            buf.seek(0)
            buf.truncate()
            for r in rows:
                w.writerow(["" if v is None else str(jsonable(v)) for v in r])
            sent += len(rows)
            yield buf.getvalue()
    return gen()


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


def profile_query(cur, sql: str, top_n: int = 5, max_columns: int = 200, exact_top_rows: int = 500_000,
                  sample_rows: int = 100_000, batch_cols: int = 25) -> dict:
    """Per-column profile (nulls, distinct, min/max, numeric spread, string lengths, top values) of a SELECT.

    Column statistics come from a few batched scans (``batch_cols`` columns per scan) instead of several scans per
    column. Top values are exact up to ``exact_top_rows`` rows; beyond that they are computed on a ``sample_rows`` sample
    and flagged (``top_sampled``).
    """
    stmts = cur.extract_statements(sql)
    if len(stmts) != 1 or stmts[0].type != duckdb.StatementType.SELECT:
        raise ValueError("Profiling needs a single SELECT statement or a table name")
    check_sql(cur, sql, allow_write=False)
    view = "_prof_" + uuid.uuid4().hex[:8]
    cur.execute(f"CREATE TEMP VIEW {view} AS {sql.strip().rstrip(';')}")
    try:
        cols = cur.execute(f"SELECT column_name, column_type FROM (DESCRIBE {view})").fetchall()
        n = cur.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        result = {"rows": n, "columns": [], "truncated_columns": len(cols) > max_columns, "top_sampled": n > exact_top_rows}
        infos, todo = [], []
        for name, typ in cols[:max_columns]:
            base = typ.split("(")[0].upper()
            info = {"name": name, "type": typ}
            if base == "BLOB" or typ.endswith("]") or typ.startswith(("STRUCT", "MAP")):
                info["skipped"] = True
            else:
                todo.append((info, name, base))
            infos.append(info)
        qi = lambda s: '"' + s.replace('"', '""') + '"'
        for start in range(0, len(todo), batch_cols):
            part = todo[start:start + batch_cols]
            exprs, layout = [], []
            for info, name, base in part:
                c = qi(name)
                e = [f"count({c})", f"count(DISTINCT {c})", f"min({c})::VARCHAR", f"max({c})::VARCHAR"]
                kind = "num" if base in NUMERIC else "str" if base == "VARCHAR" else "other"
                if kind == "num":
                    e += [f"avg({c})::DOUBLE", f"stddev_samp({c})::DOUBLE", f"quantile_cont({c}, 0.25)", f"quantile_cont({c}, 0.5)",
                          f"quantile_cont({c}, 0.75)", f"count(*) FILTER (WHERE {c} = 0)", f"count(*) FILTER (WHERE {c} < 0)"]
                elif kind == "str":
                    e += [f"min(length({c}))", f"max(length({c}))", f"avg(length({c}))", f"count(*) FILTER (WHERE trim({c}) = '')"]
                layout.append((kind, len(e)))
                exprs += e
            row = cur.execute(f"SELECT {', '.join(exprs)} FROM {view}").fetchone()
            pos = 0
            for (info, name, base), (kind, size) in zip(part, layout):
                v = row[pos:pos + size]
                pos += size
                nn, distinct = v[0], v[1]
                info.update(non_null=nn, nulls=n - nn, null_pct=round(100 * (n - nn) / n, 2) if n else 0, distinct=distinct,
                            distinct_pct=round(100 * distinct / nn, 2) if nn else 0, min=v[2], max=v[3])
                if kind == "num" and nn:
                    info.update(mean=v[4], std=v[5], p25=v[6], median=v[7], p75=v[8], zeros=v[9], negatives=v[10])
                elif kind == "str" and nn:
                    info.update(min_len=v[4], max_len=v[5], avg_len=v[6], blank=v[7])
        source = view if n <= exact_top_rows else f"(SELECT * FROM {view} USING SAMPLE {int(sample_rows)} ROWS)"
        denom = n if n <= exact_top_rows else min(n, sample_rows)
        for info, name, base in todo:
            if info.get("non_null"):
                c = qi(name)
                info["top"] = [{"value": v, "count": cnt, "pct": round(100 * cnt / denom, 2)} for v, cnt in cur.execute(
                    f"SELECT {c}::VARCHAR, count(*) c FROM {source} WHERE {c} IS NOT NULL "
                    f"GROUP BY 1 ORDER BY c DESC, 1 LIMIT {int(top_n)}").fetchall()]
        result["columns"] = [{k: jsonable(v) for k, v in info.items()} for info in infos]
        return result
    finally:
        cur.execute(f"DROP VIEW IF EXISTS {view}")
