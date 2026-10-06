"""Advanced data profiling: a table summary, a deep dive per column, and profiles of every pipeline stage.

Everything runs inside the local DuckDB. ``table_summary`` answers "what does this table look like overall" (rows,
duplicates, empty / constant / key-like columns, rows per coverholder), ``column_profile`` goes deep on one column
(distribution, outliers, patterns, data-quality flags, per-coverholder breakdown) and ``profile_pipeline`` profiles the
output table of each pipeline stage and stores a snapshot so stages and runs can be compared.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from .bulk import qi
from .query import NUMERIC, check_sql, jsonable, profile_query

PROFILE_DDL = """
CREATE TABLE IF NOT EXISTS profile_snapshots (
    snapshot_id VARCHAR, taken_utc TIMESTAMP, pipeline_id VARCHAR, run_id VARCHAR, stage VARCHAR, table_name VARCHAR,
    coverholder VARCHAR, rows BIGINT, columns BIGINT, summary VARCHAR, column_stats VARCHAR
);
"""
NULL_LIKE = ("", "n/a", "na", "null", "none", "nil", "-", "--", "?", "unknown", "tbc", "tba", "#n/a", "nan", "not applicable")
DATE_TYPES = ("DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS")


def _kind(typ: str) -> str:
    base = typ.split("(")[0].upper()
    if base in NUMERIC:
        return "number"
    if base in DATE_TYPES or base.startswith("TIMESTAMP"):
        return "date"
    if base == "VARCHAR":
        return "text"
    if base == "BOOLEAN":
        return "boolean"
    return "other"


def source_sql(table: str = "", sql: str = "", coverholder: str = "") -> str:
    """The SELECT to profile: a given query, or a table (optionally one coverholder's rows)."""
    if sql.strip():
        return sql.strip().rstrip(";")
    if not table:
        raise ValueError("Give a table name or a SELECT statement")
    out = f"SELECT * FROM {qi(table)}"
    if coverholder:
        out += " WHERE _coverholder = '" + coverholder.replace("'", "''") + "'"
    return out


class _View:
    """Temp view over a guarded SELECT, dropped on exit."""

    def __init__(self, cur, sql: str):
        check_sql(cur, sql, allow_write=False)
        self.cur, self.name = cur, "_adv_" + uuid.uuid4().hex[:8]
        cur.execute(f"CREATE TEMP VIEW {self.name} AS {sql}")

    def __enter__(self):
        return self.name

    def __exit__(self, *exc):
        self.cur.execute(f"DROP VIEW IF EXISTS {self.name}")


def _rows(cur, sql: str, params=None) -> list[list]:
    return [[jsonable(v) for v in r] for r in cur.execute(sql, params or []).fetchall()]


# ----------------------------------------------------------------------------- table summary
def table_summary(cur, sql: str, top_n: int = 5) -> dict:
    """Overall picture of a SELECT: size, duplicates, per-column quality flags, rows per coverholder."""
    prof = profile_query(cur, sql, top_n=top_n)
    n = prof["rows"]
    cols = prof["columns"]
    with _View(cur, sql) as v:
        distinct_rows = cur.execute(f"SELECT count(*) FROM (SELECT DISTINCT * FROM {v})").fetchone()[0] if n else 0
        names = {c["name"] for c in cols}
        by_ch = _rows(cur, f"SELECT coalesce(_coverholder, '') ch, count(*) FROM {v} GROUP BY 1 ORDER BY 2 DESC") \
            if "_coverholder" in names else []
    flags: dict[str, list[str]] = {"all_null": [], "constant": [], "mostly_null": [], "key_candidates": [], "high_cardinality_text": []}
    for c in cols:
        if c.get("skipped"):
            continue
        if n and c["non_null"] == 0:
            flags["all_null"].append(c["name"])
        elif n and c["distinct"] == 1:
            flags["constant"].append(c["name"])
        elif c["null_pct"] >= 50:
            flags["mostly_null"].append(c["name"])
        if n and c["non_null"] == n and c["distinct"] == n:
            flags["key_candidates"].append(c["name"])
        if _kind(c["type"]) == "text" and c["non_null"] and c["distinct_pct"] >= 90 and c["non_null"] > 100:
            flags["high_cardinality_text"].append(c["name"])
    complete = round(100 * sum(c["non_null"] for c in cols if not c.get("skipped")) / (n * max(1, sum(1 for c in cols if not c.get("skipped")))), 2) if n else 0
    return {"rows": n, "columns": len(cols), "duplicate_rows": n - distinct_rows, "completeness_pct": complete,
            "flags": flags, "by_coverholder": [{"coverholder": a, "rows": b} for a, b in by_ch],
            "column_stats": cols, "top_sampled": prof["top_sampled"], "truncated_columns": prof["truncated_columns"]}


# ----------------------------------------------------------------------------- one column, in depth
def column_profile(cur, sql: str, column: str, top_n: int = 20, bins: int = 20) -> dict:
    """Deep profile of one column of a SELECT: distribution, outliers, patterns, quality flags, per-coverholder view."""
    with _View(cur, sql) as v:
        cols = {n: t for n, t in cur.execute(f"SELECT column_name, column_type FROM (DESCRIBE {v})").fetchall()}
        if column not in cols:
            raise LookupError(f"No column {column!r} in this result")
        typ = cols[column]
        kind = _kind(typ)
        c = qi(column)
        n = cur.execute(f"SELECT count(*) FROM {v}").fetchone()[0]
        nn, distinct = cur.execute(f"SELECT count({c}), count(DISTINCT {c}) FROM {v}").fetchone()
        out: dict = {"column": column, "type": typ, "kind": kind, "rows": n, "non_null": nn, "nulls": n - nn,
                     "null_pct": round(100 * (n - nn) / n, 2) if n else 0, "distinct": distinct,
                     "unique_pct": round(100 * distinct / nn, 2) if nn else 0, "is_key_candidate": bool(n and nn == n and distinct == n),
                     "issues": []}
        if kind == "other":
            out["issues"].append("This column type is not profiled in depth")
            return out
        out["top_values"] = [{"value": a, "count": b, "pct": round(100 * b / n, 2) if n else 0} for a, b in cur.execute(
            f"SELECT {c}::VARCHAR, count(*) k FROM {v} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY k DESC, 1 LIMIT {int(top_n)}").fetchall()]
        out["rare_values"] = cur.execute(f"SELECT count(*) FROM (SELECT {c} FROM {v} WHERE {c} IS NOT NULL GROUP BY 1 HAVING count(*) = 1)").fetchone()[0]
        if nn and not out["top_values"][0]["count"] == 1 and out["top_values"][0]["count"] / nn > 0.9:
            out["issues"].append(f"Dominated by one value ({out['top_values'][0]['value']!r}, {round(100 * out['top_values'][0]['count'] / nn)}%)")
        if kind == "number" and nn:
            _numeric(cur, v, c, out, bins)
        elif kind == "date" and nn:
            _dates(cur, v, c, out)
        elif kind == "text" and nn:
            _text(cur, v, c, out, top_n)
        if out["null_pct"] >= 50:
            out["issues"].append(f"{out['null_pct']}% empty")
        if "_coverholder" in cols and column != "_coverholder":
            out["by_coverholder"] = _by_coverholder(cur, v, c, kind)
            nulls = [r for r in out["by_coverholder"] if r["null_pct"] >= 50 and r["rows"]]
            if nulls and len(nulls) < len(out["by_coverholder"]):
                out["issues"].append("Mostly empty for: " + ", ".join(r["coverholder"] or "(none)" for r in nulls))
        return out


def _numeric(cur, v, c, out, bins):
    r = cur.execute(
        f"SELECT min({c})::DOUBLE, max({c})::DOUBLE, avg({c})::DOUBLE, stddev_samp({c})::DOUBLE, skewness({c})::DOUBLE, kurtosis({c})::DOUBLE, "
        f"quantile_cont({c}::DOUBLE, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]), count(*) FILTER (WHERE {c} = 0), count(*) FILTER (WHERE {c} < 0), "
        f"count(*) FILTER (WHERE {c}::DOUBLE <> round({c}::DOUBLE)), sum({c})::DOUBLE FROM {v}").fetchone()
    mn, mx, mean, std, skew, kurt, q, zeros, neg, frac, total = r
    out["stats"] = {k: jsonable(x) for k, x in zip(("min", "max", "mean", "std", "skewness", "kurtosis"), (mn, mx, mean, std, skew, kurt))}
    out["stats"].update({"sum": total, "p1": q[0], "p5": q[1], "p25": q[2], "median": q[3], "p75": q[4], "p95": q[5], "p99": q[6],
                         "zeros": zeros, "negatives": neg, "non_integers": frac})
    iqr = q[4] - q[2]
    lo, hi = q[2] - 1.5 * iqr, q[4] + 1.5 * iqr
    if iqr > 0:
        below, above = cur.execute(f"SELECT count(*) FILTER (WHERE {c} < ?), count(*) FILTER (WHERE {c} > ?) FROM {v}", [lo, hi]).fetchone()
        out["outliers"] = {"method": "1.5 x IQR", "low_fence": lo, "high_fence": hi, "below": below, "above": above,
                           "extremes": _rows(cur, f"SELECT {c}::DOUBLE, count(*) FROM {v} WHERE {c} < ? OR {c} > ? GROUP BY 1 ORDER BY abs({c}::DOUBLE - ?) DESC LIMIT 10",
                                             [lo, hi, q[3]])}
        if below + above:
            out["issues"].append(f"{below + above} outlier(s) outside {lo:g} .. {hi:g}")
    if neg:
        out["issues"].append(f"{neg} negative value(s)")
    if mx != mn:
        w = (mx - mn) / bins
        out["histogram"] = [{"from": mn + i * w, "to": mn + (i + 1) * w, "count": k} for i, k in
                            _fill(cur.execute(f"SELECT least(floor(({c}::DOUBLE - ?) / ?), ?)::INT b, count(*) FROM {v} WHERE {c} IS NOT NULL GROUP BY 1",
                                              [mn, w, bins - 1]).fetchall(), bins)]


def _fill(rows, bins):
    got = dict(rows)
    return [(i, got.get(i, 0)) for i in range(bins)]


def _dates(cur, v, c, out):
    mn, mx, future, weekend, midnight = cur.execute(
        f"SELECT min({c})::VARCHAR, max({c})::VARCHAR, count(*) FILTER (WHERE {c}::DATE > current_date), "
        f"count(*) FILTER (WHERE dayofweek({c}::DATE) IN (0, 6)), count(*) FILTER (WHERE {c}::TIMESTAMP::TIME = TIME '00:00:00') FROM {v}").fetchone()
    span = cur.execute(f"SELECT date_diff('day', min({c})::DATE, max({c})::DATE) FROM {v}").fetchone()[0]
    out["stats"] = {"min": mn, "max": mx, "span_days": span, "future": future, "weekend": weekend, "midnight_only": midnight}
    unit = "month" if span <= 365 * 5 else "year"
    out["by_period"] = {"unit": unit, "rows": _rows(cur, f"SELECT strftime(date_trunc('{unit}', {c}::DATE), '{'%Y-%m' if unit == 'month' else '%Y'}') p, count(*) "
                                                   f"FROM {v} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY 1")}
    if future:
        out["issues"].append(f"{future} date(s) in the future")
    if span > 365 * 60:
        out["issues"].append(f"Dates span {span // 365} years; check for placeholder dates")


def _text(cur, v, c, out, top_n):
    lows = ", ".join("'" + x.replace("'", "''") + "'" for x in NULL_LIKE)
    r = cur.execute(
        f"SELECT min(length({c})), max(length({c})), avg(length({c}))::DOUBLE, "
        f"count(*) FILTER (WHERE {c} <> trim({c})), count(*) FILTER (WHERE lower(trim({c})) IN ({lows})), "
        f"count(DISTINCT lower(trim({c}))), count(*) FILTER (WHERE try_cast(trim({c}) AS DOUBLE) IS NOT NULL), "
        f"count(*) FILTER (WHERE try_cast(trim({c}) AS DATE) IS NOT NULL AND try_cast(trim({c}) AS DOUBLE) IS NULL), "
        f"count(*) FILTER (WHERE {c} <> upper({c}) AND {c} <> lower({c})), "
        f"count(*) FILTER (WHERE {c} = upper({c}) AND {c} <> lower({c})), count(*) FILTER (WHERE {c} = lower({c}) AND {c} <> upper({c})), "
        f"count(*) FILTER (WHERE regexp_matches({c}, '[^\\x20-\\x7E]')), count(*) FILTER (WHERE trim({c}) = '') FROM {v}").fetchone()
    mnl, mxl, avl, padded, nulllike, norm_distinct, numeric, datelike, mixed, upper, lower, nonascii, blank = r
    nn = out["non_null"]
    out["text"] = {"min_len": mnl, "max_len": mxl, "avg_len": jsonable(avl), "leading_trailing_spaces": padded, "blank": blank,
                   "null_like": nulllike, "numeric_like": numeric, "date_like": datelike, "non_ascii": nonascii,
                   "case": {"upper": upper, "lower": lower, "mixed": mixed}, "distinct_after_trim_and_case": norm_distinct}
    out["lengths"] = _rows(cur, f"SELECT length({c}) l, count(*) FROM {v} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10")
    mask = f"regexp_replace(regexp_replace(regexp_replace(left({c}, 30), '[A-Z]', 'A', 'g'), '[a-z]', 'a', 'g'), '[0-9]', '9', 'g')"
    pats = cur.execute(f"SELECT {mask} p, count(*) k, any_value({c}) FROM {v} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY k DESC, 1 LIMIT 10").fetchall()
    out["patterns"] = [{"pattern": p, "count": k, "example": e, "pct": round(100 * k / nn, 2)} for p, k, e in pats]
    out["pattern_count"] = cur.execute(f"SELECT count(DISTINCT {mask}) FROM {v} WHERE {c} IS NOT NULL").fetchone()[0]
    if padded:
        out["issues"].append(f"{padded} value(s) with leading/trailing spaces")
    if nulllike:
        out["issues"].append(f"{nulllike} placeholder value(s) such as N/A, null or - (treated as non-empty)")
    if norm_distinct < out["distinct"]:
        out["issues"].append(f"{out['distinct'] - norm_distinct} value(s) differ only by case or spaces")
    if numeric and numeric >= 0.9 * nn:
        out["issues"].append("Looks numeric: consider a number type")
    elif datelike and datelike >= 0.9 * nn:
        out["issues"].append("Looks like dates: consider a date type")
    elif 0 < numeric < nn:
        out["issues"].append(f"Mixed: {numeric} numeric-looking value(s) among text")
    if nonascii:
        out["issues"].append(f"{nonascii} value(s) with non-ASCII characters")
    if out["pattern_count"] > 1 and out["patterns"][0]["pct"] >= 80:
        out["issues"].append(f"Mostly one format ({out['patterns'][0]['pattern']!r}); {out['pattern_count'] - 1} other format(s)")
    if out["distinct"] <= 50:
        out["is_categorical"] = True


def _by_coverholder(cur, v, c, kind):
    ext = f", min({c})::VARCHAR, max({c})::VARCHAR" if kind != "text" else f", min(length({c})), max(length({c}))"
    rows = cur.execute(f"SELECT coalesce(_coverholder, ''), count(*), count({c}), count(DISTINCT {c}){ext} FROM {v} GROUP BY 1 ORDER BY 2 DESC LIMIT 200").fetchall()
    return [{"coverholder": a, "rows": b, "non_null": d, "null_pct": round(100 * (b - d) / b, 2) if b else 0, "distinct": e,
             "min": jsonable(f), "max": jsonable(g), "range_is_length": kind == "text"} for a, b, d, e, f, g in rows]


# ----------------------------------------------------------------------------- stages of a pipeline
def profile_pipeline(cur, pipeline_id: str, coverholder: str = "", run_id: str = "", store: bool = True) -> dict:
    """Table summary of every stage's output table (optionally one coverholder), saved as snapshots for comparison."""
    from . import pipeline as pl
    cur.execute(PROFILE_DDL)
    pipe = pl.get_pipeline(cur, pipeline_id)
    out = {"pipeline": pipe["name"], "coverholder": coverholder, "stages": []}
    now = datetime.now(timezone.utc)
    prev_cols: Optional[list[str]] = None
    for stage in pipe["stages"]:
        t = stage["output_table"]
        entry = {"position": stage["position"], "stage": stage["name"], "kind": stage["kind"], "table": t}
        if not t or not cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [t]).fetchone():
            entry["error"] = "Not created yet: run the pipeline first"
            out["stages"].append(entry)
            continue
        try:
            s = table_summary(cur, source_sql(t, coverholder=coverholder))
        except Exception as e:                  # one stage must not hide the others
            entry["error"] = str(e)
            out["stages"].append(entry)
            continue
        names = [c["name"] for c in s["column_stats"]]
        if prev_cols is not None:
            entry["added_columns"] = [x for x in names if x not in prev_cols and not x.startswith("_")]
            entry["dropped_columns"] = [x for x in prev_cols if x not in names and not x.startswith("_")]
        prev_cols = names
        entry.update(s)
        out["stages"].append(entry)
        if store:
            slim = {k: s[k] for k in ("duplicate_rows", "completeness_pct", "flags", "by_coverholder")}
            cur.execute("INSERT INTO profile_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [uuid.uuid4().hex, now, pipeline_id, run_id, stage["name"], t, coverholder, s["rows"], s["columns"],
                         json.dumps(slim), json.dumps(s["column_stats"], default=str)])
    return out


def snapshot_history(cur, pipeline_id: str, limit: int = 50) -> list[dict]:
    """Stored stage profiles, newest first, for spotting drift between runs."""
    cur.execute(PROFILE_DDL)
    return [dict(zip(("taken_utc", "stage", "table", "coverholder", "rows", "columns", "completeness_pct", "duplicate_rows"), r)) for r in cur.execute(
        "SELECT taken_utc::VARCHAR, stage, table_name, coverholder, rows, columns, "
        "json_extract(summary, '$.completeness_pct')::DOUBLE, json_extract(summary, '$.duplicate_rows')::BIGINT "
        "FROM profile_snapshots WHERE pipeline_id = ? ORDER BY taken_utc DESC, stage LIMIT ?", [pipeline_id, limit]).fetchall()]
