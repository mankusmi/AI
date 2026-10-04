"""Apply column mappings to stored Excel files and append the rows to a typed DuckDB table."""
from __future__ import annotations

import io
import re
import threading
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

LOAD_DDL = """
CREATE TABLE IF NOT EXISTS load_log (
    load_id VARCHAR, entity VARCHAR, dataflow_id VARCHAR, started_utc TIMESTAMPTZ, finished_utc TIMESTAMPTZ,
    source_sha256 VARCHAR, source_path VARCHAR, sheet VARCHAR, layout_hash VARCHAR,
    rows_loaded BIGINT, rows_skipped_empty BIGINT, coerce_error_cells BIGINT, status VARCHAR, error VARCHAR,
    source_id VARCHAR, coverholder VARCHAR);
-- entity name -> DuckDB table name, unique even when two entity names slug to the same text
CREATE TABLE IF NOT EXISTS entity_tables (entity VARCHAR PRIMARY KEY, table_name VARCHAR UNIQUE);
"""

DUCK_TYPES = {
    "string": "VARCHAR", "guid": "VARCHAR", "int64": "BIGINT", "int32": "INTEGER", "int16": "SMALLINT",
    "byte": "TINYINT", "double": "DOUBLE", "float": "DOUBLE", "single": "DOUBLE", "decimal": "DECIMAL(38,10)",
    "boolean": "BOOLEAN", "datetime": "TIMESTAMP", "datetimeoffset": "TIMESTAMPTZ", "date": "DATE",
    "time": "TIME",
}
PROVENANCE = [("_load_id", "VARCHAR"), ("_source_path", "VARCHAR"), ("_source_sha256", "VARCHAR"),
              ("_sheet", "VARCHAR"), ("_excel_row", "INTEGER"), ("_layout_hash", "VARCHAR"),
              ("_loaded_utc", "TIMESTAMPTZ"), ("_coerce_errors", "VARCHAR"), ("_source_id", "VARCHAR"),
              ("_coverholder", "VARCHAR")]
DMY_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y")
MDY_FORMATS = ("%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y", "%m/%d/%y", "%m-%d-%y")
OTHER_FORMATS = ("%Y/%m/%d", "%d %b %Y", "%d-%b-%Y", "%d %B %Y")
AMBIGUOUS_RE = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}")


def date_order_for_culture(culture: str) -> str:
    """'MDY' for US cultures (Power Query reads 06/02/2024 as 2 June there), otherwise 'DMY'."""
    return "MDY" if (culture or "").lower().replace("_", "-") in ("en-us", "es-us", "en-ph", "fil-ph") else "DMY"


def is_ambiguous_date(value) -> bool:
    """True for text like 06/02/2024 that reads as a valid date in both day-first and month-first order."""
    m = AMBIGUOUS_RE.match(value) if isinstance(value, str) else None
    return bool(m) and int(m.group(1)) <= 12 and int(m.group(2)) <= 12 and m.group(1) != m.group(2) \
        and int(m.group(1)) >= 1 and int(m.group(2)) >= 1


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def target_table(entity: str) -> str:
    return "df_" + re.sub(r"[^0-9a-zA-Z]+", "_", entity).strip("_").lower()


def duck_type(data_type: str) -> str:
    return DUCK_TYPES.get(str(data_type).lower(), "VARCHAR")


def table_for(con, entity: str) -> str:
    """The DuckDB table an entity loads into. Names are registered, so "A B" and "A-B" never share a table."""
    r = con.execute("SELECT table_name FROM entity_tables WHERE entity = ?", [entity]).fetchone()
    if r:
        return r[0]
    base = target_table(entity)
    used = {x[0] for x in con.execute("SELECT table_name FROM entity_tables").fetchall()}
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    try:
        con.execute("INSERT INTO entity_tables VALUES (?, ?)", [entity, name])
    except Exception:                       # registered concurrently by another thread
        r = con.execute("SELECT table_name FROM entity_tables WHERE entity = ?", [entity]).fetchone()
        if r:
            return r[0]
        raise
    return name


