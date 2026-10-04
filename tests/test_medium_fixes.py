"""Tests for the medium-priority review fixes: resume/reuse/parallel runs, latest-state views, header detection,
disk blobs, naming/versioning, Windows paths."""
import datetime
import json
import os
from pathlib import Path

import duckdb
import openpyxl
import pytest

from sp_profiler import dataflow as df
from sp_profiler import mapping_load as ml
from sp_profiler import transforms as tf
from sp_profiler.inspect_excel import cell_text, detect_header_row, inspect_workbook
from sp_profiler.runner import run_profile
from sp_profiler.sources import FileListSource, LocalSource
from sp_profiler.store import open_db, read_blob

from . import test_m_transform as T
from .test_m_transform import make_book

H = ["Policy No", "Premium", "Currency"]


def book(path: Path, headers=H, rows=2, extra=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    make_book(path, headers, [[f"P{i}{extra}", i, "GBP"] for i in range(rows)])


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "src"
    for n in ("a", "b", "c", "d"):
        book(root / f"{n}.xlsx", rows=3, extra=ord(n))
    return root


def db(tmp_path):
    return open_db(str(tmp_path / "t.duckdb"))


# ------------------------------------------------------------------ header detection
def test_header_detection_years_dates_and_text():
    rows = [("Title",), ("Policy", 2023, 2024, 2025), ("P1", 10, 20, 30)]
    assert detect_header_row(rows) == 1                       # year columns count, row below (numbers only) does not
    d = datetime.datetime
    rows = [("Policy", d(2024, 1, 31), d(2024, 2, 29)), ("P1", 1, 2)]
    assert detect_header_row(rows) == 0
    assert detect_header_row([(2023, 2024, 2025), ("x",)]) is None          # needs at least one text header
    assert detect_header_row([("a", "b", "c"), ("P1", 2024, 1999)]) == 0    # data rows do not outrank the header
    assert cell_text(d(2024, 1, 31)) == "2024-01-31" and cell_text(2024.0) == "2024" and cell_text(" x ") == "x"


def test_year_headers_end_to_end(tmp_path):
    p = tmp_path / "y.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Report"])
    ws.append(["Policy", 2023, 2024, 2025])
    ws.append(["P1", 1, 2, 3])
    wb.save(p)
    s = inspect_workbook(p).sheets[0]
    assert s.header_row == 2 and s.headers == ["Policy", "2023", "2024", "2025"]


def test_hidden_sheets_skipped_unless_asked(tmp_path):
    p = tmp_path / "h.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(H)
    ws.append(["P1", 1, "GBP"])
    hid = wb.create_sheet("Hidden")
    hid.append(["Secret A", "Secret B", "Secret C"])
    hid.sheet_state = "hidden"
    wb.save(p)
    info = inspect_workbook(p)
    assert [(s.name, s.state, s.header_row is not None) for s in info.sheets] == [("Sheet", "visible", True), ("Hidden", "hidden", False)]
    assert inspect_workbook(p, include_hidden=True).sheets[1].header_row == 1


def test_uncached_formulas_warn(tmp_path):
    p = tmp_path / "f.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(H)
    ws.append(["P1", "=1+1", "GBP"])          # written by openpyxl: no cached value
    wb.save(p)
    info = inspect_workbook(p)
    assert info.status == "ok" and any("no cached value" in w for w in info.warnings)
    q = tmp_path / "ok.xlsx"
    book(q)
    assert inspect_workbook(q).warnings == []


def test_exact_row_count(tmp_path):
    p = tmp_path / "e.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(H)
    for i in range(5):
        ws.append([f"P{i}", i, "GBP"])
    ws.append([None, None, None])
    ws["A20"].value = None
    wb.save(p)
    assert inspect_workbook(p, exact_rows=True).sheets[0].data_rows == 5


# ------------------------------------------------------------------ runs: reuse, resume, parallel
def count_inspections(monkeypatch):
    import sp_profiler.profiler as prof
    calls = []
    real = prof.inspect_workbook

    def counting(path, *a, **k):
        calls.append(Path(path).name)
        return real(path, *a, **k)
    monkeypatch.setattr(prof, "inspect_workbook", counting)
    return calls


