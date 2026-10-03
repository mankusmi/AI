import json
import time
import urllib.error
import urllib.request

from .test_sp_ui import env, server  # noqa: F401  (fixtures)


def raw(port, token, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"X-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_csv_download_is_streamed_and_guarded(server):  # noqa: F811
    app, port, _ = server
    s, body = raw(port, app.token, "/api/sql_csv", {"sql": "select 1 as a, 'x,y' as b"})
    assert s == 200 and body.decode("utf-8-sig").splitlines() == ["a,b", '1,"x,y"']
    # the download used to run any SQL; it must obey the same rules as the console
    for bad in ["drop table files", "select * from read_csv('/etc/passwd')", 'select * from "read_csv"(\'x\')',
                "select * from 'secrets.csv'", "select * from query('select 1')", "select getenv('HOME')",
                "describe 'x.csv'", "select 1; select 2", "select * from glob('/*')"]:
        for path in ("/api/sql_csv", "/api/sql"):
            s, body = raw(port, app.token, path, {"sql": bad})
            assert s in (400, 403) and b"error" in body, (path, bad, s)


def test_guard_does_not_block_harmless_text_or_allowed_writes(server):  # noqa: F811
    app, port, _ = server
    harmless = ["select 'read_csv(' as s", "select 'x' -- read_csv('y')", "select count(*) from files where name like '%.csv'",
                "select \"name\" as glob from files limit 1"]
    for sql in harmless:
        assert raw(port, app.token, "/api/sql", {"sql": sql})[0] == 200, sql
    assert raw(port, app.token, "/api/sql", {"sql": "create table scratch2 as select 1 x", "allow_write": True})[0] == 200
    assert raw(port, app.token, "/api/profile-data", {"sql": "select * from read_csv('/etc/passwd')"})[0] in (400, 403)
    assert raw(port, app.token, "/api/profile-data", {"table": "files"})[0] == 200


def test_oversized_request_is_refused(server, monkeypatch):  # noqa: F811
    import sp_profiler.webapp as w
    app, port, _ = server
    monkeypatch.setattr(w, "MAX_BODY", 50)
    s, body = raw(port, app.token, "/api/sql", {"sql": "select " + "1," * 40 + "1"})
    assert s == 413 and b"too large" in body


def test_finished_jobs_are_pruned():
    from sp_profiler.webapp import Jobs
    jobs = Jobs()
    for _ in range(Jobs.KEEP_FINISHED + 15):
        jobs.start("t", lambda job: 1)
    for _ in range(200):
        if all(j["status"] != "running" for j in jobs.jobs.values()):
            break
        time.sleep(0.02)
    jobs.start("t", lambda job: 1)                      # starting a job prunes old finished ones
    assert len(jobs.jobs) <= Jobs.KEEP_FINISHED + 1


def test_serve_picks_next_free_port(tmp_path, monkeypatch, capsys):
    import socket
    import sp_profiler.webapp as w
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    started = {}
    real = w.ThreadingHTTPServer

    class Stop(real):
        def serve_forever(self, *a, **k):
            started["port"] = self.server_address[1]
            raise KeyboardInterrupt
    monkeypatch.setattr(w, "ThreadingHTTPServer", Stop)
    w.serve(str(tmp_path / "s.duckdb"), port, open_browser=False)
    busy.close()
    assert started["port"] != port and started["port"] > port
    assert "?t=" in capsys.readouterr().out