def attribute_problems(attributes: list[dict]) -> list[str]:
    """Reasons a set of dataflow attributes cannot become DuckDB columns."""
    problems, seen = [], {}
    reserved = {p[0].lower() for p in PROVENANCE}
    for a in attributes:
        low = a["name"].lower()
        if low in seen:
            problems.append(f"attributes {seen[low]!r} and {a['name']!r} differ only by case (DuckDB columns are case-insensitive)")
        seen.setdefault(low, a["name"])
        if low in reserved:
            problems.append(f"attribute {a['name']!r} clashes with a provenance column; rename it in the dataflow")
    return problems


def ensure_target_table(con, entity: str, attributes: list[dict], table: Optional[str] = None) -> str:
    """Create the entity's table, or evolve it: add missing columns/provenance and migrate changed attribute types."""
    problems = attribute_problems(attributes)
    if problems:
        raise ValueError("; ".join(problems))
    table = table or table_for(con, entity)
    cols = [(a["name"], duck_type(a["data_type"])) for a in attributes]
    exists = con.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]).fetchone()
    if not exists:
        ddl = ", ".join(f"{quote_ident(n)} {t}" for n, t in cols + PROVENANCE)
        con.execute(f"CREATE TABLE {quote_ident(table)} ({ddl})")
        return table
    have = {r[0].lower(): r[1].upper() for r in con.execute(f"SELECT column_name, column_type FROM (DESCRIBE {quote_ident(table)})").fetchall()}
    for n, t in cols:
        if n.lower() not in have:
            con.execute(f"ALTER TABLE {quote_ident(table)} ADD COLUMN {quote_ident(n)} {t}")
        elif have[n.lower()] != t.upper() and not (t.upper() == "TIMESTAMPTZ" and have[n.lower()] == "TIMESTAMP WITH TIME ZONE"):
            try:         # the dataflow changed this attribute's type: convert the existing values (strict cast)
                con.execute(f"ALTER TABLE {quote_ident(table)} ALTER COLUMN {quote_ident(n)} SET DATA TYPE {t} "
                            f"USING CAST({quote_ident(n)} AS {t})")
            except Exception as e:
                raise ValueError(f"Column {n!r} changed type from {have[n.lower()]} to {t} and existing values cannot be "
                                 f"converted ({e}). Fix or delete those rows, or load into a new entity name.") from e
    added_source = False
    for n, t in PROVENANCE:
        if n.lower() not in have:
            con.execute(f"ALTER TABLE {quote_ident(table)} ADD COLUMN {quote_ident(n)} {t}")
            added_source = added_source or n == "_source_id"
    if added_source:
        con.execute(f"UPDATE {quote_ident(table)} SET _source_id = _source_path WHERE _source_id IS NULL")
    return table


# ------------------------------------------------------------------ coercion
def _excel_serial(v: float) -> Optional[datetime]:
    if 20000 < v < 80000:
        from openpyxl.utils.datetime import from_excel
        return from_excel(v)
    return None


def _to_datetime(v, order: str = "DMY") -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return _excel_serial(v)
    s = str(v).strip()
    if re.fullmatch(r"\d{5}(\.\d+)?", s):          # Excel serial date stored as text
        return _excel_serial(float(s))
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    first, second = (MDY_FORMATS, DMY_FORMATS) if order == "MDY" else (DMY_FORMATS, MDY_FORMATS)
    for fmt in first + second + OTHER_FORMATS:       # preferred order first; the other only if that cannot parse
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _to_decimal(v) -> Decimal:
    if isinstance(v, bool):
        raise InvalidOperation
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = re.sub(r"[\s,£$€]", "", str(v))
    neg = s.startswith("(") and s.endswith(")")
    d = Decimal(s.strip("()"))
    return -d if neg else d