def test_unchanged_files_are_reused_and_changed_ones_reinspected(folder, tmp_path, monkeypatch):
    con = db(tmp_path)
    calls = count_inspections(monkeypatch)
    r1 = run_profile(con, LocalSource(folder), "local", str(folder))
    assert sorted(calls) == ["a.xlsx", "b.xlsx", "c.xlsx", "d.xlsx"] and r1["result"]["summary"]["reused_files"] == 0
    calls.clear()
    r2 = run_profile(con, LocalSource(folder), "local", str(folder))
    assert calls == [] and r2["result"]["summary"]["reused_files"] == 4
    assert r2["result"]["summary"]["distinct_layouts"] == r1["result"]["summary"]["distinct_layouts"] == 1
    assert con.execute("select count(*) from v_files").fetchone()[0] == 4
    assert con.execute("select count(*) from files where reused_from is not null").fetchone()[0] == 4
    book(folder / "b.xlsx", rows=9, extra=1)                  # changed size -> re-inspected
    calls.clear()
    r3 = run_profile(con, LocalSource(folder), "local", str(folder))
    assert calls == ["b.xlsx"] and r3["result"]["summary"]["reused_files"] == 3
    calls.clear()
    run_profile(con, LocalSource(folder), "local", str(folder), refresh=True)
    assert len(calls) == 4
    calls.clear()
    run_profile(con, LocalSource(folder), "local", str(folder), scan_rows=30)      # other settings -> no reuse
    assert len(calls) == 4
    # the reused rows are complete: layouts and headers are available from the newest run
    assert con.execute("select count(*) from v_layouts").fetchone()[0] == 1
    assert con.execute("select files from v_layouts").fetchone()[0] == 4


def test_interrupted_run_keeps_work_and_resumes(folder, tmp_path, monkeypatch):
    import sp_profiler.profiler as prof
    con = db(tmp_path)
    real = prof.inspect_workbook
    n = {"i": 0}

    def dying(path, *a, **k):
        n["i"] += 1
        if n["i"] == 3:
            raise KeyboardInterrupt()          # not an Exception: simulates the process being stopped
        return real(path, *a, **k)
    monkeypatch.setattr(prof, "inspect_workbook", dying)
    with pytest.raises(KeyboardInterrupt):
        run_profile(con, LocalSource(folder), "local", str(folder))
    assert con.execute("select status from runs").fetchone()[0] == "failed"
    assert con.execute("select count(*) from files").fetchone()[0] == 2           # the two finished files were saved
    monkeypatch.setattr(prof, "inspect_workbook", real)
    calls = count_inspections(monkeypatch)
    r = run_profile(con, LocalSource(folder), "local", str(folder))
    assert sorted(calls) == ["c.xlsx", "d.xlsx"] and r["result"]["summary"]["reused_files"] == 2
    assert con.execute("select status from runs order by started_utc desc limit 1").fetchone()[0] == "complete"
    assert con.execute("select count(*) from v_files").fetchone()[0] == 4


def test_parallel_workers_match_sequential(folder, tmp_path):
    con = db(tmp_path)
    seq = run_profile(con, LocalSource(folder), "local", str(folder), workers=1, refresh=True)["result"]
    par = run_profile(con, LocalSource(folder), "local", str(folder), workers=4, refresh=True)["result"]
    assert [f["rel_path"] for f in par["files"]] == [f["rel_path"] for f in seq["files"]]
    assert [f["sha256"] for f in par["files"]] == [f["sha256"] for f in seq["files"]]
    assert par["summary"]["distinct_layouts"] == 1


