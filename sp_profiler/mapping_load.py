"""Apply column mappings to stored Excel files and append the rows to a typed DuckDB table."""
from __future__ import annotations

import io
import re
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

LOAD_DDL = """
CREATE TABLE IF NOT EXISTS load_log (
    load_id VARCHAR, entity VARCHAR, dataflow_id VARCHAR, started_utc TIMESTAMPTZ, finished_utc TIMESTAMPTZ,
    source_sha256 VARCHAR, source_path VARCHAR, sheet VARCHAR, layout_hash VARCHAR,
    rows_loaded BIGINT, rows_skipped_empty BIGINT, coerce_error_cells BIGINT, status VARCHAR, error VARCHAR);
"""

DUCK_TYPES = {
    "string": "VARCHAR", "guid": "VARCHAR", "int64": "BIGINT", "int32": "INTEGER", "int16": "SMALLINT",
    "byte": "TINYINT", "double": "DOUBLE", "float": "DOUBLE", "single": "DOUBLE", "decimal": "DECIMAL(38,10)",
    "boolean": "BOOLEAN", "datetime": "TIMESTAMP", "datetimeoffset": "TIMESTAMPTZ", "date": "DATE",
    "time": "TIME",
}
PROVENANCE = [("_load_id", "VARCHAR"), ("_source_path", "VARCHAR"), ("_source_sha256", "VARCHAR"),
              ("_sheet", "VARCHAR"), ("_excel_row", "INTEGER"), ("_layout_hash", "VARCHAR"),
              ("_loaded_utc", "TIMESTAMPTZ"), ("_coerce_errors", "VARCHAR")]
DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d %b %Y", "%d-%b-%Y", "%d/%m/%y", "%m/%d/%Y")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def target_table(entity: str) -> str:
    return "df_" + re.sub(r"[^0-9a-zA-Z]+", "_", entity).strip("_").lower()


def duck_type(data_type: str) -> str:
    return DUCK_TYPES.get(str(data_type).lower(), "VARCHAR")


def ensure_target_table(con, entity: str, attributes: list[dict]) -> str:
    """Create ``df_<entity>`` or add any attributes missing from it (schema evolution)."""
    table = target_table(entity)
    cols = [(a["name"], duck_type(a["data_type"])) for a in attributes]
    exists = con.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]).fetchone()
    if not exists:
        ddl = ", ".join(f"{quote_ident(n)} {t}" for n, t in cols + PROVENANCE)
        con.execute(f"CREATE TABLE {quote_ident(table)} ({ddl})")
    else:
        have = {r[0].lower() for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]).fetchall()}
        for n, t in cols:
            if n.lower() not in have:
                con.execute(f"ALTER TABLE {quote_ident(table)} ADD COLUMN {quote_ident(n)} {t}")
    return table


# ------------------------------------------------------------------ coercion
def _excel_serial(v: float) -> Optional[datetime]:
    if 20000 < v < 80000:
        from openpyxl.utils.datetime import from_excel
        return from_excel(v)
    return None


def _to_datetime(v) -> Optional[datetime]:
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
    for fmt in DATE_FORMATS:         # day-first by default: bordereaux are mostly UK/EU formatted
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


def coerce(value, data_type: str):
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
            d = _to_datetime(value)
            if d is None:
                return None, False
            if dt == "datetimeoffset" and d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d, True
        if dt == "date":
            d = _to_datetime(value)
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
def pending_sheets(con, entity: str, layout_hashes: Optional[list[str]] = None, force: bool = False,
                   kind: str = "attribute") -> list[dict]:
    """Distinct (content, sheet) units with a mapped layout and stored bytes, minus those already loaded."""
    rows = con.execute("""
        SELECT f.sha256, any_value(f.rel_path) AS rel_path, s.sheet, s.layout_hash, min(s.header_row) AS header_row
        FROM sheets s JOIN files f USING (run_id, rel_path)
        JOIN file_blobs b ON b.sha256 = f.sha256
        WHERE s.header_row IS NOT NULL AND s.layout_hash IN
              (SELECT DISTINCT layout_hash FROM column_mappings WHERE entity = ? AND kind = ?)
        GROUP BY f.sha256, s.sheet, s.layout_hash ORDER BY rel_path, s.sheet""", [entity, kind]).fetchall()
    done = set() if force else set(con.execute(
        "SELECT source_sha256, sheet FROM load_log WHERE entity = ? AND status = 'ok'", [entity]).fetchall())
    out = []
    for sha, path, sheet, lh, hr in rows:
        if layout_hashes and lh not in layout_hashes:
            continue
        if (sha, sheet) in done:
            continue
        out.append({"sha256": sha, "rel_path": path, "sheet": sheet, "layout_hash": lh, "header_row": hr})
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


def _rows_attribute_mode(con, entity, w, blob, col_names, types):
    mapping = dict(con.execute("SELECT norm_header, attribute FROM column_mappings WHERE entity = ? AND layout_hash = ? "
                               "AND kind = 'attribute'", [entity, w["layout_hash"]]).fetchall())
    mapping = {h: a for h, a in mapping.items() if a in types}      # drop attrs gone from this dataflow
    header, rows = _sheet_rows(blob, w["sheet"], w["header_row"])
    col_for = {}
    for i, h in enumerate(_norm_header_cells(header)):
        if h in mapping and mapping[h] not in col_for:
            col_for[mapping[h]] = i
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
            v, ok = coerce(raw, types[name])
            vals.append(v)
            if not ok:
                errs.append(name)
        yield k, vals, errs


