"""Pipelines: coverholder files -> dataflow 1 -> dataflow 2 (with lookup files) -> ... -> final merge.

A pipeline is an ordered list of stages:

* ``files``  (first stage): applies a dataflow to the stored Excel sheets of each coverholder (layout mapping + Power Query
  steps), appending rows to the stage's output table.
* ``stage``: applies another dataflow to the previous stage's output *for one coverholder* (its source is that table, not a
  file); the coverholder's rows in this stage's output table are replaced.
* ``merge``: applies a dataflow to the previous stage's output for *all* coverholders and replaces the whole output table;
  runs once, after every coverholder has finished.

Work is only redone when something it depends on changed (new/changed files, a changed lookup file, a different dataflow or
setting), and a coverholder that fails or still needs mapping stops there without blocking the others.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from . import mapping_load as ml
from . import transforms as tf
from .bulk import bulk_insert, qi
from .m2sql import ensure_macros

PIPELINE_DDL = """
CREATE TABLE IF NOT EXISTS pipelines (
    pipeline_id VARCHAR PRIMARY KEY, name VARCHAR UNIQUE, created_utc TIMESTAMPTZ DEFAULT now(), updated_utc TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS pipeline_stages (
    pipeline_id VARCHAR, position INTEGER, name VARCHAR, kind VARCHAR, dataflow_id VARCHAR, entity VARCHAR,
    input_stage INTEGER, output_table VARCHAR, PRIMARY KEY (pipeline_id, position));
-- a coverholder may use a different dataflow/entity for a stage (the output table stays the stage's)
CREATE TABLE IF NOT EXISTS pipeline_overrides (
    pipeline_id VARCHAR, position INTEGER, coverholder VARCHAR, dataflow_id VARCHAR, entity VARCHAR,
    PRIMARY KEY (pipeline_id, position, coverholder));
CREATE TABLE IF NOT EXISTS pipeline_state (
    pipeline_id VARCHAR, position INTEGER, coverholder VARCHAR, fingerprint VARCHAR, version_utc TIMESTAMPTZ, rows BIGINT,
    PRIMARY KEY (pipeline_id, position, coverholder));
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id VARCHAR PRIMARY KEY, pipeline_id VARCHAR, started_utc TIMESTAMPTZ, finished_utc TIMESTAMPTZ, status VARCHAR,
    params JSON, summary JSON);
CREATE TABLE IF NOT EXISTS pipeline_run_steps (
    run_id VARCHAR, position INTEGER, stage_name VARCHAR, coverholder VARCHAR, status VARCHAR, rows BIGINT, detail VARCHAR,
    started_utc TIMESTAMPTZ, finished_utc TIMESTAMPTZ);
"""
KINDS = ("files", "stage", "merge")
OK_STATUSES = {"loaded", "up_to_date"}
LINEAGE = ["_source_path", "_source_sha256", "_sheet", "_excel_row", "_layout_hash", "_source_id", "_coverholder"]


class PipelineError(ValueError):
    pass


def slug(s: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", s or "").strip("_").lower() or "x"


# ----------------------------------------------------------------------------- definition
def _check_output_table(con, name: str, owned: set[str]) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise PipelineError(f"Output table name {name!r}: use letters, digits and underscores only")
    exists = con.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()
    if exists and name not in owned:
        cols = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?", [name]).fetchall()}
        if "_load_id" not in cols:
            raise PipelineError(f"Table {name!r} already exists and was not created by sp-profile; choose another output table name")


def save_pipeline(con, name: str, stages: list[dict], overrides: Optional[list[dict]] = None,
                  pipeline_id: Optional[str] = None) -> str:
    """Create or replace a pipeline definition (validated). Returns its id."""
    if not name.strip():
        raise PipelineError("Give the pipeline a name")
    if not stages:
        raise PipelineError("A pipeline needs at least one stage")
    kinds = [s.get("kind") for s in stages]
    if any(k not in KINDS for k in kinds):
        raise PipelineError(f"Stage kind must be one of {KINDS}")
    if kinds[0] != "files" or kinds.count("files") != 1:
        raise PipelineError("The first stage (and only the first) must be of kind 'files'")
    if "merge" in kinds and "stage" in kinds[kinds.index("merge"):]:
        raise PipelineError("Merge stages must come after all per-coverholder stages")
    pipeline_id = pipeline_id or uuid.uuid4().hex[:10]
    owned = {r[0] for r in con.execute("SELECT output_table FROM pipeline_stages WHERE pipeline_id = ?", [pipeline_id]).fetchall()}
    rows, seen_tables, seen_names = [], set(), set()
    for pos, s in enumerate(stages):
        dfid, ent = s.get("dataflow_id"), s.get("entity")
        if not con.execute("SELECT 1 FROM dataflow_attributes WHERE dataflow_id = ? AND entity = ?", [dfid, ent]).fetchone():
            raise PipelineError(f"Stage {pos + 1}: entity {ent!r} not found in the chosen dataflow")
        sname = (s.get("name") or "").strip() or f"{s['kind']} {pos + 1}"
        if sname.lower() in seen_names:
            raise PipelineError(f"Duplicate stage name {sname!r}")
        seen_names.add(sname.lower())
        out = (s.get("output_table") or "").strip()
        if not out:
            out = ml.table_for(con, ent) if s["kind"] == "files" else f"df_{slug(sname)}"
        _check_output_table(con, out, owned)
        if out in seen_tables:
            raise PipelineError(f"Two stages write to {out!r}")
        seen_tables.add(out)
        inp = s.get("input_stage")
        if pos == 0:
            inp = None
        else:
            inp = pos - 1 if inp in (None, "") else int(inp)
            if not 0 <= inp < pos:
                raise PipelineError(f"Stage {pos + 1}: input must be an earlier stage")
        rows.append([pipeline_id, pos, sname, s["kind"], dfid, ent, inp, out])
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM pipeline_stages WHERE pipeline_id = ?", [pipeline_id])
        con.execute("DELETE FROM pipeline_overrides WHERE pipeline_id = ?", [pipeline_id])
        if con.execute("SELECT 1 FROM pipelines WHERE pipeline_id = ?", [pipeline_id]).fetchone():
            con.execute("UPDATE pipelines SET name = ?, updated_utc = now() WHERE pipeline_id = ?", [name.strip(), pipeline_id])
        else:
            con.execute("INSERT INTO pipelines (pipeline_id, name) VALUES (?, ?)", [pipeline_id, name.strip()])
        con.executemany("INSERT INTO pipeline_stages VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        for o in overrides or []:
            if not (o.get("coverholder") and o.get("dataflow_id") and o.get("entity")):
                continue
            if not con.execute("SELECT 1 FROM dataflow_attributes WHERE dataflow_id = ? AND entity = ?",
                               [o["dataflow_id"], o["entity"]]).fetchone():
                raise PipelineError(f"Override for {o['coverholder']!r}: entity {o['entity']!r} not found in that dataflow")
            con.execute("INSERT INTO pipeline_overrides VALUES (?, ?, ?, ?, ?)",
                        [pipeline_id, int(o["position"]), o["coverholder"], o["dataflow_id"], o["entity"]])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return pipeline_id


def get_pipeline(con, pipeline_id: str) -> dict:
    p = con.execute("SELECT pipeline_id, name FROM pipelines WHERE pipeline_id = ?", [pipeline_id]).fetchone()
    if not p:
        raise LookupError("No such pipeline")
    cols = ["position", "name", "kind", "dataflow_id", "entity", "input_stage", "output_table"]
    stages = [dict(zip(cols, r)) for r in con.execute(
        "SELECT position, name, kind, dataflow_id, entity, input_stage, output_table FROM pipeline_stages "
        "WHERE pipeline_id = ? ORDER BY position", [pipeline_id]).fetchall()]
    ovr = [dict(zip(["position", "coverholder", "dataflow_id", "entity"], r)) for r in con.execute(
        "SELECT position, coverholder, dataflow_id, entity FROM pipeline_overrides WHERE pipeline_id = ? ORDER BY position, coverholder",
        [pipeline_id]).fetchall()]
    return {"pipeline_id": p[0], "name": p[1], "stages": stages, "overrides": ovr}


def list_pipelines(con) -> list[dict]:
    return [get_pipeline(con, r[0]) for r in con.execute("SELECT pipeline_id FROM pipelines ORDER BY name").fetchall()]


def delete_pipeline(con, pipeline_id: str) -> None:
    for t in ("pipeline_stages", "pipeline_overrides", "pipeline_state", "pipelines"):
        con.execute(f"DELETE FROM {t} WHERE pipeline_id = ?", [pipeline_id])


# ----------------------------------------------------------------------------- coverholders
def coverholders(con) -> list[dict]:
    rows = con.execute("""
        SELECT coalesce(f.coverholder, '') AS ch, count(*) AS files, count(*) FILTER (WHERE f.content_stored) AS stored,
               count(DISTINCT s.layout_hash) AS layouts, max(f.modified)::VARCHAR AS last_modified,
               count(*) FILTER (WHERE f.warnings IS NOT NULL AND f.warnings <> '') AS warned
        FROM v_files f LEFT JOIN v_sheets s ON s.run_id = f.run_id AND s.rel_path = f.rel_path AND s.header_row IS NOT NULL
        WHERE f.inspected AND f.status = 'ok' GROUP BY 1 ORDER BY 1""").fetchall()
    return [{"name": r[0], "files": r[1], "stored": r[2], "layouts": r[3], "last_modified": r[4], "warned": r[5]} for r in rows]


# ----------------------------------------------------------------------------- helpers
def _target(con, pipe: dict, stage: dict, coverholder: str) -> tuple[str, str]:
    for o in pipe["overrides"]:
        if o["position"] == stage["position"] and o["coverholder"] == coverholder:
            return o["dataflow_id"], o["entity"]
    return stage["dataflow_id"], stage["entity"]


def _attrs(con, dataflow_id: str, entity: str) -> list[dict]:
    return [{"name": n, "data_type": t} for n, t in con.execute(
        "SELECT name, data_type FROM dataflow_attributes WHERE dataflow_id = ? AND entity = ? ORDER BY position",
        [dataflow_id, entity]).fetchall()]


def _version(con, pipe: dict, position: int, coverholder: Optional[str]) -> str:
    """When this stage's output (for one coverholder, or overall) last changed: what downstream stages depend on."""
    stage = pipe["stages"][position]
    if stage["kind"] == "files":
        ents = {stage["entity"]} | {o["entity"] for o in pipe["overrides"] if o["position"] == position}
        ph = ", ".join("?" for _ in ents)
        sql = f"SELECT max(finished_utc) FROM load_log WHERE status = 'ok' AND entity IN ({ph})"
        args = list(ents)
        if coverholder is not None:
            sql, args = sql + " AND coalesce(coverholder, '') = ?", args + [coverholder]
        v = con.execute(sql, args).fetchone()[0]
        return str(v)
    if coverholder is not None:
        r = con.execute("SELECT version_utc FROM pipeline_state WHERE pipeline_id = ? AND position = ? AND coverholder = ?",
                        [pipe["pipeline_id"], position, coverholder]).fetchone()
        return str(r[0]) if r else "none"
    rows = con.execute("SELECT coverholder, version_utc FROM pipeline_state WHERE pipeline_id = ? AND position = ? ORDER BY coverholder",
                       [pipe["pipeline_id"], position]).fetchall()
    return ";".join(f"{c}={v}" for c, v in rows) or "none"


def _fingerprint(parts: dict) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _state(con, pipe_id: str, position: int, coverholder: str) -> Optional[tuple]:
    return con.execute("SELECT fingerprint, version_utc FROM pipeline_state WHERE pipeline_id = ? AND position = ? AND coverholder = ?",
                       [pipe_id, position, coverholder]).fetchone()


def _save_state(con, pipe_id, position, coverholder, fingerprint, rows) -> None:
    con.execute("DELETE FROM pipeline_state WHERE pipeline_id = ? AND position = ? AND coverholder = ?", [pipe_id, position, coverholder])
    con.execute("INSERT INTO pipeline_state VALUES (?, ?, ?, ?, now(), ?)", [pipe_id, position, coverholder, fingerprint, rows])


def _make_view(con, table: str, coverholder: str) -> None:
    """``<table>__<coverholder>`` view so a lookup/merge query can be bound to one coverholder's rows."""
    ch = coverholder.replace("'", "''")
    con.execute(f"CREATE OR REPLACE VIEW {qi(f'{table}__{slug(coverholder)}')} AS SELECT * FROM {qi(table)} WHERE _coverholder = '{ch}'")


# ----------------------------------------------------------------------------- stage execution
def _run_files_stage(con, pipe, stage, coverholder, force, progress) -> dict:
    dfid, ent = _target(con, pipe, stage, coverholder)
    plan = tf.resolve(con, dfid, ent)
    kind = "input" if plan["mode"] == "m" else "attribute"
    if plan["use_m"] and plan["mode"] != "m":
        return {"status": "error", "detail": f"Power Query steps for {ent!r} are not ready: {why_not_ready(plan)}"}
    has_files = con.execute("SELECT count(*) FROM v_files f JOIN file_blobs b ON b.sha256 = f.sha256 "
                            "WHERE coalesce(f.coverholder, '') = ? AND f.inspected AND f.status = 'ok'", [coverholder]).fetchone()[0]
    if not has_files:
        return {"status": "no_files", "detail": "No profiled files with stored content for this coverholder"}
    res = ml.load_entity(con, dfid, ent, force=force, coverholder=coverholder, table=stage["output_table"], progress=progress)
    unmapped = ml.unmapped_layouts(con, ent, kind, coverholder)
    rows = con.execute(f"SELECT count(*) FROM {qi(stage['output_table'])} WHERE coalesce(_coverholder, '') = ?", [coverholder]).fetchone()[0]
    _save_state(con, pipe["pipeline_id"], stage["position"], coverholder, "", rows)
    _make_view(con, stage["output_table"], coverholder)
    warn = [f"{res['ambiguous_dates']} text dates could be read either way (read {res['date_order']}-first)"] if res["ambiguous_dates"] else []
    if res["sheets_failed"]:
        return {"status": "error", "rows": res["rows"], "detail": f"{res['sheets_failed']} sheet(s) failed (see load_log)", "warnings": warn}
    if unmapped:
        names = ", ".join(f"{u['example']} ({u['files']} file(s))" for u in unmapped[:5])
        return {"status": "needs_mapping", "rows": res["rows"], "warnings": warn,
                "detail": f"{len(unmapped)} layout(s) have no column mapping yet, e.g. {names}. Map them (or mark them as not a bordereau) "
                          "on the Map & load tab.", "layouts": [u["layout_hash"] for u in unmapped]}
    status = "loaded" if res["sheets_loaded"] else "up_to_date"
    return {"status": status, "rows": res["rows"], "warnings": warn,
            "detail": f"{res['sheets_loaded']} sheet(s) loaded, {res['replaced_rows']} earlier rows replaced" if res["sheets_loaded"] else "nothing new"}


def why_not_ready(plan: dict) -> str:
    """Plain-language reason a dataflow cannot run yet: unbound lookups, untranslated steps, parse errors."""
    p = plan["pipeline"]
    parts = []
    missing = [n for n, r in p.references.items() if r.get("kind") == "missing"]
    if missing:
        parts.append(f"lookup(s) {', '.join(missing)} need data: import the lookup file and bind each one to its table "
                     "(Map & load tab, Lookups)")
    bad = [f"{s['name']} ({s['error']})" for s in p.steps if not s["ok"] and "needs data" not in s["error"]]
    if bad:
        parts.append("steps that cannot be translated: " + "; ".join(bad[:3]))
    if p.error and not missing:
        parts.append(p.error)
    return "; ".join(parts) or plan["status"]


def _run_derived_stage(con, pipe, stage, coverholder: Optional[str], force, run_id) -> dict:
    """Apply a dataflow to the previous stage's output. ``coverholder=None`` means all coverholders (merge)."""
    ch = coverholder or ""
    dfid, ent = _target(con, pipe, stage, ch)
    plan = tf.resolve(con, dfid, ent, ch)
    if plan["mode"] != "m":
        return {"status": "error", "detail": f"The dataflow for {ent!r} is not ready: {why_not_ready(plan)}. You can also supply SQL "
                                              "or accept a partial translation on the Map & load tab."}
    upstream = pipe["stages"][stage["input_stage"]]
    up_table = upstream["output_table"]
    if not con.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [up_table]).fetchone():
        return {"status": "blocked", "detail": f"Upstream table {up_table!r} does not exist yet"}
    up_cols = [r[0] for r in con.execute(f"SELECT column_name FROM (DESCRIBE {qi(up_table)})").fetchall()]
    missing = [c for c in plan["inputs"] if c.lower() not in {u.lower() for u in up_cols}]
    if missing:
        return {"status": "error", "detail": f"The steps read column(s) {missing} that the previous stage ({upstream['name']!r}) does not "
                                              f"produce. Available: {[c for c in up_cols if not c.startswith('_')]}"}
    p = plan["pipeline"]
    refs = {n: tf.table_fingerprint(con, r["table"]) for n, r in p.references.items() if r.get("table")}
    parts = {"up": _version(con, pipe, upstream["position"], coverholder), "df": dfid, "entity": ent, "sql": plan["sql"], "refs": refs,
             "attrs": _attrs(con, dfid, ent), "order": plan["date_order"], "ch": ch}
    fp = _fingerprint(parts)
    st = _state(con, pipe["pipeline_id"], stage["position"], ch)
    exists = con.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [stage["output_table"]]).fetchone()
    if st and st[0] == fp and exists and not force:
        return {"status": "up_to_date", "detail": "inputs unchanged", "warnings": p.warnings}
    where, args = ("WHERE coalesce(_coverholder, '') = ?", [ch]) if coverholder is not None else ("", [])
    order = "ORDER BY coalesce(_coverholder, ''), coalesce(_source_id, ''), coalesce(_sheet, ''), coalesce(_excel_row, 0)"
    ensure_macros(con)
    con.execute(f'CREATE OR REPLACE TEMP TABLE "_stg" AS SELECT row_number() OVER ({order}) AS "__row", * '
                f"FROM {qi(up_table)} {where}", args)
    res = con.execute(plan["sql"])
    out_cols = [d[0] for d in res.description]
    result_rows = res.fetchall()
    idx = {c.lower(): i for i, c in enumerate(out_cols)}
    attrs = _attrs(con, dfid, ent)
    types = {a["name"]: a["data_type"] for a in attrs}
    names = [a["name"] for a in attrs]
    now = datetime.now(timezone.utc)
    all_cols = names + [c for c, _ in ml.PROVENANCE]
    buf, bad = [], 0
    for r in result_rows:
        vals, errs = [], []
        for n in names:
            i = idx.get(n.lower())
            v, ok = ml.coerce(r[i] if i is not None else None, types[n], plan["date_order"])
            vals.append(v)
            if not ok:
                errs.append(n)
        bad += len(errs)
        prov = {"_load_id": run_id, "_loaded_utc": now, "_coerce_errors": ",".join(errs) or None}
        for c in LINEAGE:
            i = idx.get(c)
            prov[c] = r[i] if i is not None else (ch if c == "_coverholder" and coverholder is not None else None)
        buf.append(vals + [prov.get(c) for c, _ in ml.PROVENANCE])
    con.execute("BEGIN")
    try:
        ml.ensure_target_table(con, ent, attrs, stage["output_table"])
        if coverholder is not None:
            con.execute(f"DELETE FROM {qi(stage['output_table'])} WHERE coalesce(_coverholder, '') = ?", [ch])
        else:
            con.execute(f"DELETE FROM {qi(stage['output_table'])}")
        bulk_insert(con, stage["output_table"], all_cols, buf)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    _save_state(con, pipe["pipeline_id"], stage["position"], ch, fp, len(buf))
    if coverholder is not None:
        _make_view(con, stage["output_table"], coverholder)
    return {"status": "loaded", "rows": len(buf), "warnings": p.warnings + ([f"{bad} cell(s) could not be converted"] if bad else []),
            "detail": f"{len(buf)} rows written"}


