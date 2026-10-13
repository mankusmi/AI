"""Markdown + self-contained HTML documentation from a snapshot and its analysis."""
from __future__ import annotations

import html
from pathlib import Path

from .store import slug


def _md_cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _ws_lines(snapshot: dict) -> list[tuple[str, object]]:
    ws = snapshot["workspace"]
    return [("Name", ws.get("name", "(unknown)")), ("Id", ws["id"]), ("Type", ws.get("type")), ("State", ws.get("state")),
            ("Dedicated capacity", ws.get("isOnDedicatedCapacity")), ("Capacity id", ws.get("capacityId")),
            ("Read-only", ws.get("isReadOnly")), ("Fetched at", snapshot["fetched_at"])]


def _object_rows(snapshot: dict) -> dict[str, list[list]]:
    o = snapshot["objects"]
    return {
        "Dataflows": [[x.get("name"), x.get("configuredBy"), x.get("modifiedBy"), x.get("modifiedDateTime"), x.get("description")] for x in o["dataflows"]],
        "Datasets": [[x.get("name"), x.get("configuredBy"), x.get("createdDate"), x.get("isRefreshable"), x.get("targetStorageMode")] for x in o["datasets"]],
        "Reports": [[x.get("name"), x.get("reportType"), x.get("datasetId"), x.get("modifiedBy"), x.get("webUrl")] for x in o["reports"]],
        "Dashboards": [[x.get("displayName") or x.get("name"), x.get("isReadOnly"), x.get("webUrl")] for x in o["dashboards"]],
    }


_OBJ_HEADERS = {
    "Dataflows": ["Name", "Configured by", "Modified by", "Modified", "Description"],
    "Datasets": ["Name", "Configured by", "Created", "Refreshable", "Storage mode"],
    "Reports": ["Name", "Type", "Dataset id", "Modified by", "URL"],
    "Dashboards": ["Name", "Read-only", "URL"],
}


def _sources(rec: dict) -> list[str]:
    seen = [f"{s['kind']}" + (f": {s['detail']}" if s["detail"] else "") for q in rec["queries"] for s in q["sources"]]
    seen += [f"{s['kind']}" + (f": {s['detail']}" if s["detail"] else "") for s in rec["api_sources"]]
    return list(dict.fromkeys(seen))


def dataflow_markdown(rec: dict, names: dict[str, str], flow: dict) -> str:
    L = [f"# {rec['name']}", "", f"Run position **{rec['order']}** (stage {rec['stage']})  ", f"Id: `{rec['id']}`", "",
         "## Summary", "", rec["summary"], ""]
    m = rec["meta"]
    L += ["## Metadata", "", "| Field | Value |", "|---|---|"]
    for k in ("description", "configuredBy", "modifiedBy", "modifiedDateTime", "modelUrl"):
        if m.get(k):
            L.append(f"| {k} | {_md_cell(m[k])} |")
    L.append(f"| last successful refresh | {_md_cell(rec['last_success'] or 'n/a')} |")
    L += ["", "## Dependencies", "",
          "Runs after: " + (", ".join(names[d] for d in rec["depends_on"]) or "nothing (no upstream dataflows)"),
          "", "Feeds dataflows: " + (", ".join(names[d] for d in rec["downstream"]) or "none"), "",
          "Feeds datasets: " + (", ".join(rec["datasets"]) or "none")]
    if rec["external_deps"]:
        L += ["", "Depends on dataflows outside this workspace: " + ", ".join(f"`{x}`" for x in rec["external_deps"])]
    L += ["", "## Data sources", ""] + ([f"- {s}" for s in _sources(rec)] or ["- none detected"])
    L += ["", "## Entities (tables)", "", "| Name | Kind | Columns | Last refresh | Description |", "|---|---|---|---|---|"]
    for e in rec["entities"]:
        L.append(f"| {_md_cell(e['name'])} | {e['kind']}{' <- ' + _md_cell(e['linked_source']) if e['linked_source'] else ''} | "
                 f"{e['columns']} | {_md_cell(e['last_refresh'] or '')} | {_md_cell(e['description'])} |")
    for e in rec["entities"]:
        if e["column_names"]:
            L += ["", f"**{e['name']} columns:** " + ", ".join(f"`{c}`" for c in e["column_names"])]
    L += ["", "## Queries", "", "| Query | Sources | Transformations |", "|---|---|---|"]
    for q in rec["queries"]:
        L.append(f"| {_md_cell(q['name'])} | {_md_cell('; '.join(s['kind'] for s in q['sources']))} | "
                 f"{_md_cell(', '.join(f'{k} x{v}' for k, v in q['steps'].items()))} |")
    tx = flow.get("transactions") or []
    if tx:
        L += ["", "## Recent refreshes", "", "| Start | End | Type | Status |", "|---|---|---|---|"]
        for t in sorted(tx, key=lambda t: t.get("startTime") or "", reverse=True)[:10]:
            L.append(f"| {t.get('startTime', '')} | {t.get('endTime', '')} | {t.get('refreshType', '')} | {t.get('status', '')} |")
    return "\n".join(L) + "\n"


