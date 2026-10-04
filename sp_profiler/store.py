"""Persist profiling results in DuckDB (append-only, one ``run_id`` per profiling run)."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .dataflow import DATAFLOW_DDL, migrate
from .mapping_load import LOAD_DDL

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY, started_utc TIMESTAMPTZ, tool_version VARCHAR, source_type VARCHAR,
    root VARCHAR, site_url VARCHAR, library VARCHAR, folder VARCHAR, params JSON, summary JSON,
    status VARCHAR DEFAULT 'complete', finished_utc TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS files (
    run_id VARCHAR, rel_path VARCHAR, name VARCHAR, folder VARCHAR, depth INTEGER,
    path_segments VARCHAR[], extension VARCHAR, size_bytes BIGINT,
    created TIMESTAMPTZ, modified TIMESTAMPTZ, modified_by VARCHAR, created_by VARCHAR,
    modified_by_email VARCHAR, location VARCHAR, site_url VARCHAR, library VARCHAR, drive_id VARCHAR,
    item_id VARCHAR, sp_path VARCHAR, mime_type VARCHAR, etag VARCHAR, ctag VARCHAR,
    quickxor_hash VARCHAR, sha1_hash VARCHAR, sha256 VARCHAR,
    inspected BOOLEAN, status VARCHAR, error VARCHAR, sheet_count INTEGER, data_sheet_count INTEGER,
    total_data_rows BIGINT, primary_sheet VARCHAR, primary_layout VARCHAR, primary_family VARCHAR,
    layout_hashes VARCHAR, has_macros BOOLEAN, content_stored BOOLEAN,
    primary_layout_hash VARCHAR, warnings VARCHAR, reused_from VARCHAR, coverholder VARCHAR);

-- Raw file bytes, stored once per distinct content (files.sha256 -> file_blobs.sha256).
-- Either in the database (content) or on disk (path, when profiled with a blob directory).
CREATE TABLE IF NOT EXISTS file_blobs (
    sha256 VARCHAR PRIMARY KEY, size_bytes BIGINT, stored_utc TIMESTAMPTZ DEFAULT now(), content BLOB, path VARCHAR);

CREATE TABLE IF NOT EXISTS sheets (
    run_id VARCHAR, rel_path VARCHAR, sheet VARCHAR, state VARCHAR, max_row INTEGER, max_col INTEGER,
    header_row INTEGER, data_rows INTEGER, header_count INTEGER, layout VARCHAR,
    layout_hash VARCHAR, set_hash VARCHAR);

CREATE TABLE IF NOT EXISTS sheet_headers (
    run_id VARCHAR, rel_path VARCHAR, sheet VARCHAR, layout VARCHAR, position INTEGER,
    raw_header VARCHAR, norm_header VARCHAR, layout_hash VARCHAR);

CREATE TABLE IF NOT EXISTS layouts (
    run_id VARCHAR, layout VARCHAR, layout_hash VARCHAR, family VARCHAR, files INTEGER, sheets INTEGER,
    columns INTEGER, total_data_rows BIGINT, headers VARCHAR[], norm_headers VARCHAR[],
    added_vs_family_seed VARCHAR[], removed_vs_family_seed VARCHAR[], reordered_vs_family_seed BOOLEAN,
    examples VARCHAR[]);
"""

# Columns added after the first release: applied to older databases too.
ADDED_COLUMNS = [("runs", "status", "VARCHAR DEFAULT 'complete'"), ("runs", "finished_utc", "TIMESTAMPTZ"),
                 ("files", "primary_layout_hash", "VARCHAR"), ("files", "warnings", "VARCHAR"),
                 ("files", "reused_from", "VARCHAR"), ("file_blobs", "path", "VARCHAR"),
                 ("sheet_headers", "layout_hash", "VARCHAR"), ("load_log", "source_id", "VARCHAR"),
                 ("files", "coverholder", "VARCHAR"), ("load_log", "coverholder", "VARCHAR")]