def coerce(value, data_type: str, date_order: str = "DMY"):
    """Return ``(converted, ok)``. Empty values give ``(None, True)``; unparseable ones ``(None, False)``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, True
    dt = str(data_type).lower()
    try:
        if dt in ("string", "guid") or dt not in DUCK_TYPES:
            if isinstance(value, float) and value.is_integer():
                return str(int(value)), True      # 12345.0 from Excel -> "12345"
            return (value.isoformat() if isinstance(value, (datetime, date, time)) else str(value)), True
        if dt in ("int64", "int32", "int16", "byte"):
            d = _to_decimal(value)
            return (int(d), True) if d == d.to_integral_value() else (None, False)
        if dt in ("double", "float", "single"):
            return float(_to_decimal(value)), True
        if dt == "decimal":
            return _to_decimal(value), True
        if dt == "boolean":
            if isinstance(value, bool):
                return value, True
            s = str(value).strip().lower()
            if s in ("true", "yes", "y", "1", "1.0"):
                return True, True
            if s in ("false", "no", "n", "0", "0.0"):
                return False, True
            return None, False
        if dt in ("datetime", "datetimeoffset"):
            d = _to_datetime(value, date_order)
            if d is None:
                return None, False
            if dt == "datetimeoffset" and d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d, True
        if dt == "date":
            d = _to_datetime(value, date_order)
            return (d.date(), True) if d else (None, False)
        if dt == "time":
            if isinstance(value, time):
                return value, True
            if isinstance(value, datetime):
                return value.time(), True
            return time.fromisoformat(str(value).strip()), True
    except (InvalidOperation, ValueError, OverflowError):
        return None, False
    return None, False


# ------------------------------------------------------------------ mappings
def save_mapping(con, entity: str, layout_hash: str, pairs: list[dict], attributes: list[str], kind: str = "attribute") -> int:
    """Replace the mapping of one layout. ``pairs`` = [{norm_header, attribute}]; blank attribute = unmapped."""
    valid = set(attributes)
    seen: set[str] = set()
    rows = []
    for p in pairs:
        attr, hdr = (p.get("attribute") or "").strip(), p["norm_header"]
        if not attr:
            continue
        if attr not in valid:
            raise ValueError(f"Unknown target {attr!r} for entity {entity!r}")
        if attr in seen:
            raise ValueError(f"Attribute {attr!r} is mapped from more than one column")
        seen.add(attr)
        rows.append([entity, layout_hash, hdr, attr, kind])
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM column_mappings WHERE entity = ? AND layout_hash = ? AND kind = ?", [entity, layout_hash, kind])
        con.executemany("INSERT INTO column_mappings (entity, layout_hash, norm_header, attribute, kind) VALUES (?, ?, ?, ?, ?)", rows)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(rows)


# ------------------------------------------------------------------ loading
def unmapped_layouts(con, entity: str, kind: str = "attribute", coverholder: Optional[str] = None) -> list[dict]:
    """Layouts with stored data that this entity has no mapping for (and that are not marked 'ignore')."""
    where, args = "", [entity, kind, entity]
    if coverholder is not None:
        where, args = " AND f.coverholder = ?", args + [coverholder]
    rows = con.execute(f"""
        SELECT s.layout_hash, count(DISTINCT f.sha256) AS files, any_value(f.rel_path) AS example, sum(s.data_rows) AS data_rows
        FROM v_sheets s JOIN v_files f USING (run_id, rel_path) JOIN file_blobs b ON b.sha256 = f.sha256
        WHERE s.header_row IS NOT NULL AND s.data_rows > 0
          AND s.layout_hash NOT IN (SELECT layout_hash FROM column_mappings WHERE entity = ? AND kind = ?)
          AND s.layout_hash NOT IN (SELECT layout_hash FROM layout_ignores WHERE entity = ?){where}
        GROUP BY s.layout_hash ORDER BY files DESC""", args).fetchall()
    return [{"layout_hash": h, "files": n, "example": ex, "data_rows": dr} for h, n, ex, dr in rows]


def set_layout_ignored(con, entity: str, layout_hash: str, ignored: bool) -> None:
    con.execute("DELETE FROM layout_ignores WHERE entity = ? AND layout_hash = ?", [entity, layout_hash])
    if ignored:
        con.execute("INSERT INTO layout_ignores VALUES (?, ?)", [entity, layout_hash])


def pending_sheets(con, entity: str, layout_hashes: Optional[list[str]] = None, force: bool = False,
                   kind: str = "attribute", coverholder: Optional[str] = None) -> list[dict]:
    """Distinct (content, sheet) units with a mapped layout and stored bytes, minus those already loaded."""
    rows = con.execute("""
        SELECT f.sha256, any_value(f.rel_path) AS rel_path, s.sheet, s.layout_hash, min(s.header_row) AS header_row,
               any_value(COALESCE(NULLIF(f.item_id, ''), f.location, f.rel_path)) AS source_id,
               coalesce(f.coverholder, '') AS coverholder
        FROM v_sheets s JOIN v_files f USING (run_id, rel_path)
        JOIN file_blobs b ON b.sha256 = f.sha256
        WHERE s.header_row IS NOT NULL AND s.layout_hash IN
              (SELECT DISTINCT layout_hash FROM column_mappings WHERE entity = ? AND kind = ?)
        GROUP BY f.sha256, s.sheet, s.layout_hash, coalesce(f.coverholder, '') ORDER BY rel_path, s.sheet""", [entity, kind]).fetchall()
    done = set() if force else set(con.execute(
        "SELECT source_sha256, sheet, coalesce(coverholder, '') FROM load_log WHERE entity = ? AND status = 'ok'", [entity]).fetchall())
    out = []
    for sha, path, sheet, lh, hr, sid, ch in rows:
        if layout_hashes and lh not in layout_hashes:
            continue
        if coverholder is not None and ch != coverholder:
            continue
        if (sha, sheet, ch) in done:
            continue
        out.append({"sha256": sha, "rel_path": path, "sheet": sheet, "layout_hash": lh, "header_row": hr, "source_id": sid,
                    "coverholder": ch})
    return out


def _sheet_rows(blob: bytes, sheet: str, header_row: int):
    """(header cells, iterator of (excel_row, row tuple)) for one stored sheet."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    ws = wb[sheet]
    it = ws.iter_rows(min_row=header_row, values_only=True)
    header = next(it)

    def gen():
        try:
            for k, row in enumerate(it, start=header_row + 1):
                yield k, row
        finally:
            wb.close()
    return header, gen()


