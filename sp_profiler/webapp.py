"""Local browser UI: pick folders/files, import a Gen1 dataflow, map columns, append to DuckDB, query, profile.

Binds to 127.0.0.1 only. Every API call needs a per-run random token (embedded in the page that
``/?t=<token>`` serves) and a localhost Host header, which blocks other websites from driving the
server through your browser (CSRF / DNS rebinding).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import traceback
import uuid
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import dataflow as df
from . import mapping_load as ml
from . import pipeline as pl
from . import query as q
from . import transforms as tf
from .runner import coverholder_resolver, run_profile
from .sources import DEFAULT_CLIENT_ID, BrowserAuth, FileListSource, GraphSource, LocalSource
from .store import open_db

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 64 * 1024 * 1024           # largest request body accepted (dataflow JSON uploads are far smaller)


class Jobs:
    KEEP_FINISHED = 30            # finished jobs kept for polling; older ones are dropped

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def _prune(self) -> None:
        finished = [j for j in self.jobs.values() if j["status"] != "running"]
        for j in sorted(finished, key=lambda j: j["finished"])[:-self.KEEP_FINISHED]:
            self.jobs.pop(j["id"], None)

    def start(self, kind: str, fn) -> str:
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, "status": "running", "done": 0, "total": 0, "message": "", "result": None,
               "error": "", "finished": 0.0}
        with self.lock:
            self._prune()
            self.jobs[jid] = job

        def run():
            try:
                job["result"] = fn(job)
                job["status"] = "done"
            except Exception as e:
                job["status"] = "error"
                job["error"] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            finally:
                job["finished"] = time.time()

        threading.Thread(target=run, daemon=True).start()
        return jid


class App:
    def __init__(self, db_path: str, home: str | None = None):
        self.db_path = str(Path(db_path).resolve())
        self.con = open_db(self.db_path)
        self.jobs = Jobs()
        self.token = secrets.token_urlsafe(24)           # API/session token (never appears in a URL)
        self.launch_token = secrets.token_urlsafe(24)    # printed link; valid for one page load only
        self.launch_used = False
        self.home = Path(home or Path.home())

    def cur(self):
        return self.con.cursor()

    # ---- filesystem picker -------------------------------------------------
    def fs_list(self, path: str, show_all: bool) -> dict:
        p = Path(path).expanduser() if path else self.home
        p = p.resolve()
        if not p.is_dir():
            raise NotADirectoryError(str(p))
        entries = []
        try:
            for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if e.name.startswith(".") and not show_all:
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                is_dir = e.is_dir()
                if not is_dir and not show_all and e.suffix.lower() not in (
                        ".xlsx", ".xlsm", ".xltx", ".xltm", ".xls", ".xlsb", ".json"):
                    continue
                entries.append({"name": e.name, "dir": is_dir, "size": None if is_dir else st.st_size,
                                "modified": int(st.st_mtime)})
        except PermissionError:
            raise PermissionError(f"Cannot read {p}")
        roots = [str(self.home)] + ([f"{c}:\\" for c in "CDEFGH" if Path(f"{c}:\\").exists()] if os.name == "nt" else ["/"])
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "entries": entries, "roots": roots}

    # ---- jobs ---------------------------------------------------------------
    def start_profile(self, body: dict) -> str:
        files, folder = body.get("files") or [], body.get("folder")
        sp = body.get("sharepoint")

        def work(job):
            cur = self.cur()
            if sp:
                src = GraphSource(sp["site_url"], sp.get("folder", ""), sp.get("library", "Documents"),
                                  BrowserAuth(sp.get("tenant_id") or "organizations", sp.get("client_id") or DEFAULT_CLIENT_ID))
                root, extra, st = f"{sp['site_url']}/{sp.get('library', 'Documents')}", {
                    "site_url": sp["site_url"], "library": sp.get("library", "Documents"), "folder": sp.get("folder", "")}, "graph"
            elif files:
                src = FileListSource(files)
                root, extra, st = str(src.root or "(several drives)"), {}, "local"
            elif folder:
                src = LocalSource(folder)
                root, extra, st = str(src.root), {}, "local"
            else:
                raise ValueError("Choose a folder or files")

            def prog(n, path):
                job["done"], job["message"] = n, path

            root_name = (Path(folder).name if folder else (sp.get("folder", "").strip("/").split("/")[-1] or sp.get("library", "Documents"))
                         if sp else "")
            mode = "name" if body.get("coverholder") else body.get("coverholder_mode", "folder")
            out = run_profile(cur, src, st, root, extra, excel_only=True, store_content=body.get("store_content", True),
                              max_content_mb=int(body.get("max_content_mb", 200)), blob_dir=body.get("blob_dir") or None,
                              workers=int(body.get("workers") or (4 if sp else 1)),
                              include_hidden=bool(body.get("include_hidden")), exact_rows=bool(body.get("exact_rows")),
                              refresh=bool(body.get("refresh")), progress=prog,
                              coverholder_of=coverholder_resolver(mode, body.get("coverholder", ""), root_name))
            res = out["result"]
            keys = ("total_files", "inspected_files", "status_counts", "distinct_layouts", "layout_families",
                    "duplicate_files", "reused_files", "files_with_warnings")
            return {"run_id": out["run_id"], "summary": {k: res["summary"].get(k) for k in keys}}
        return self.jobs.start("profile", work)

    def start_pipeline_run(self, body: dict) -> str:
        def work(job):
            def prog(n, total, msg):
                job["done"], job["total"], job["message"] = n, total, msg
            return pl.run_pipeline(self.cur(), body["pipeline_id"], body.get("coverholders") or None, bool(body.get("force")),
                                   bool(body.get("allow_partial_merge")), prog)
        return self.jobs.start("pipeline", work)

    def start_load(self, body: dict) -> str:
        def work(job):
            def prog(n, total, msg):
                job["done"], job["total"], job["message"] = n, total, msg
            return ml.load_entity(self.cur(), body["dataflow_id"], body["entity"], body.get("layout_hashes") or None,
                                  bool(body.get("force")), prog)
        return self.jobs.start("load", work)

    # ---- data access --------------------------------------------------------
    def layouts(self) -> list[dict]:
        rows = self.cur().execute("SELECT layout_hash, files, sheets, example, headers, norm_headers, coverholders FROM v_layouts "
                                  "ORDER BY files DESC, layout_hash").fetchall()
        return [{"layout_hash": h, "files": n, "sheets": s, "example": ex, "headers": hd, "norm_headers": nh,
                 "coverholders": ch or []} for h, n, s, ex, hd, nh, ch in rows]

    def dataflows(self) -> list[dict]:
        cur = self.cur()
        out = []
        for did, name, imported, src in cur.execute(
                "SELECT dataflow_id, name, imported_utc::VARCHAR, source_file FROM dataflows ORDER BY imported_utc DESC").fetchall():
            ents = []
            for ent, desc, mq, parts in cur.execute(
                    "SELECT entity, description, m_query, partitions FROM dataflow_entities WHERE dataflow_id = ?", [did]).fetchall():
                attrs = [{"name": a, "type": t, "description": d} for a, t, d in cur.execute(
                    "SELECT name, data_type, description FROM dataflow_attributes WHERE dataflow_id = ? AND entity = ? "
                    "ORDER BY position", [did, ent]).fetchall()]
                ents.append({"name": ent, "description": desc, "m_query": mq, "partitions": parts, "attributes": attrs,
                             "table": ml.table_for(cur, ent),
                             "mapped_layouts": cur.execute("SELECT count(DISTINCT layout_hash) FROM column_mappings WHERE entity = ?", [ent]).fetchone()[0]})
            out.append({"dataflow_id": did, "name": name, "imported": imported, "source_file": src, "entities": ents})
        return out

    def mapping_targets(self, cur, dataflow_id: str, entity: str) -> tuple[str, list[dict], dict]:
        attrs = [{"name": a, "type": t} for a, t in cur.execute(
            "SELECT name, data_type FROM dataflow_attributes WHERE dataflow_id = ? AND entity = ? ORDER BY position",
            [dataflow_id, entity]).fetchall()]
        if not attrs:
            raise LookupError("Unknown dataflow/entity")
        plan = tf.resolve(cur, dataflow_id, entity)
        if plan["mode"] == "m":
            return "input", [{"name": n, "type": "input column"} for n in plan["inputs"]], plan
        return "attribute", attrs, plan

    def stage_use(self, cur, dataflow_id: str, entity: str):
        """If this dataflow entity is a pipeline stage that reads another stage's output, describe that input."""
        r = cur.execute("SELECT s.pipeline_id, p.name, s.position, s.name, s.input_stage FROM pipeline_stages s "
                        "JOIN pipelines p USING (pipeline_id) WHERE s.dataflow_id = ? AND s.entity = ? AND s.kind <> 'files' LIMIT 1",
                        [dataflow_id, entity]).fetchone()
        if not r:
            plan = tf.resolve(cur, dataflow_id, entity)
            if plan["pipeline"].source_kinds == {"dataflow"}:       # reads a linked dataflow: no sheet columns to map
                return {"pipeline": None, "stage": None, "upstream_stage": None, "upstream_table": None,
                        "needs": plan["pipeline"].inputs, "available": [], "missing": []}
            return None
        pipe = pl.get_pipeline(cur, r[0])
        up = pipe["stages"][r[4]]
        plan = tf.resolve(cur, dataflow_id, entity)
        have = [c[0] for c in cur.execute(f"SELECT column_name FROM (DESCRIBE \"{up['output_table']}\")").fetchall()] \
            if cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [up["output_table"]]).fetchone() else []
        need = plan["pipeline"].inputs
        return {"pipeline": r[1], "stage": r[3], "upstream_stage": up["name"], "upstream_table": up["output_table"],
                "needs": need, "available": [c for c in have if not c.startswith("_")],
                "missing": [c for c in need if c.lower() not in {h.lower() for h in have}] if have else []}

    def mapping(self, dataflow_id: str, entity: str) -> dict:
        cur = self.cur()
        stage = self.stage_use(cur, dataflow_id, entity)
        if stage:
            return {"attributes": [], "layouts": [], "table": stage["upstream_table"], "kind": "stage", "mode": "stage", "stage": stage}
        kind, targets, plan = self.mapping_targets(cur, dataflow_id, entity)
        saved: dict[str, dict] = {}
        for lh, nh, a in cur.execute("SELECT layout_hash, norm_header, attribute FROM column_mappings "
                                     "WHERE entity = ? AND kind = ?", [entity, kind]).fetchall():
            saved.setdefault(lh, {})[nh] = a
        pending = {w["layout_hash"] for w in ml.pending_sheets(cur, entity, kind=kind)}
        ignored = {r[0] for r in cur.execute("SELECT layout_hash FROM layout_ignores WHERE entity = ?", [entity]).fetchall()}
        out = []
        for lay in self.layouts():
            sugg = df.suggest_mapping(lay["norm_headers"], [a["name"] for a in targets])
            lay["saved"] = saved.get(lay["layout_hash"], {})
            lay["suggested"] = {h: s for h, s in sugg.items() if h not in lay["saved"]}
            lay["pending"] = lay["layout_hash"] in pending
            lay["ignored"] = lay["layout_hash"] in ignored
            out.append(lay)
        return {"attributes": targets, "layouts": out, "table": ml.table_for(cur, entity), "kind": kind,
                "mode": plan["mode"]}

    def transform(self, dataflow_id: str, entity: str) -> dict:
        cur = self.cur()
        plan = tf.resolve(cur, dataflow_id, entity)
        p, st = plan["pipeline"], plan["settings"] or {}
        attrs = [r[0] for r in cur.execute("SELECT name FROM dataflow_attributes WHERE dataflow_id = ? AND entity = ? ORDER BY position",
                                           [dataflow_id, entity]).fetchall()]
        info = {"steps": [{k: s.get(k, "") for k in ("query", "name", "func", "ok", "error", "cte")} for s in p.steps],
                "inputs": p.inputs, "references": p.references, "translated": p.complete, "error": p.error,
                "generated_sql": plan["generated_sql"], "status": plan["status"], "mode": plan["mode"],
                "use_m": plan["use_m"], "explicit": bool(plan["settings"]), "override_sql": st.get("override_sql", ""),
                "accept_partial": st.get("accept_partial", False), "extra_inputs": st.get("extra_inputs", []),
                "has_m": bool(p.steps) or bool(p.error), "bindings": tf.bindings(cur, dataflow_id),
                "coverholder_bindings": tf.coverholder_bindings(cur, dataflow_id),
                "date_order": plan["date_order"], "culture": plan["culture"], "date_order_source": plan["date_order_source"],
                "date_order_setting": st.get("date_order", "auto"), "outputs": [], "missing_attributes": [], "sql_error": ""}
        if plan["sql"]:
            try:
                cols = tf.output_columns(cur, plan)
                info["outputs"] = [{"name": n, "type": t} for n, t in cols if n != "__row"]
                have = {n.lower() for n, _ in cols}
                info["missing_attributes"] = [a for a in attrs if a.lower() not in have]
            except Exception as e:
                info["sql_error"] = f"{type(e).__name__}: {e}"
        return info

    def transform_preview(self, b: dict) -> dict:
        cur = self.cur()
        plan = tf.resolve(cur, b["dataflow_id"], b["entity"])
        p = plan["pipeline"]
        step = b.get("step") or ""
        if step:
            rec = next((s for s in p.steps if s["name"] == step and s["ok"]), None)
            if not rec:
                raise LookupError(f"Step {step!r} is not translated")
            sql = p.sql(final=rec["cte"], limit=int(b.get("limit", 50)))
            inputs = list(dict.fromkeys(p.inputs + (plan["settings"] or {}).get("extra_inputs", [])))
            plan = {**plan, "inputs": inputs}
        elif plan["sql"]:
            sql = f"SELECT * FROM ({plan['sql']}) LIMIT {int(b.get('limit', 50))}"
        else:
            raise ValueError(f"Nothing to preview: {plan['status']}")
        from .m2sql import register_udfs
        register_udfs(cur)
        n = tf.stage_sample(cur, plan, b["entity"], b["layout_hash"])
        res = q.run_sql(cur, sql, limit=int(b.get("limit", 50)))
        res["staged_rows"] = n
        return res