# "Latest state" views: one row per file identity (SharePoint item id, else absolute path), newest run wins, so
# profiling a subset or another folder never hides files profiled earlier.
VIEWS = """
CREATE OR REPLACE VIEW latest_run AS SELECT run_id FROM runs ORDER BY started_utc DESC LIMIT 1;
CREATE OR REPLACE VIEW v_files AS
    SELECT f.* FROM files f JOIN runs r USING (run_id)
    QUALIFY row_number() OVER (PARTITION BY COALESCE(NULLIF(f.item_id, ''), f.location, f.rel_path)
                               ORDER BY r.started_utc DESC, f.run_id DESC) = 1;
CREATE OR REPLACE VIEW v_files_last_run AS SELECT * FROM files WHERE run_id = (SELECT run_id FROM latest_run);
CREATE OR REPLACE VIEW v_sheets AS SELECT s.* FROM sheets s JOIN v_files f USING (run_id, rel_path);
CREATE OR REPLACE VIEW v_layouts AS
    WITH h AS (SELECT run_id, rel_path, sheet, any_value(layout_hash) AS layout_hash,
                      list(raw_header ORDER BY position) AS headers, list(norm_header ORDER BY position) AS norm_headers
               FROM sheet_headers GROUP BY run_id, rel_path, sheet)
    SELECT 'L' || lpad(CAST(row_number() OVER (ORDER BY count(DISTINCT f.sha256) DESC, s.layout_hash) AS VARCHAR), 2, '0') AS layout,
           s.layout_hash, count(DISTINCT f.sha256) AS files, count(*) AS sheets,
           any_value(len(h.norm_headers)) AS columns, any_value(f.rel_path) AS example,
           any_value(h.headers) AS headers, any_value(h.norm_headers) AS norm_headers,
           list(DISTINCT f.coverholder) FILTER (WHERE f.coverholder <> '') AS coverholders
    FROM v_sheets s JOIN v_files f USING (run_id, rel_path) JOIN h USING (run_id, rel_path, sheet)
    WHERE s.header_row IS NOT NULL AND f.sha256 <> '' GROUP BY s.layout_hash;
CREATE OR REPLACE VIEW v_file_layouts AS
    SELECT f.rel_path, f.size_bytes, f.modified, s.sheet, s.data_rows, l.layout, s.layout_hash, l.norm_headers
    FROM v_sheets s JOIN v_files f USING (run_id, rel_path) JOIN v_layouts l ON l.layout_hash = s.layout_hash
    WHERE s.header_row IS NOT NULL;
CREATE OR REPLACE VIEW v_duplicates AS
    SELECT sha256, count(*) AS copies, list(rel_path) AS paths FROM v_files
    WHERE sha256 <> '' GROUP BY sha256 HAVING count(*) > 1;
"""

TS = {"created", "modified"}
INSPECT_KEY_FIELDS = ("scan_rows", "min_headers", "include_hidden", "exact_rows")


def inspect_key(scan_rows=50, min_headers=3, include_hidden=False, exact_rows=False) -> str:
    """Fingerprint of the inspection settings: results are only reused when it matches."""
    return f"{__version__}/{scan_rows}/{min_headers}/{int(include_hidden)}/{int(exact_rows)}"


def _insert(con, table: str, rows: list[dict], cols: list[str], bulk: bool = True) -> None:
    if not rows:
        return
    if bulk:
        from .bulk import bulk_insert
        bulk_insert(con, table, cols, [[r.get(c) for c in cols] for r in rows])
        return
    ph = ", ".join("?::TIMESTAMPTZ" if c in TS else "?" for c in cols)
    con.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})",
                    [[r.get(c) for c in cols] for r in rows])


def table_columns(con, table: str) -> list[str]:
    return [r[0] for r in con.execute(f"SELECT column_name FROM information_schema.columns "
                                      f"WHERE table_name = '{table}' ORDER BY ordinal_position").fetchall()]