def _rows_m_mode(con, entity, w, blob, plan, col_names, types, batch=20000):
    """Run the translated Power Query pipeline over one sheet (staged as VARCHAR columns)."""
    from .m2sql import stage_table, stage_value
    inputs = plan["inputs"]
    mapping = dict(con.execute("SELECT norm_header, attribute FROM column_mappings WHERE entity = ? AND layout_hash = ? "
                               "AND kind = 'input'", [entity, w["layout_hash"]]).fetchall())
    header, rows = _sheet_rows(blob, w["sheet"], w["header_row"])
    col_for = {}
    for i, h in enumerate(_norm_header_cells(header)):
        if h in mapping and mapping[h] in inputs and mapping[h] not in col_for:
            col_for[mapping[h]] = i
    staged, empty = [], 0
    for k, row in rows:
        vals = [stage_value(row[col_for[c]]) if c in col_for and col_for[c] < len(row) else None for c in inputs]
        if all(v is None or not v.strip() for v in vals):
            empty += 1
            continue
        staged.append((k, *vals))
    stage_table(con, inputs, staged)
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
            v, ok = coerce(r[i] if i is not None else None, types[name])
            vals.append(v)
            if not ok:
                errs.append(name)
        yield (r[row_i] if row_i is not None else None), vals, errs


def load_entity(con, dataflow_id: str, entity: str, layout_hashes: Optional[list[str]] = None,
                force: bool = False, progress: Optional[Callable[[int, int, str], None]] = None,
                batch: int = 5000) -> dict:
    """Append rows for ``entity`` from every pending stored sheet. Returns a summary dict.

    Uses the translated Power Query pipeline when the entity has one enabled, otherwise the plain
    column-to-attribute mapping.
    """
    from . import transforms
    from .m2sql import register_udfs
    attrs = [{"name": r[0], "data_type": r[1]} for r in con.execute(
        "SELECT name, data_type FROM dataflow_attributes WHERE dataflow_id = ? AND entity = ? ORDER BY position",
        [dataflow_id, entity]).fetchall()]
    if not attrs:
        raise LookupError(f"Entity {entity!r} not found in dataflow {dataflow_id}")
    plan = transforms.resolve(con, dataflow_id, entity)
    if plan["use_m"] and plan["mode"] != "m":
        raise ValueError(f"Power Query transformation for {entity!r} is not ready ({plan['status']})")
    kind = "input" if plan["mode"] == "m" else "attribute"
    if kind == "input":
        register_udfs(con)
    table = ensure_target_table(con, entity, attrs)
    types = {a["name"]: a["data_type"] for a in attrs}
    col_names = [a["name"] for a in attrs]
    load_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    work = pending_sheets(con, entity, layout_hashes, force, kind)
    total = {"load_id": load_id, "table": table, "sheets_loaded": 0, "sheets_failed": 0, "mode": plan["mode"],
             "rows": 0, "coerce_error_cells": 0, "sheets_planned": len(work)}
    insert_sql = (f"INSERT INTO {quote_ident(table)} ({', '.join(quote_ident(c) for c in col_names + [p[0] for p in PROVENANCE])}) "
                  f"VALUES ({', '.join('?' for _ in col_names + PROVENANCE)})")
    for n, w in enumerate(work, 1):
        if progress:
            progress(n, len(work), f"{w['rel_path']} [{w['sheet']}]")
        started = datetime.now(timezone.utc)
        loaded = empty = bad = 0
        status, err = "ok", ""
        try:
            blob = bytes(con.execute("SELECT content FROM file_blobs WHERE sha256 = ?", [w["sha256"]]).fetchone()[0])
            gen = (_rows_m_mode(con, entity, w, blob, plan, col_names, types) if kind == "input"
                   else _rows_attribute_mode(con, entity, w, blob, col_names, types))
            buf = []
            for k, vals, errs in gen:
                if vals is None:
                    empty += 1
                    continue
                bad += len(errs)
                buf.append(vals + [load_id, w["rel_path"], w["sha256"], w["sheet"], k, w["layout_hash"],
                                   started, ",".join(errs) or None])
                if len(buf) >= batch:
                    con.executemany(insert_sql, buf)
                    loaded += len(buf)
                    buf = []
            if buf:
                con.executemany(insert_sql, buf)
                loaded += len(buf)
        except Exception as e:
            status, err = "error", f"{type(e).__name__}: {e}"
        if status == "error":
            # remove partial rows from this sheet so a retry does not duplicate them
            con.execute(f"DELETE FROM {quote_ident(table)} WHERE _load_id = ? AND _source_sha256 = ? AND _sheet = ?",
                        [load_id, w["sha256"], w["sheet"]])
            loaded = 0
            total["sheets_failed"] += 1
        else:
            total["sheets_loaded"] += 1
        total["rows"] += loaded
        total["coerce_error_cells"] += bad
        con.execute("INSERT INTO load_log VALUES (?, ?, ?, ?, now(), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [load_id, entity, dataflow_id, started, w["sha256"], w["rel_path"], w["sheet"], w["layout_hash"],
                     loaded, empty, bad, status, err])
    return total