def make_handler(app: App, port: int):
    index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        server_version = "sp-profile"

        def log_message(self, fmt, *a):
            pass

        def _send(self, code: int, body: bytes, ctype="application/json", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'; "
                                                        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode())

        def _check(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, b"bad host", "text/plain")
                return False
            return True

        def _authed(self) -> bool:
            if secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                return True
            self._json({"error": "unauthorized"}, 401)
            return False

        def do_GET(self):
            if not self._check():
                return
            u = urlparse(self.path)
            if u.path == "/":
                launch = parse_qs(u.query).get("t", [""])[0]
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                has_session = "sp_session" in cookie and secrets.compare_digest(cookie["sp_session"].value, app.token)
                fresh = bool(launch) and not app.launch_used and secrets.compare_digest(launch, app.launch_token)
                if not (fresh or has_session):
                    return self._send(403, b"This link was already used. Restart sp-profile serve to get a new one.", "text/plain")
                headers = {}
                if fresh:
                    app.launch_used = True          # the URL in the browser history is dead from now on
                    headers["Set-Cookie"] = f"sp_session={app.token}; HttpOnly; SameSite=Strict; Path=/"
                return self._send(200, index.replace("__TOKEN__", app.token).encode(), "text/html; charset=utf-8", headers)
            if not u.path.startswith("/api/"):
                return self._send(404, b"not found", "text/plain")
            if not self._authed():
                return
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                self._json(self.route_get(u.path, qs))
            except Exception as e:
                self._error(e)

        def do_POST(self):
            if not self._check() or not self._authed():
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > MAX_BODY:
                    return self._json({"error": f"Request too large (limit {MAX_BODY // 1048576} MB)"}, 413)
                body = json.loads(self.rfile.read(n) or b"{}")
                u = urlparse(self.path)
                if u.path == "/api/sql_csv":
                    chunks = q.csv_chunks(app.cur(), body["sql"], bool(body.get("allow_write")))   # same checks as the console
                    self.send_response(200)
                    for k, v in (("Content-Type", "text/csv; charset=utf-8"), ("Content-Disposition", 'attachment; filename="query.csv"'),
                                 ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Connection", "close")):
                        self.send_header(k, v)
                    self.end_headers()
                    self.close_connection = True
                    for chunk in chunks:
                        self.wfile.write(chunk.encode("utf-8"))
                    return
                self._json(self.route_post(u.path, body))
            except Exception as e:
                self._error(e)

        def _error(self, e):
            code = 403 if isinstance(e, PermissionError) else 404 if isinstance(e, (LookupError, FileNotFoundError)) else 400
            if code == 400 and not isinstance(e, (ValueError, NotADirectoryError)):
                traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, code)

        # ---- routes -----------------------------------------------------
        def route_get(self, path, qs):
            cur = app.cur()
            if path == "/api/state":
                one = lambda s: cur.execute(s).fetchone()[0]
                return {"db": app.db_path, "files": one("SELECT count(*) FROM v_files"), "layouts": one("SELECT count(*) FROM v_layouts"),
                        "dataflows": one("SELECT count(*) FROM dataflows"), "home": str(app.home)}
            if path == "/api/fs":
                return app.fs_list(qs.get("path", ""), qs.get("all") == "1")
            if path == "/api/dataflows":
                return app.dataflows()
            if path == "/api/layouts":
                return app.layouts()
            if path == "/api/mapping":
                return app.mapping(qs["dataflow_id"], qs["entity"])
            if path == "/api/transform":
                return app.transform(qs["dataflow_id"], qs["entity"])
            if path == "/api/schema":
                return q.schema(cur)
            if path == "/api/coverholders":
                return pl.coverholders(cur)
            if path == "/api/pipelines":
                return pl.list_pipelines(cur)
            if path == "/api/pipelines/status":
                return pl.status_matrix(cur, qs["pipeline_id"])
            if path == "/api/files/stored":
                like = f"%{qs.get('search', '').lower()}%"
                rows = cur.execute("SELECT name, rel_path, coverholder, sha256, size_bytes FROM v_files WHERE content_stored "
                                   "AND lower(extension) IN ('.xlsx', '.xlsm') AND lower(rel_path) LIKE ? ORDER BY rel_path LIMIT 200",
                                   [like]).fetchall()
                return [dict(zip(["name", "rel_path", "coverholder", "sha256", "size"], r)) for r in rows]
            if path == "/api/loads":
                rows = cur.execute("SELECT load_id, entity, source_path, sheet, rows_loaded, rows_skipped_empty, coerce_error_cells, "
                                   "status, error, finished_utc::VARCHAR FROM load_log ORDER BY finished_utc DESC LIMIT 200").fetchall()
                keys = ["load_id", "entity", "source_path", "sheet", "rows_loaded", "rows_skipped_empty", "coerce_error_cells", "status", "error", "finished"]
                return [dict(zip(keys, r)) for r in rows]
            if path.startswith("/api/jobs/"):
                job = app.jobs.jobs.get(path.rsplit("/", 1)[1])
                if not job:
                    raise LookupError("no such job")
                return job
            raise LookupError(path)

        def route_post(self, path, b):
            cur = app.cur()
            if path == "/api/profile/start":
                return {"job": app.start_profile(b)}
            if path == "/api/load/start":
                return {"job": app.start_load(b)}
            if path == "/api/dataflow/import":
                if b.get("path"):
                    p = Path(b["path"]).expanduser()
                    parsed, src = df.parse_model_json(p.read_bytes()), str(p)
                else:
                    parsed, src = df.parse_model_json(b["content"]), b.get("filename", "upload")
                did, new = df.store_dataflow(cur, parsed, src)
                return {"dataflow_id": did, "new": new, "name": parsed["name"], "carried": parsed.get("carried"),
                        "warnings": df.attribute_problems_of(parsed),
                        "entities": [{"name": e["name"], "attributes": len(e["attributes"])} for e in parsed["entities"]]}
            if path == "/api/mapping/save":
                kind, targets, _ = app.mapping_targets(cur, b["dataflow_id"], b["entity"])
                return {"saved": ml.save_mapping(cur, b["entity"], b["layout_hash"], b["pairs"],
                                                 [t["name"] for t in targets], kind)}
            if path == "/api/transform/save":
                sql = (b.get("override_sql") or "").strip()
                if sql:
                    stmts = cur.extract_statements(sql)
                    if len(stmts) != 1 or stmts[0].type != q.duckdb.StatementType.SELECT:
                        raise ValueError("The SQL override must be a single SELECT/WITH statement reading the staged table `_stg` (or CTE `src`)")
                tf.save_settings(cur, b["dataflow_id"], b["entity"], bool(b.get("use_m")), sql,
                                 bool(b.get("accept_partial")), [x.strip() for x in b.get("extra_inputs", []) if x.strip()],
                                 b.get("date_order", "auto"))
                return app.transform(b["dataflow_id"], b["entity"])
            if path == "/api/transform/preview":
                return app.transform_preview(b)
            if path == "/api/bindings/save":
                ch = b.get("coverholder") or ""
                cur.execute("DELETE FROM query_bindings WHERE dataflow_id = ? AND query_name = ? AND coverholder = ?",
                            [b["dataflow_id"], b["query_name"], ch])
                if b.get("table_name"):
                    if not cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [b["table_name"]]).fetchone():
                        raise LookupError(f"Table {b['table_name']!r} does not exist")
                    cur.execute("INSERT INTO query_bindings (dataflow_id, query_name, table_name, coverholder) VALUES (?, ?, ?, ?)",
                                [b["dataflow_id"], b["query_name"], b["table_name"], ch])
                return {"ok": True}
            if path == "/api/export/databricks":
                from .export import export_databricks
                if not b.get("out"):
                    raise ValueError("Choose an output folder")
                m = export_databricks(app.con.cursor(), b["out"], b.get("tables") or None, bool(b.get("incremental")),
                                      bool(b.get("include_blobs")), b.get("prefix", ""), b.get("catalog", "main"),
                                      b.get("schema", "bordereaux"), b.get("volume_path") or "/Volumes/<catalog>/<schema>/<volume>/sp_profiler")
                return {k: m[k] for k in ("export_id", "mode", "path", "skipped")} | {
                    "tables": [{"name": x["name"], "kind": x["kind"], "rows": x["rows"], "files": len(x["files"])} for x in m["tables"]]}
            if path == "/api/pipelines/save":
                pid = pl.save_pipeline(cur, b["name"], b["stages"], b.get("overrides"), b.get("pipeline_id") or None)
                return pl.get_pipeline(cur, pid)
            if path == "/api/pipelines/delete":
                pl.delete_pipeline(cur, b["pipeline_id"])
                return {"ok": True}
            if path == "/api/pipelines/run":
                return {"job": app.start_pipeline_run(b)}
            if path == "/api/mapping/ignore":
                ml.set_layout_ignored(cur, b["entity"], b["layout_hash"], bool(b.get("ignored")))
                return {"ok": True}
            if path == "/api/reference/import-stored":
                return tf.import_reference_stored(cur, b["sha256"], b.get("table_name", ""), b.get("sheet", ""))
            if path == "/api/reference/import":
                return tf.import_reference(cur, b["path"], b.get("table_name", ""), b.get("sheet", ""))
            if path == "/api/sql":
                return q.run_sql(cur, b["sql"], int(b.get("limit", 1000)), bool(b.get("allow_write")))
            if path == "/api/profile-data":
                sql = b.get("sql") or f'SELECT * FROM "{b["table"].replace(chr(34), chr(34) * 2)}"'
                return q.profile_query(cur, sql)
            raise LookupError(path)

    return H


def serve(db_path: str, port: int = 8765, open_browser: bool = True) -> None:
    app = App(db_path)
    server, last = None, None
    for p in range(port, port + 20):                     # first free port at or after the requested one
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app, p))
            port = p
            break
        except OSError as e:
            last = e
    if server is None:
        raise SystemExit(f"No free port between {port} and {port + 19}: {last}")
    url = f"http://127.0.0.1:{port}/?t={app.launch_token}"
    print(f"sp-profile UI  ->  {url}\nDatabase: {app.db_path}\nLocal only (127.0.0.1). The link works once; "
          f"Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.con.close()