def open_db(db_path: str | Path):
    import duckdb
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(DDL)
    migrate(con)
    con.execute(DATAFLOW_DDL)
    con.execute(LOAD_DDL)
    from .pipeline import PIPELINE_DDL
    con.execute(PIPELINE_DDL)
    for table, col, typ in ADDED_COLUMNS:
        con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
    con.execute("UPDATE sheet_headers SET layout_hash = s.layout_hash FROM sheets s WHERE sheet_headers.layout_hash IS NULL "
                "AND sheet_headers.run_id = s.run_id AND sheet_headers.rel_path = s.rel_path AND sheet_headers.sheet = s.sheet")
    con.execute(VIEWS)
    from .m2sql import register_udfs
    register_udfs(con)
    return con


class BlobSink:
    """Stores file bytes (deduplicated by SHA-256) in ``file_blobs``, or on disk under ``blob_dir``.

    Safe to call from several threads. Files over ``max_bytes`` are skipped (and counted).
    """

    def __init__(self, con, max_bytes: int = 200 * 1024 * 1024, blob_dir: str | Path | None = None):
        self.con, self.max_bytes = con, max_bytes
        self.blob_dir = Path(blob_dir) if blob_dir else None
        self.skipped_too_large = 0

    def __call__(self, local_path, sha256: str) -> bool:
        import shutil
        path = Path(local_path)
        size = path.stat().st_size
        if size > self.max_bytes:
            self.skipped_too_large += 1
            return False
        cur = self.con.cursor()
        if cur.execute("SELECT 1 FROM file_blobs WHERE sha256 = ?", [sha256]).fetchone():
            return True
        try:
            if self.blob_dir:
                dest = self.blob_dir / sha256[:2] / f"{sha256}{path.suffix.lower()}"
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + f".{uuid.uuid4().hex[:6]}.tmp")
                    shutil.copyfile(path, tmp)
                    os.replace(tmp, dest)
                cur.execute("INSERT INTO file_blobs (sha256, size_bytes, content, path) VALUES (?, ?, NULL, ?)",
                            [sha256, size, str(dest.resolve())])
            else:       # read inside DuckDB: no Python-side copy of the whole file
                cur.execute("INSERT INTO file_blobs (sha256, size_bytes, content) "
                            "SELECT ?, ?, content FROM read_blob(?)", [sha256, size, str(path)])
        except Exception as e:
            if "constraint" in str(e).lower() or "duplicate" in str(e).lower():
                return True               # another worker stored the same content first
            raise
        return True


def read_blob(con, sha256: str) -> bytes:
    """The stored bytes for a file hash, from the database or from the blob directory."""
    row = con.execute("SELECT content, path FROM file_blobs WHERE sha256 = ?", [sha256]).fetchone()
    if not row:
        raise LookupError(f"No stored content for {sha256[:12]}")
    content, path = row
    if content is not None:
        return bytes(content)
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Stored file is missing from disk: {p}")
        return p.read_bytes()
    raise LookupError(f"No stored content for {sha256[:12]}")


def has_blob(con, sha256: str) -> bool:
    return bool(con.execute("SELECT 1 FROM file_blobs WHERE sha256 = ?", [sha256]).fetchone())


def export_file(con, rel_path: str, dest: str | Path) -> Path:
    """Write the stored bytes of ``rel_path`` (latest run containing it) to ``dest`` (dir or file path)."""
    row = con.execute(
        "SELECT f.name, f.sha256 FROM files f JOIN file_blobs b USING (sha256) "
        "JOIN runs r USING (run_id) WHERE f.rel_path = ? ORDER BY r.started_utc DESC LIMIT 1", [rel_path]).fetchone()
    if not row:
        raise LookupError(f"No stored content for {rel_path!r}")
    dest = Path(dest)
    if dest.is_dir():
        dest = dest / row[0]
    dest.write_bytes(read_blob(con, row[1]))
    return dest