def _norm_header_cells(header):
    from .inspect_excel import normalise_header
    return [normalise_header(c) if c is not None and str(c).strip() else f"<blank{i + 1}>" for i, c in enumerate(header)]


def _rows_attribute_mode(con, entity, w, blob, col_names, types, order, stats):
    mapping = dict(con.execute("SELECT norm_header, attribute FROM column_mappings WHERE entity = ? AND layout_hash = ? "
                               "AND kind = 'attribute'", [entity, w["layout_hash"]]).fetchall())
    mapping = {h: a for h, a in mapping.items() if a in types}      # drop attrs gone from this dataflow
    header, rows = _sheet_rows(blob, w["sheet"], w["header_row"])
    col_for = {}
    for i, h in enumerate(_norm_header_cells(header)):
        if h in mapping and mapping[h] not in col_for:
            col_for[mapping[h]] = i
    date_cols = {n for n in col_names if str(types[n]).lower() in ("datetime", "datetimeoffset", "date")}
    for k, row in rows:
        raws = []
        for name in col_names:
            i = col_for.get(name)
            raws.append(row[i] if i is not None and i < len(row) else None)
        if all(r is None or (isinstance(r, str) and not r.strip()) for r in raws):
            yield k, None, None
            continue
        vals, errs = [], []
        for name, raw in zip(col_names, raws):
            if name in date_cols and is_ambiguous_date(raw):
                stats["ambiguous_dates"] += 1
            v, ok = coerce(raw, types[name], order)
            vals.append(v)
            if not ok:
                errs.append(name)
        yield k, vals, errs


