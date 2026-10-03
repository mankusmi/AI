import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

import openpyxl
import pytest

from sp_profiler import dataflow as df
from sp_profiler import mapping_load as ml
from sp_profiler import query as q
from sp_profiler.cli import main
from sp_profiler.store import open_db
from sp_profiler.webapp import App, make_handler

MODEL = {
    "name": "Bordereau", "version": "1.0", "culture": "en-US", "modifiedTime": "2024-01-01T00:00:00Z",
    "pbi:mashup": {"document": 'section Section1;\r\nshared Policies = let\r\n  Source = 1\r\nin\r\n  Source;\r\nshared #"Other Q" = let x = 2 in x;'},
    "entities": [{
        "$type": "LocalEntity", "name": "Policies", "partitions": [{"name": "p"}],
        "attributes": [
            {"name": "PolicyNumber", "dataType": "string"}, {"name": "Insured", "dataType": "string"},
            {"name": "InceptionDate", "dataType": "dateTime"}, {"name": "GrossPremium", "dataType": "decimal"},
            {"name": "Units", "dataType": "int64"}, {"name": "Active", "dataType": "boolean"}]}]}


def make_book(path: Path, headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Bordereau title"])
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)


@pytest.fixture
def env(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_book(src / "a.xlsx", ["Policy No", "Insured Name", "Inception Date", "Gross Premium", "Units", "Active"],
              [["P1", "Acme", datetime(2024, 1, 5), 100.5, 3, "Yes"], ["P2", "Beta", "06/02/2024", "1,200.00", "x", "no"],
               [None, None, None, None, None, None]])
    make_book(src / "b.xlsx", ["PolicyNumber", "Insured", "Premium (Gross)"], [["P3", "Gamma", 50]])
    db = tmp_path / "t.duckdb"
    assert main(["local", "--path", str(src), "--db", str(db)]) == 0
    con = open_db(str(db))
    did, new = df.store_dataflow(con, df.parse_model_json(json.dumps(MODEL)))
    assert new
    return con, did, src


def test_parse_model_json_and_m_queries():
    p = df.parse_model_json(json.dumps(MODEL).encode())
    e = p["entities"][0]
    assert e["name"] == "Policies" and len(e["attributes"]) == 6 and e["partitions"] == 1
    assert "Source = 1" in e["m_query"]
    assert df.extract_m_queries(MODEL["pbi:mashup"]["document"]).keys() == {"Policies", "Other Q"}
    with pytest.raises(ValueError):
        df.parse_model_json("[1,2]")
    with pytest.raises(ValueError):
        df.parse_model_json("not json")


def test_suggest_mapping():
    s = df.suggest_mapping(["policy no", "insured name", "gross premium", "junk"],
                           ["PolicyNumber", "Insured", "GrossPremium", "Units"])
    assert s["policy no"]["attribute"] == "PolicyNumber"
    assert s["gross premium"] == {"attribute": "GrossPremium", "score": 1.0}
    assert "junk" not in s


def test_coerce():
    assert ml.coerce("1,200.50", "decimal") == (Decimal("1200.50"), True)
    assert ml.coerce("(5)", "int64") == (-5, True)
    assert ml.coerce("x", "int64") == (None, False)
    assert ml.coerce(2.5, "int64") == (None, False)
    assert ml.coerce("06/02/2024", "dateTime")[0] == datetime(2024, 2, 6)
    assert ml.coerce(45000, "date")[0] == date(2023, 3, 15)
    assert ml.coerce("Yes", "boolean") == (True, True)
    assert ml.coerce(12345.0, "string") == ("12345", True)
    assert ml.coerce("  ", "int64") == (None, True)


def test_map_load_append_and_idempotent(env):
    con, did, _ = env
    attrs = [r[0] for r in con.execute("select name from dataflow_attributes").fetchall()]
    layouts = {tuple(r[1]): r[0] for r in con.execute("select layout_hash, headers from layouts").fetchall()}
    assert len(layouts) == 2
    for lay in con.execute("select layout_hash, norm_headers from layouts").fetchall():
        sugg = df.suggest_mapping(lay[1], attrs)
        ml.save_mapping(con, "Policies", lay[0], [{"norm_header": h, "attribute": s["attribute"]} for h, s in sugg.items()], attrs)
    with pytest.raises(ValueError):
        ml.save_mapping(con, "Policies", "x", [{"norm_header": "a", "attribute": "Nope"}], attrs)
    with pytest.raises(ValueError):
        ml.save_mapping(con, "Policies", "x", [{"norm_header": "a", "attribute": "Insured"}, {"norm_header": "b", "attribute": "Insured"}], attrs)
    r = ml.load_entity(con, did, "Policies")
    assert r["rows"] == 3 and r["sheets_loaded"] == 2 and r["sheets_failed"] == 0
    rows = con.execute("select PolicyNumber, InceptionDate, GrossPremium, Units, Active, _coerce_errors, _excel_row "
                       "from df_policies order by PolicyNumber").fetchall()
    assert rows[0][0] == "P1" and rows[0][2] == Decimal("100.5000000000") and rows[0][4] is True
    assert rows[1][3] is None and rows[1][5] == "Units" and rows[1][1] == datetime(2024, 6, 2)   # en-US culture: month first
    assert rows[2][2] == Decimal(50)                       # b.xlsx 'Premium (Gross)' fuzzy-mapped
    again = ml.load_entity(con, did, "Policies")
    assert again["rows"] == 0 and again["sheets_planned"] == 0   # idempotent
    forced = ml.load_entity(con, did, "Policies", force=True)
    assert forced["rows"] == 3 and forced["replaced_rows"] == 3
    assert con.execute("select count(*) from df_policies").fetchone()[0] == 3   # replaced, not duplicated
    assert con.execute("select count(*) from load_log where status = 'superseded'").fetchone()[0] == 2


def test_sql_guard_and_profile(env):
    con, did, _ = env
    cur = con.cursor()
    assert q.run_sql(cur, "select 1 as a, 'x' as b")["rows"] == [[1, "x"]]
    assert q.run_sql(cur, "describe select 1")["columns"]
    for bad in ["drop table files", "select 1; select 2", "copy (select 1) to '/tmp/z.csv'", "attach ':memory:' as m"]:
        with pytest.raises((PermissionError, ValueError)):
            q.run_sql(cur, bad)
    q.run_sql(cur, "create table scratch as select 1 x", allow_write=True)
    assert q.run_sql(cur, "select content from file_blobs limit 1")["rows"][0][0].startswith("<BLOB")
    p = q.profile_query(cur, "select * from files")
    names = {c["name"]: c for c in p["columns"]}
    assert p["rows"] == 2 and names["extension"]["top"][0]["value"] == ".xlsx"
    assert names["size_bytes"]["median"] is not None and names["name"]["max_len"] >= 6
    assert q.schema(cur)


@pytest.fixture
def server(env, tmp_path):
    con, did, src = env
    con.close()      # App opens its own connection to the same file
    app = App(str(tmp_path / "t.duckdb"), home=str(src))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(app, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, port, src
    srv.shutdown()


def call(port, token, path, body=None, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Token": token, "Host": host or f"127.0.0.1:{port}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}") if e.headers.get("Content-Type", "").startswith("application/json") else {}


def test_server_auth_and_flow(server):
    app, port, src = server
    assert call(port, "wrong", "/api/state")[0] == 401
    assert call(port, app.token, "/api/state", host="evil.com")[0] == 403
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/?t={app.token}") as r:
        assert app.token in r.read().decode()
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/")
    assert ei.value.code == 403
    s, st = call(port, app.token, "/api/state")
    assert s == 200 and st["files"] == 2
    s, fs = call(port, app.token, f"/api/fs?path={src}")
    assert {e["name"] for e in fs["entries"]} >= {"a.xlsx", "b.xlsx"}
    s, d = call(port, app.token, "/api/dataflows")
    did = d[0]["dataflow_id"]
    s, m = call(port, app.token, f"/api/mapping?dataflow_id={did}&entity=Policies")
    assert s == 200 and len(m["layouts"]) == 2 and m["layouts"][0]["suggested"]
    for lay in m["layouts"]:
        pairs = [{"norm_header": h, "attribute": v["attribute"]} for h, v in lay["suggested"].items()]
        assert call(port, app.token, "/api/mapping/save", {"dataflow_id": did, "entity": "Policies", "layout_hash": lay["layout_hash"], "pairs": pairs})[0] == 200
    s, j = call(port, app.token, "/api/load/start", {"dataflow_id": did, "entity": "Policies"})
    import time
    for _ in range(50):
        s, job = call(port, app.token, f"/api/jobs/{j['job']}")
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "done" and job["result"]["rows"] == 3
    s, r = call(port, app.token, "/api/sql", {"sql": "select count(*) from df_policies"})
    assert r["rows"] == [[3]]
    assert call(port, app.token, "/api/sql", {"sql": "delete from df_policies"})[0] == 403
    s, p = call(port, app.token, "/api/profile-data", {"table": "df_policies"})
    assert s == 200 and p["rows"] == 3
    s, j = call(port, app.token, "/api/profile/start", {"files": [str(src / "a.xlsx")]})
    for _ in range(50):
        s, job = call(port, app.token, f"/api/jobs/{j['job']}")
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "done", job