# ------------------------------------------------------------------ views
def test_views_show_latest_state_across_runs_and_roots(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    book(a / "x.xlsx", extra=1)
    book(b / "y.xlsx", headers=["Ref", "Name", "Amount"], extra=2)
    con = db(tmp_path)
    run_profile(con, LocalSource(a), "local", str(a))
    run_profile(con, LocalSource(b), "local", str(b))
    assert {r[0] for r in con.execute("select name from v_files").fetchall()} == {"x.xlsx", "y.xlsx"}   # second run did not hide the first
    assert con.execute("select count(*) from v_layouts").fetchone()[0] == 2
    run_profile(con, FileListSource([a / "x.xlsx"]), "local", str(a))                                 # a subset run
    assert con.execute("select count(*) from v_files").fetchone()[0] == 2
    assert con.execute("select count(*) from v_files_last_run").fetchone()[0] == 1
    assert con.execute("select count(*) from v_file_layouts").fetchone()[0] == 2


# ------------------------------------------------------------------ disk blobs
def test_blobs_on_disk(folder, tmp_path):
    con = db(tmp_path)
    blobs = tmp_path / "blobs"
    run_profile(con, LocalSource(folder), "local", str(folder), blob_dir=str(blobs))
    rows = con.execute("select sha256, content is null, path from file_blobs").fetchall()
    assert len(rows) == 4 and all(r[1] and Path(r[2]).exists() and r[0] in r[2] for r in rows)
    sha = rows[0][0]
    assert read_blob(con, sha)[:2] == b"PK"
    Path(rows[0][2]).unlink()
    with pytest.raises(FileNotFoundError, match="missing from disk"):
        read_blob(con, sha)


def test_load_works_from_disk_blobs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_book(src / "a.xlsx", ["Policy No", "Insured Name", "Region", "Gross Premium", "Inception Date", "Units", "Currency"],
              [["P1", " acme ", "North", 1200.5, datetime.datetime(2024, 1, 5), 3, "GBP"]])
    con = db(tmp_path)
    run_profile(con, LocalSource(src), "local", str(src), blob_dir=str(tmp_path / "blobs"))
    did, _ = df.store_dataflow(con, df.parse_model_json(json.dumps(T.MODEL)))
    T.map_inputs(con, did)
    assert ml.load_entity(con, did, "Policies")["rows"] == 1


def test_blobs_survive_reuse_check(folder, tmp_path):
    """Reuse needs the stored bytes: if the blob directory file disappears, the file is re-inspected."""
    con = db(tmp_path)
    blobs = tmp_path / "blobs"
    run_profile(con, LocalSource(folder), "local", str(folder), blob_dir=str(blobs))
    con.execute("DELETE FROM file_blobs WHERE sha256 = (SELECT sha256 FROM files WHERE name = 'a.xlsx')")
    r = run_profile(con, LocalSource(folder), "local", str(folder), blob_dir=str(blobs))
    assert r["result"]["summary"]["reused_files"] == 3


# ------------------------------------------------------------------ naming / versioning
def test_table_names_are_unique_per_entity(tmp_path):
    con = db(tmp_path)
    t1, t2, t3 = ml.table_for(con, "A B"), ml.table_for(con, "A-B"), ml.table_for(con, "a_b")
    assert len({t1, t2, t3}) == 3 and t1 == "df_a_b" and ml.table_for(con, "A B") == t1


def test_attribute_problems_and_type_migration(tmp_path):
    con = db(tmp_path)
    with pytest.raises(ValueError, match="differ only by case"):
        ml.ensure_target_table(con, "E", [{"name": "Amount", "data_type": "string"}, {"name": "amount", "data_type": "string"}])
    with pytest.raises(ValueError, match="provenance"):
        ml.ensure_target_table(con, "E2", [{"name": "_load_id", "data_type": "string"}])
    t = ml.ensure_target_table(con, "E3", [{"name": "N", "data_type": "string"}])
    con.execute(f'INSERT INTO {t} (N) VALUES (\'12\'), (\'7\')')
    ml.ensure_target_table(con, "E3", [{"name": "N", "data_type": "int64"}])        # type changed in the dataflow
    assert con.execute(f"select N from {t} order by 1").fetchall() == [(7,), (12,)]
    assert dict(con.execute(f"select column_name, column_type from (describe {t})").fetchall())["N"] == "BIGINT"
    con.execute(f"INSERT INTO {t} (N) VALUES (5)")
    with pytest.raises(ValueError, match="cannot be converted"):
        ml.ensure_target_table(con, "E3", [{"name": "N", "data_type": "date"}])


def test_changed_file_replaces_its_older_rows(tmp_path):
    src = tmp_path / "src"
    cols = ["Policy No", "Insured Name", "Region", "Gross Premium", "Inception Date", "Units", "Currency"]
    row = lambda p: [p, "acme", "North", 100, datetime.datetime(2024, 1, 5), 1, "GBP"]
    make_book(src / "a.xlsx", cols, [row("P1"), row("P2")]) if (src.mkdir() or True) else None
    con = db(tmp_path)
    did, _ = df.store_dataflow(con, df.parse_model_json(json.dumps(T.MODEL)))
    run_profile(con, LocalSource(src), "local", str(src))
    T.map_inputs(con, did)
    assert ml.load_entity(con, did, "Policies")["rows"] == 2
    make_book(src / "a.xlsx", cols, [row("P1"), row("P2"), row("P3")])            # same location, new content
    run_profile(con, LocalSource(src), "local", str(src))
    r = ml.load_entity(con, did, "Policies")
    assert r["rows"] == 3 and r["replaced_rows"] == 2
    assert sorted(x[0] for x in con.execute("select PolicyNumber from df_policies").fetchall()) == ["P1", "P2", "P3"]
    assert con.execute("select count(distinct _source_id) from df_policies").fetchone()[0] == 1
    assert con.execute("select count(*) from load_log where status = 'ok' and entity = 'Policies'").fetchone()[0] == 1


def test_reimported_dataflow_inherits_bindings_and_settings(tmp_path):
    con = db(tmp_path)
    m1 = json.loads(json.dumps(T.MODEL))
    did1, _ = df.store_dataflow(con, df.parse_model_json(json.dumps(m1)))
    con.execute("CREATE TABLE ref_fx (code VARCHAR)")
    con.execute("INSERT INTO query_bindings (dataflow_id, query_name, table_name) VALUES (?, 'Currencies', 'ref_fx')", [did1])
    tf.save_settings(con, did1, "Policies", True, "", True, [], "MDY")
    m2 = json.loads(json.dumps(T.MODEL))
    m2["entities"][1]["attributes"].append({"name": "Extra", "dataType": "string"})        # new version of the same dataflow
    p2 = df.parse_model_json(json.dumps(m2))
    did2, new = df.store_dataflow(con, p2)
    assert new and did2 != did1 and p2["carried"] == {"from": did1, "bindings": 1, "settings": 1}
    assert tf.bindings(con, did2) == {"Currencies": "ref_fx"}
    st = tf.settings(con, did2, "Policies")
    assert st["accept_partial"] and st["date_order"] == "MDY"
    assert tf.settings(con, did1, "Policies") is not None                            # the old version keeps its own copy
    assert {r[0] for r in con.execute("select dataflow_id from entity_transforms").fetchall()} == {did1, did2}


# ------------------------------------------------------------------ Windows paths
def test_file_list_source_across_drives(tmp_path, monkeypatch):
    a, b = tmp_path / "one" / "a.xlsx", tmp_path / "two" / "b.xlsx"
    book(a)
    book(b)

    def boom(paths):
        raise ValueError("Paths don't have the same drive")
    monkeypatch.setattr(os.path, "commonpath", boom)
    src = FileListSource([a, b])
    assert src.root is None
    rels = [f.rel_path for f in src.iter_files()]
    assert all(":" not in r and r.endswith(".xlsx") for r in rels) and len(set(rels)) == 2


# ------------------------------------------------------------------ old databases are upgraded
def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE runs (run_id VARCHAR PRIMARY KEY, started_utc TIMESTAMPTZ, tool_version VARCHAR, source_type VARCHAR, "
                "root VARCHAR, site_url VARCHAR, library VARCHAR, folder VARCHAR, params JSON, summary JSON)")
    con.execute("CREATE TABLE sheets (run_id VARCHAR, rel_path VARCHAR, sheet VARCHAR, state VARCHAR, max_row INTEGER, max_col INTEGER, "
                "header_row INTEGER, data_rows INTEGER, header_count INTEGER, layout VARCHAR, layout_hash VARCHAR, set_hash VARCHAR)")
    con.execute("CREATE TABLE sheet_headers (run_id VARCHAR, rel_path VARCHAR, sheet VARCHAR, layout VARCHAR, position INTEGER, "
                "raw_header VARCHAR, norm_header VARCHAR)")
    con.execute("INSERT INTO sheets VALUES ('r', 'a.xlsx', 'S', 'visible', 5, 3, 1, 4, 3, 'L01', 'hash1', 'set1')")
    con.execute("INSERT INTO sheet_headers VALUES ('r', 'a.xlsx', 'S', 'L01', 1, 'Policy No', 'policy no')")
    con.close()
    con = open_db(str(path))
    cols = {r[0] for r in con.execute("select column_name from information_schema.columns where table_name = 'sheet_headers'").fetchall()}
    assert "layout_hash" in cols
    assert con.execute("select layout_hash from sheet_headers").fetchone()[0] == "hash1"          # back-filled
    assert "status" in {r[0] for r in con.execute("select column_name from information_schema.columns where table_name = 'runs'").fetchall()}