def _rows_m_mode(con, entity, w, blob, plan, col_names, types, stats):
    """Run the translated Power Query pipeline over one sheet (staged as VARCHAR columns)."""
    from .m2sql import sheet_columns, stage_table, stage_value
    inputs, order = plan["inputs"], plan["date_order"]
    mapping = dict(con.execute("SELECT norm_header, attribute FROM column_mappings WHERE entity = ? AND layout_hash = ? "
                               "AND kind = 'input'", [entity, w["layout_hash"]]).fetchall())
    header, rows = _sheet_rows(blob, w["sheet"], w["header_row"])
    cols = sheet_columns(header, inputs, mapping, _norm_header_cells(header), plan.get("dynamic_columns", False))
    names = [n for n, _ in cols]
    staged, empty = [], 0
    for k, row in rows:
        vals = [stage_value(row[i]) if i is not None and i < len(row) else None for _, i in cols]
        if all(v is None or not v.strip() for v in vals):
            empty += 1
            continue
        stats["ambiguous_dates"] += sum(1 for v in vals if is_ambiguous_date(v))
        staged.append((k, *vals))
    stage_table(con, names, staged)
    res = con.execute(plan["sql"])
    out_cols = [d[0] for d in res.description]
    result_rows = res.fetchall()          # fully fetched: the connection is reused for the INSERTs
    idx = {c.lower(): i for i, c in enumerate(out_cols)}
    row_i = idx.get("__row")
    for _ in range(empty):
        yield None, None, None
    for r in result_rows:
        vals, errs = [], []
        for name in col_names:
            i = idx.get(name.lower())
            v, ok = coerce(r[i] if i is not None else None, types[name], order)
            vals.append(v)
            if not ok:
                errs.append(name)
        yield (r[row_i] if row_i is not None else None), vals, errs


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _entity_lock(entity: str) -> threading.Lock:
    """One lock per entity so two concurrent loads cannot both decide the same sheet is pending."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(entity, threading.Lock())


def load_entity(con, dataflow_id: str, entity: str, layout_hashes: Optional[list[str]] = None,
                force: bool = False, progress: Optional[Callable[[int, int, str], None]] = None,
                batch: int = 50_000, coverholder: Optional[str] = None, table: Optional[str] = None) -> dict:
    """Append rows for ``entity`` from every pending stored sheet. Returns a summary dict.

    Each sheet is loaded in its own transaction (all rows plus the log entry, or nothing). Reloading a
    sheet (``force``) replaces the rows previously loaded from it instead of duplicating them. Uses the
    translated Power Query pipeline when the entity has one enabled, otherwise the plain mapping.
    """
    with _entity_lock(entity):
        return _load_entity(con, dataflow_id, entity, layout_hashes, force, progress, batch, coverholder, table)


def _load_entity(con, dataflow_id, entity, layout_hashes, force, progress, batch, coverholder=None, table=None) -> dict:
    from . import transforms
    from .bulk import bulk_insert
    from .m2sql import register_udfs
    from .store import read_blob
    attrs = [{"name": r[0], "data_type": r[1]} for r in con.execute(
        "SELECT name, data_type FROM dataflow_attributes WHERE dataflow_id = ? AND entity = ? ORDER BY position",
        [dataflow_id, entity]).fetchall()]
    if not attrs:
        raise LookupError(f"Entity {entity!r} not found in dataflow {dataflow_id}")
    plan = transforms.resolve(con, dataflow_id, entity)
    if plan["use_m"] and plan["mode"] != "m":
        raise ValueError(f"Power Query transformation for {entity!r} is not ready ({plan['status']})")
    kind = "input" if plan["mode"] == "m" else "attribute"
    register_udfs(con)
    table = ensure_target_table(con, entity, attrs, table)
    types = {a["name"]: a["data_type"] for a in attrs}
    col_names = [a["name"] for a in attrs]
    all_cols = col_names + [p[0] for p in PROVENANCE]
    order = plan["date_order"]
    load_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    work = pending_sheets(con, entity, layout_hashes, force, kind, coverholder)
    total = {"load_id": load_id, "table": table, "sheets_loaded": 0, "sheets_failed": 0, "mode": plan["mode"],
             "rows": 0, "coerce_error_cells": 0, "sheets_planned": len(work), "date_order": order,
             "ambiguous_dates": 0, "replaced_rows": 0}
    log_sql = ("INSERT INTO load_log (load_id, entity, dataflow_id, started_utc, finished_utc, source_sha256, source_path, sheet, "
               "layout_hash, rows_loaded, rows_skipped_empty, coerce_error_cells, status, error, source_id, coverholder) "
               "VALUES (?, ?, ?, ?, now(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    for n, w in enumerate(work, 1):
        if progress:
            progress(n, len(work), f"{w['rel_path']} [{w['sheet']}]")
        started = datetime.now(timezone.utc)
        loaded = empty = bad = replaced = 0
        stats = {"ambiguous_dates": 0}
        err = ""
        con.execute("BEGIN")
        try:
            blob = read_blob(con, w["sha256"])
            # replace what this sheet loaded before: same content (reload) or the same file location with older content
            same = "(_source_sha256 = ? OR _source_id = ?) AND _sheet = ? AND coalesce(_coverholder, '') = ?"
            sargs = [w["sha256"], w["source_id"], w["sheet"], w["coverholder"]]
            replaced = con.execute(f"SELECT count(*) FROM {quote_ident(table)} WHERE {same}", sargs).fetchone()[0]
            if replaced:
                con.execute(f"DELETE FROM {quote_ident(table)} WHERE {same}", sargs)
            gen = (_rows_m_mode(con, entity, w, blob, plan, col_names, types, stats) if kind == "input"
                   else _rows_attribute_mode(con, entity, w, blob, col_names, types, order, stats))
            buf = []
            for k, vals, errs in gen:
                if vals is None:
                    empty += 1
                    continue
                bad += len(errs)
                buf.append(vals + [load_id, w["rel_path"], w["sha256"], w["sheet"], k, w["layout_hash"],
                                   started, ",".join(errs) or None, w["source_id"], w["coverholder"]])
                if len(buf) >= batch:
                    loaded += bulk_insert(con, table, all_cols, buf)
                    buf = []
            loaded += bulk_insert(con, table, all_cols, buf)
            con.execute("UPDATE load_log SET status = 'superseded' WHERE entity = ? AND sheet = ? AND status = 'ok' "
                        "AND coalesce(coverholder, '') = ? AND (source_sha256 = ? OR source_id = ?)",
                        [entity, w["sheet"], w["coverholder"], w["sha256"], w["source_id"]])
            con.execute(log_sql, [load_id, entity, dataflow_id, started, w["sha256"], w["rel_path"], w["sheet"],
                                  w["layout_hash"], loaded, empty, bad, "ok", "", w["source_id"], w["coverholder"]])
            con.execute("COMMIT")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            con.execute("ROLLBACK")                       # no partial rows, no log entry for this sheet
            loaded = replaced = 0
            con.execute(log_sql, [load_id, entity, dataflow_id, started, w["sha256"], w["rel_path"], w["sheet"],
                                  w["layout_hash"], 0, 0, 0, "error", err, w["source_id"], w["coverholder"]])
        if err:
            total["sheets_failed"] += 1
        else:
            total["sheets_loaded"] += 1
            total["ambiguous_dates"] += stats["ambiguous_dates"]
        total["rows"] += loaded
        total["replaced_rows"] += replaced
        total["coerce_error_cells"] += bad
    return total
