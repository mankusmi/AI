"""Persist profiling results in DuckDB (append-only, one ``run_id`` per profiling run)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY, started_utc TIMESTAMPTZ, tool_version VARCHAR, source_type VARCHAR,
    root VARCHAR, site_url VARCHAR, library VARCHAR, folder VARCHAR, params JSON, summary JSON);

CREATE TABLE IF NOT EXISTS files (
    run_id VARCHAR, rel_path VARCHAR, name VARCHAR, folder VARCHAR, depth INTEGER,
    path_segments VARCHAR[], extension VARCHAR, size_bytes BIGINT,
    created TIMESTAMPTZ, modified TIMESTAMPTZ, modified_by VARCHAR, created_by VARCHAR,
    modified_by_email VARCHAR, location VARCHAR, site_url VARCHAR, library VARCHAR, drive_id VARCHAR,
    item_id VARCHAR, sp_path VARCHAR, mime_type VARCHAR, etag VARCHAR, ctag VARCHAR,
    quickxor_hash VARCHAR, sha1_hash VARCHAR, sha256 VARCHAR,
    inspected BOOLEAN, status VARCHAR, error VARCHAR, sheet_count INTEGER, data_sheet_count INTEGER,
    total_data_rows BIGINT, primary_sheet VARCHAR, primary_layout VARCHAR, primary_family VARCHAR,
    layout_hashes VARCHAR, has_macros BOOLEAN);

CREATE TABLE IF NOT EXISTS sheets (
    run_id VARCHAR, rel_path VARCHAR, sheet VARCHAR, state VARCHAR, max_row INTEGER, max_col INTEGER,
    header_row INTEGER, data_rows INTEGER, header_count INTEGER, layout VARCHAR,
    layout_hash VARCHAR, set_hash VARCHAR);

CREATE TABLE IF NOT EXISTS sheet_headers (
    run_id VARCHAR, rel_path VARCHAR, sheet VARCHAR, layout VARCHAR, position INTEGER,
    raw_header VARCHAR, norm_header VARCHAR);

CREATE TABLE IF NOT EXISTS layouts (
    run_id VARCHAR, layout VARCHAR, layout_hash VARCHAR, family VARCHAR, files INTEGER, sheets INTEGER,
    columns INTEGER, total_data_rows BIGINT, headers VARCHAR[], norm_headers VARCHAR[],
    added_vs_family_seed VARCHAR[], removed_vs_family_seed VARCHAR[], reordered_vs_family_seed BOOLEAN,
    examples VARCHAR[]);

CREATE OR REPLACE VIEW latest_run AS SELECT run_id FROM runs ORDER BY started_utc DESC LIMIT 1;
CREATE OR REPLACE VIEW v_files AS SELECT * FROM files WHERE run_id = (SELECT run_id FROM latest_run);
CREATE OR REPLACE VIEW v_layouts AS SELECT * FROM layouts WHERE run_id = (SELECT run_id FROM latest_run);
CREATE OR REPLACE VIEW v_file_layouts AS
    SELECT f.rel_path, f.size_bytes, f.modified, s.sheet, s.data_rows, s.layout, l.family, l.norm_headers
    FROM sheets s JOIN files f USING (run_id, rel_path) JOIN layouts l USING (run_id, layout)
    WHERE s.run_id = (SELECT run_id FROM latest_run);
CREATE OR REPLACE VIEW v_duplicates AS
    SELECT sha256, count(*) AS copies, list(rel_path) AS paths FROM v_files
    WHERE sha256 <> '' GROUP BY sha256 HAVING count(*) > 1;
"""

TS = {"created", "modified"}


def _insert(con, table: str, rows: list[dict], cols: list[str]) -> None:
    if not rows:
        return
    ph = ", ".join("?::TIMESTAMPTZ" if c in TS else "?" for c in cols)
    con.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})",
                    [[r.get(c) for c in cols] for r in rows])


def table_columns(con, table: str) -> list[str]:
    return [r[0] for r in con.execute(f"SELECT column_name FROM information_schema.columns "
                                      f"WHERE table_name = '{table}' ORDER BY ordinal_position").fetchall()]


def save_run(db_path: str | Path, result: dict, source_type: str, root: str,
             site_url: str = "", library: str = "", folder: str = "", params: dict | None = None) -> str:
    import duckdb
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(DDL)
        con.execute("BEGIN")
        con.execute("INSERT INTO runs VALUES (?, now(), ?, ?, ?, ?, ?, ?, ?, ?)",
                    [run_id, __version__, source_type, root, site_url, library, folder,
                     json.dumps(params or {}), json.dumps(result["summary"], default=str)])
        files = [{**f, "run_id": run_id, "path_segments": f["rel_path"].split("/")} for f in result["files"]]
        _insert(con, "files", files, [c for c in table_columns(con, "files")])
        sheets = [{**s, "run_id": run_id} for s in result["sheets"]]
        _insert(con, "sheets", sheets, table_columns(con, "sheets"))
        hdr = [{"run_id": run_id, "rel_path": s["rel_path"], "sheet": s["sheet"], "layout": s.get("layout"),
                "position": i + 1, "raw_header": raw, "norm_header": norm}
               for s in result["sheets"] if s["header_row"] is not None
               for i, (raw, norm) in enumerate(zip(s["headers"], s["norm_headers"]))]
        _insert(con, "sheet_headers", hdr, table_columns(con, "sheet_headers"))
        lay = []
        for l in result["layouts"]:
            d = result["layout_diffs"][l.layout_id]
            lay.append({"run_id": run_id, "layout": l.layout_id, "layout_hash": l.layout_hash,
                        "family": l.family, "files": len(l.files), "sheets": l.sheets,
                        "columns": len(l.norm_headers), "total_data_rows": l.total_rows,
                        "headers": l.headers, "norm_headers": l.norm_headers,
                        "added_vs_family_seed": d["added"], "removed_vs_family_seed": d["removed"],
                        "reordered_vs_family_seed": d["reordered"], "examples": l.examples})
        _insert(con, "layouts", lay, table_columns(con, "layouts"))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return run_id