class RunWriter:
    """Writes a profiling run to the database as it progresses (so an interrupted run keeps its work).

    ``add`` buffers a file and its sheets and flushes every ``flush_every`` files in one transaction; ``finish``
    writes the layouts, back-fills the layout ids on files/sheets/headers, stores the summary and marks the run
    complete. A run that never finishes stays ``status = 'running'`` and its files still count for reuse.
    """

    def __init__(self, con, source_type: str, root: str, site_url: str = "", library: str = "", folder: str = "",
                 params: dict | None = None, flush_every: int = 200):
        self.con, self.flush_every = con, flush_every
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
        self._files: list[dict] = []
        self._sheets: list[dict] = []
        con.execute("INSERT INTO runs (run_id, started_utc, tool_version, source_type, root, site_url, library, folder, "
                    "params, status) VALUES (?, now(), ?, ?, ?, ?, ?, ?, ?, 'running')",
                    [self.run_id, __version__, source_type, root, site_url, library, folder, json.dumps(params or {})])

    def add(self, file_row: dict, sheet_rows: list[dict]) -> None:
        self._files.append({**file_row, "run_id": self.run_id})
        self._sheets.extend({**s, "run_id": self.run_id} for s in sheet_rows)
        if len(self._files) >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if not self._files and not self._sheets:
            return
        files, sheets = self._files, self._sheets
        hdr = [{"run_id": self.run_id, "rel_path": s["rel_path"], "sheet": s["sheet"], "layout": s.get("layout"),
                "layout_hash": s["layout_hash"], "position": i + 1, "raw_header": raw, "norm_header": norm}
               for s in sheets if s["header_row"] is not None
               for i, (raw, norm) in enumerate(zip(s["headers"], s["norm_headers"]))]
        self.con.execute("BEGIN")
        try:
            _insert(self.con, "files", files, [c for c in table_columns(self.con, "files") if c != "path_segments"])
            self.con.execute("UPDATE files SET path_segments = string_split(rel_path, '/') "
                             "WHERE run_id = ? AND path_segments IS NULL", [self.run_id])
            _insert(self.con, "sheets", sheets, table_columns(self.con, "sheets"))
            _insert(self.con, "sheet_headers", hdr, table_columns(self.con, "sheet_headers"))
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        self._files, self._sheets = [], []

    def finish(self, result: dict) -> str:
        self.flush()
        lay_rows, mapping = [], []
        for item in result["layouts"]:
            d = result["layout_diffs"][item.layout_id]
            mapping.append([item.layout_hash, item.layout_id, item.family])
            lay_rows.append({"run_id": self.run_id, "layout": item.layout_id, "layout_hash": item.layout_hash,
                             "family": item.family, "files": len(item.files), "sheets": item.sheets,
                             "columns": len(item.norm_headers), "total_data_rows": item.total_rows,
                             "headers": item.headers, "norm_headers": item.norm_headers,
                             "added_vs_family_seed": d["added"], "removed_vs_family_seed": d["removed"],
                             "reordered_vs_family_seed": d["reordered"], "examples": item.examples})
        c = self.con
        c.execute("BEGIN")
        try:
            _insert(c, "layouts", lay_rows, table_columns(c, "layouts"), bulk=False)     # list-valued columns, few rows
            if mapping:
                c.execute("CREATE OR REPLACE TEMP TABLE _lay_map (layout_hash VARCHAR, layout VARCHAR, family VARCHAR)")
                c.executemany("INSERT INTO _lay_map VALUES (?, ?, ?)", mapping)
                c.execute("UPDATE sheets SET layout = m.layout FROM _lay_map m "
                          "WHERE sheets.run_id = ? AND sheets.layout_hash = m.layout_hash", [self.run_id])
                c.execute("UPDATE sheet_headers SET layout = m.layout FROM _lay_map m "
                          "WHERE sheet_headers.run_id = ? AND sheet_headers.layout_hash = m.layout_hash", [self.run_id])
                c.execute("UPDATE files SET primary_layout = m.layout, primary_family = m.family FROM _lay_map m "
                          "WHERE files.run_id = ? AND files.primary_layout_hash = m.layout_hash", [self.run_id])
            c.execute("UPDATE runs SET summary = ?, status = 'complete', finished_utc = now() WHERE run_id = ?",
                      [json.dumps(result["summary"], default=str), self.run_id])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return self.run_id

    def fail(self, message: str) -> None:
        try:
            self.flush()
        finally:
            self.con.execute("UPDATE runs SET status = 'failed', summary = ?, finished_utc = now() WHERE run_id = ?",
                             [json.dumps({"error": message}), self.run_id])