# ----------------------------------------------------------------------------- orchestration
def run_pipeline(con, pipeline_id: str, coverholder_names: Optional[list[str]] = None, force: bool = False,
                 allow_partial_merge: bool = False, progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """Run every stage for every (selected) coverholder, then the merge stages. Returns a per-step report."""
    pipe = get_pipeline(con, pipeline_id)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    started = datetime.now(timezone.utc)
    con.execute("INSERT INTO pipeline_runs (run_id, pipeline_id, started_utc, status, params) VALUES (?, ?, ?, 'running', ?)",
                [run_id, pipeline_id, started, json.dumps({"coverholders": coverholder_names, "force": force,
                                                            "allow_partial_merge": allow_partial_merge})])
    refreshed = tf.refresh_references(con)
    chs = coverholder_names if coverholder_names else [c["name"] for c in coverholders(con) if c["stored"] and c["name"]]
    per_ch = [s for s in pipe["stages"] if s["kind"] != "merge"]
    merges = [s for s in pipe["stages"] if s["kind"] == "merge"]
    total, done = len(chs) * len(per_ch) + len(merges), 0
    report = {"run_id": run_id, "coverholders": {}, "merge": [], "refreshed_references": refreshed}

    def record(stage, ch, res, t0):
        nonlocal done
        done += 1
        con.execute("INSERT INTO pipeline_run_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())",
                    [run_id, stage["position"], stage["name"], ch, res["status"], res.get("rows"), res.get("detail"), t0])
        if progress:
            progress(done, total, f"{ch or 'all'} · {stage['name']}: {res['status']}")

    def guarded(fn, *args):
        try:
            return fn(*args)
        except Exception as e:                 # one failing coverholder/stage must not stop the rest
            return {"status": "error", "detail": f"{type(e).__name__}: {e}"}

    failed: list[str] = []
    for ch in chs:
        steps, blocked_by = [], None
        for stage in per_ch:
            t0 = datetime.now(timezone.utc)
            if blocked_by:
                res = {"status": "blocked", "detail": f"stage {blocked_by[0]!r} is {blocked_by[1]}"}
            elif stage["kind"] == "files":
                res = guarded(_run_files_stage, con, pipe, stage, ch, force, None)
            else:
                res = guarded(_run_derived_stage, con, pipe, stage, ch, force, run_id)
            if res["status"] not in OK_STATUSES and not blocked_by:
                blocked_by = (stage["name"], res["status"])
            steps.append({"position": stage["position"], "stage": stage["name"], **res})
            record(stage, ch, res, t0)
        report["coverholders"][ch] = {"ok": blocked_by is None, "steps": steps}
        if blocked_by:
            failed.append(ch)
    for stage in merges:
        t0 = datetime.now(timezone.utc)
        if failed and not allow_partial_merge:
            res = {"status": "blocked", "detail": f"coverholder(s) not ready: {', '.join(failed)}. Fix them, or run with 'allow partial merge'."}
        elif not chs:
            res = {"status": "blocked", "detail": "No coverholders with stored files"}
        else:
            res = guarded(_run_derived_stage, con, pipe, stage, None, force, run_id)
        report["merge"].append({"position": stage["position"], "stage": stage["name"], **res})
        record(stage, "", res, t0)
    steps_all = [s for c in report["coverholders"].values() for s in c["steps"]] + report["merge"]
    good = [s for s in steps_all if s["status"] in OK_STATUSES]
    report["status"] = "ok" if len(good) == len(steps_all) else ("failed" if not good else "partial")
    report["failed_coverholders"] = failed
    con.execute("UPDATE pipeline_runs SET status = ?, finished_utc = now(), summary = ? WHERE run_id = ?",
                [report["status"], json.dumps(report, default=str), run_id])
    return report


def status_matrix(con, pipeline_id: str) -> dict:
    """Latest state per (coverholder, stage) plus the last run's step results."""
    pipe = get_pipeline(con, pipeline_id)
    state = {(r[0], r[1]): {"version": str(r[2]), "rows": r[3]} for r in con.execute(
        "SELECT position, coverholder, version_utc, rows FROM pipeline_state WHERE pipeline_id = ?", [pipeline_id]).fetchall()}
    last = con.execute("SELECT run_id, started_utc::VARCHAR, finished_utc::VARCHAR, status FROM pipeline_runs WHERE pipeline_id = ? "
                       "ORDER BY started_utc DESC LIMIT 1", [pipeline_id]).fetchone()
    steps = []
    if last:
        steps = [dict(zip(["position", "stage", "coverholder", "status", "rows", "detail"], r)) for r in con.execute(
            "SELECT position, stage_name, coverholder, status, rows, detail FROM pipeline_run_steps WHERE run_id = ? "
            "ORDER BY coverholder, position", [last[0]]).fetchall()]
    return {"pipeline": pipe, "state": [{"position": k[0], "coverholder": k[1], **v} for k, v in state.items()],
            "last_run": dict(zip(["run_id", "started", "finished", "status"], last)) if last else None, "steps": steps}