def summary_markdown(snapshot: dict, analysis: dict, doc_names: dict[str, str]) -> str:
    names = {r["id"]: r["name"] for r in analysis["flows"]}
    L = ["# Workspace summary", "", "## Workspace", "", "| Property | Value |", "|---|---|"]
    L += [f"| {k} | {_md_cell(v)} |" for k, v in _ws_lines(snapshot) if v is not None]
    L += ["", "## Objects in workspace", "", "| Type | Count |", "|---|---|"]
    L += [f"| {k} | {len(v)} |" for k, v in snapshot["objects"].items()]
    L.append(f"| Users / access entries | {len(snapshot['users'])} |")
    L += ["", "## Dataflow run order", "",
          "Dependencies first; dataflows sharing a stage are independent and can run in parallel.", "",
          "| # | Stage | Dataflow | Runs after | Last successful refresh | Last status |", "|---|---|---|---|---|---|"]
    for r in analysis["flows"]:
        L.append(f"| {r['order']} | {r['stage']} | [{_md_cell(r['name'])}](docs/{doc_names[r['id']]}) | "
                 f"{_md_cell(', '.join(names[d] for d in r['depends_on']) or '-')} | {_md_cell(r['last_success'] or '-')} | {_md_cell(r['last_status'] or '-')} |")
    L += ["", "## What each dataflow does", ""]
    for r in analysis["flows"]:
        L.append(f"{r['order']}. **{r['name']}** - {r['summary']}")
    for title, rows in _object_rows(snapshot).items():
        if rows:
            hdr = _OBJ_HEADERS[title]
            L += ["", f"## {title}", "", "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
            L += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
    if snapshot["users"]:
        L += ["", "## Access", "", "| Identifier | Type | Role |", "|---|---|---|"]
        L += [f"| {_md_cell(u.get('emailAddress') or u.get('displayName') or u.get('identifier'))} | {_md_cell(u.get('principalType'))} | {_md_cell(u.get('groupUserAccessRight'))} |" for u in snapshot["users"]]
    if analysis["warnings"]:
        L += ["", "## Warnings", ""] + [f"- {w}" for w in analysis["warnings"]]
    return "\n".join(L) + "\n"


_CSS = """:root{--bg:#fff;--fg:#1d2330;--mut:#5d6677;--card:#f5f7fa;--bd:#d9dee7;--ac:#2f6fdd}
@media (prefers-color-scheme:dark){:root{--bg:#14171d;--fg:#e6e9ef;--mut:#9aa3b2;--card:#1d222b;--bd:#2f3642;--ac:#6ea0ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}main{max-width:1200px;margin:0 auto;padding:24px 16px}
h1{margin:0 0 4px}h2{margin:32px 0 8px;border-bottom:1px solid var(--bd);padding-bottom:4px}.mut{color:var(--mut)}
.cards{display:flex;flex-wrap:wrap;gap:12px}.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:10px 16px;min-width:110px}
.card b{display:block;font-size:24px}table{border-collapse:collapse;width:100%;margin:8px 0}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bd);vertical-align:top}
th{color:var(--mut);font-weight:600}.stage{display:flex;gap:12px;align-items:flex-start;margin:8px 0;flex-wrap:wrap}.stage>b{min-width:70px;color:var(--mut)}
.chip{background:var(--card);border:1px solid var(--bd);border-left:4px solid var(--ac);border-radius:6px;padding:4px 10px}
details{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:8px 12px;margin:8px 0}summary{cursor:pointer;font-weight:600}
code{font-size:12px}.warn{color:#b45309}.wrap{overflow-x:auto}"""


def _t(headers, rows) -> str:
    e = html.escape
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{e(str(c if c is not None else ''))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='wrap'><table><tr>{head}</tr>{body}</table></div>"


def summary_html(snapshot: dict, analysis: dict) -> str:
    e = html.escape
    names = {r["id"]: r["name"] for r in analysis["flows"]}
    ws = snapshot["workspace"]
    P = [f"<h1>{e(ws.get('name', ws['id']))}</h1><div class='mut'>Power BI workspace - fetched {e(snapshot['fetched_at'])}</div>",
         "<div class='cards'>" + "".join(f"<div class='card'><b>{len(v)}</b>{e(k)}</div>" for k, v in snapshot["objects"].items())
         + f"<div class='card'><b>{len(snapshot['users'])}</b>users</div></div>",
         "<h2>Workspace</h2>", _t(["Property", "Value"], [(k, v) for k, v in _ws_lines(snapshot) if v is not None]),
         "<h2>Dataflow run order</h2><p class='mut'>Dependencies first. Dataflows in the same stage don't depend on each other.</p>"]
    for i, ids in enumerate(analysis["stages"], 1):
        P.append(f"<div class='stage'><b>Stage {i}</b>" + "".join(
            f"<span class='chip'>{r['order']}. {e(r['name'])}</span>" for r in analysis["flows"] if r["id"] in ids) + "</div>")
    P.append(_t(["#", "Dataflow", "Runs after", "Feeds", "Last success", "Last status"], [
        (r["order"], r["name"], ", ".join(names[d] for d in r["depends_on"]) or "-",
         ", ".join([names[d] for d in r["downstream"]] + r["datasets"]) or "-", r["last_success"] or "-", r["last_status"] or "-")
        for r in analysis["flows"]]))
    P.append("<h2>What each dataflow does</h2>")
    for r in analysis["flows"]:
        ent = [(x["name"], x["kind"], x["columns"], x["last_refresh"] or "") for x in r["entities"]]
        P.append(f"<details><summary>{r['order']}. {e(r['name'])}</summary><p>{e(r['summary'])}</p>"
                 f"<p class='mut'>Sources: {e('; '.join(_sources(r)) or 'none detected')}</p>"
                 + (_t(["Entity", "Kind", "Columns", "Last refresh"], ent) if ent else "") + "</details>")
    for title, rows in _object_rows(snapshot).items():
        if rows:
            P.append(f"<h2>{e(title)} ({len(rows)})</h2>" + _t(_OBJ_HEADERS[title], rows))
    if snapshot["users"]:
        P.append("<h2>Access</h2>" + _t(["Identifier", "Type", "Role"], [
            (u.get("emailAddress") or u.get("displayName") or u.get("identifier"), u.get("principalType"), u.get("groupUserAccessRight")) for u in snapshot["users"]]))
    if analysis["warnings"]:
        P.append("<h2>Warnings</h2><ul class='warn'>" + "".join(f"<li>{e(w)}</li>" for w in analysis["warnings"]) + "</ul>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Workspace summary</title><style>{_CSS}</style></head><body><main>{''.join(P)}</main></body></html>")


def write_docs(out: Path, snapshot: dict, analysis: dict) -> None:
    docs = out / "docs"
    docs.mkdir(exist_ok=True)
    for old in docs.glob("*.md"):
        old.unlink()
    names = {r["id"]: r["name"] for r in analysis["flows"]}
    flows = {f["id"]: f for f in snapshot["dataflows"]}
    width = max(2, len(str(len(analysis["flows"]))))
    doc_names = {r["id"]: f"{r['order']:0{width}d}_{slug(r['name'])}.md" for r in analysis["flows"]}
    for r in analysis["flows"]:
        (docs / doc_names[r["id"]]).write_text(dataflow_markdown(r, names, flows[r["id"]]), encoding="utf-8")
    (out / "SUMMARY.md").write_text(summary_markdown(snapshot, analysis, doc_names), encoding="utf-8")
    (out / "summary.html").write_text(summary_html(snapshot, analysis), encoding="utf-8")