def save_run(con, result: dict, source_type: str, root: str,
             site_url: str = "", library: str = "", folder: str = "", params: dict | None = None) -> str:
    """Write a finished profiling result in one go (the streaming path is ``RunWriter``)."""
    by_file: dict[str, list[dict]] = {}
    for s in result["sheets"]:
        by_file.setdefault(s["rel_path"], []).append(s)
    w = RunWriter(con, source_type, root, site_url, library, folder, params)
    try:
        for f in result["files"]:
            w.add(f, by_file.get(f["rel_path"], []))
        return w.finish(result)
    except Exception as e:
        w.fail(str(e))
        raise


def make_previous(con, key: str, want_content: bool = True):
    """Build the ``previous`` callback for ``profile()``: reuse results of unchanged files from earlier runs.

    A file is unchanged when its identity (SharePoint item id, else absolute path), size and eTag (SharePoint) or
    modified time (local) match a row from a run made with the same inspection settings. Failed inspections are retried,
    and when file bytes are wanted the stored copy must still exist.
    """
    def lookup(entry):
        ident = entry.meta.get("item_id") or entry.location
        row = con.execute(
            "SELECT f.*, r.run_id AS _prev_run FROM files f JOIN runs r USING (run_id) "
            "WHERE COALESCE(NULLIF(f.item_id, ''), f.location, f.rel_path) = ? AND f.size_bytes = ? AND f.inspected "
            "AND f.status IS NOT NULL AND f.status <> 'error' AND json_extract_string(r.params, 'inspect_key') = ? "
            "ORDER BY r.started_utc DESC LIMIT 1", [ident, entry.size_bytes, key])
        cols = [d[0] for d in row.description]
        rec = row.fetchone()
        if not rec:
            return None
        prev = dict(zip(cols, rec))
        if entry.meta.get("etag"):
            if prev.get("etag") != entry.meta["etag"]:
                return None
        elif _iso(prev.get("modified")) != _iso(entry.modified):
            return None
        if want_content and prev.get("sha256") and not has_blob(con, prev["sha256"]):
            return None
        run_id = prev.pop("_prev_run")
        file_row = {k: (_iso(v) if k in ("created", "modified") else v) for k, v in prev.items()
                    if k not in ("run_id", "path_segments")}
        file_row["reused_from"] = run_id
        sheets = []
        res = con.execute("SELECT * FROM sheets WHERE run_id = ? AND rel_path = ?", [run_id, prev["rel_path"]])
        scols = [d[0] for d in res.description]
        for s in res.fetchall():
            rec = dict(zip(scols, s))
            hdr = con.execute("SELECT raw_header, norm_header FROM sheet_headers WHERE run_id = ? AND rel_path = ? AND sheet = ? "
                              "ORDER BY position", [run_id, prev["rel_path"], rec["sheet"]]).fetchall()
            rec["headers"], rec["norm_headers"] = [h[0] for h in hdr], [h[1] for h in hdr]
            rec.pop("run_id", None)
            rec.pop("layout", None)
            sheets.append(rec)
        return file_row, sheets
    return lookup


def _iso(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return str(v)
