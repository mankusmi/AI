"""Lineage graph (sources -> dataflows -> datasets -> reports) built from a snapshot + analysis; Mermaid and SVG/HTML output."""
from __future__ import annotations

import html
import re

_COLS = ["source", "dataflow", "dataset", "report"]
_FILL = {"source": "#eef2f7", "dataflow": "#dbeafe", "dataset": "#dcfce7", "report": "#fef3c7"}
_STROKE = {"source": "#64748b", "dataflow": "#2563eb", "dataset": "#16a34a", "report": "#d97706"}


def build_lineage(snapshot: dict, analysis: dict) -> dict:
    """Return {'nodes': [{id, label, type, col, sub}], 'edges': [(from, to)]}. Dataflow column = run stage."""
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str]] = []

    def node(nid, label, typ, col, sub=""):
        nodes.setdefault(nid, {"id": nid, "label": label, "type": typ, "col": col, "sub": sub})
        return nid

    def edge(a, b):
        if (a, b) not in edges:
            edges.append((a, b))

    for r in analysis["flows"]:
        node("df:" + r["id"], r["name"], "dataflow", r["stage"], f"#{r['order']}")
    max_stage = max((r["stage"] for r in analysis["flows"]), default=0)
    for r in analysis["flows"]:
        did = "df:" + r["id"]
        srcs = [s for q in r["queries"] for s in q["sources"]] or r["api_sources"]
        for s in srcs:
            if s["kind"] == "Power BI dataflow":
                continue
            sid = node(f"src:{s['kind']}|{s['detail']}", s["kind"], "source", 0, s["detail"])
            edge(sid, did)
        for dep in r["depends_on"]:
            edge("df:" + dep, did)
    ds_names = {d.get("id"): d.get("name") or d.get("id") for d in snapshot.get("objects", {}).get("datasets", [])}
    flow_ids = {r["id"] for r in analysis["flows"]}
    used_ds = set()
    for link in snapshot.get("dataset_upstream", []):
        if link.get("dataflowObjectId") in flow_ids and link.get("datasetObjectId"):
            used_ds.add(link["datasetObjectId"])
            edge("df:" + link["dataflowObjectId"], node("ds:" + link["datasetObjectId"], ds_names.get(link["datasetObjectId"], link["datasetObjectId"]), "dataset", max_stage + 1))
    for rep in snapshot.get("objects", {}).get("reports", []):
        if rep.get("datasetId") in used_ds:
            edge("ds:" + rep["datasetId"], node("rp:" + str(rep.get("id") or rep.get("name")), rep.get("name") or "(report)", "report", max_stage + 2))
    return {"nodes": list(nodes.values()), "edges": edges}


def _mid(s: str) -> str:
    return re.sub(r"\W", "_", s)


def mermaid(g: dict) -> str:
    shape = {"source": '[("{}")]', "dataflow": '["{}"]', "dataset": '[["{}"]]', "report": '("{}")'}
    lines = ["flowchart LR"]
    for n in g["nodes"]:
        label = n["label"].replace('"', "'") + (f"<br/>{n['sub'].replace(chr(34), chr(39))}" if n["sub"] and n["type"] == "source" else "")
        lines.append(f"    {_mid(n['id'])}{shape[n['type']].format(label)}")
    lines += [f"    {_mid(a)} --> {_mid(b)}" for a, b in g["edges"]]
    return "\n".join(lines)


def lineage_markdown(snapshot: dict, analysis: dict) -> str:
    g = build_lineage(snapshot, analysis)
    names = {n["id"]: n["label"] for n in g["nodes"]}
    out = [f"# Lineage - {snapshot['workspace'].get('name', snapshot['workspace']['id'])}", "",
           "Sources -> dataflows (by run stage) -> datasets -> reports.", "", "```mermaid", mermaid(g), "```", "",
           "## Edges", "", "| From | To |", "|---|---|"]
    out += [f"| {names[a]} | {names[b]} |" for a, b in g["edges"]]
    return "\n".join(out) + "\n"


def lineage_svg(g: dict) -> str:
    e = html.escape
    W, H, GX, GY, PAD = 200, 44, 90, 18, 20
    cols = sorted({n["col"] for n in g["nodes"]})
    by_col = {c: [n for n in g["nodes"] if n["col"] == c] for c in cols}
    pos: dict[str, tuple[float, float]] = {}
    preds: dict[str, list[str]] = {}
    for a, b in g["edges"]:
        preds.setdefault(b, []).append(a)
    for c in cols:  # order each column by mean position of its inputs to reduce edge crossings
        by_col[c].sort(key=lambda n: (sum(pos[p][1] for p in preds.get(n["id"], []) if p in pos) / max(1, sum(p in pos for p in preds.get(n["id"], []))), n["label"].lower()))
        for i, n in enumerate(by_col[c]):
            pos[n["id"]] = (PAD + cols.index(c) * (W + GX), PAD + i * (H + GY))
    width = PAD * 2 + len(cols) * W + (len(cols) - 1) * GX
    height = PAD * 2 + max((len(v) for v in by_col.values()), default=1) * (H + GY)
    parts = [f"<svg class='lineage' xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' width='{width}' height='{height}' font-family='system-ui,sans-serif' font-size='12'>",
             "<defs><marker id='ar' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto'><path d='M0,0 L10,5 L0,10 z' fill='#94a3b8'/></marker></defs>"]
    for a, b in g["edges"]:
        (x1, y1), (x2, y2) = pos[a], pos[b]
        x1, y1, x2, y2 = x1 + W, y1 + H / 2, x2, y2 + H / 2
        if x2 <= x1:  # same column (dataflow stage collision cannot happen, but be safe)
            x1, x2 = x2 + W, x2
        dx = max(30, (x2 - x1) / 2)
        parts.append(f"<path class='edge' data-from='{e(a)}' data-to='{e(b)}' d='M{x1},{y1} C{x1 + dx},{y1} {x2 - dx},{y2} {x2},{y2}' fill='none' stroke='#94a3b8' stroke-width='1.5' marker-end='url(#ar)'/>")
    for n in g["nodes"]:
        x, y = pos[n["id"]]
        sub = f"<text x='{x + 10}' y='{y + 36}' fill='#475569' font-size='10'>{e(n['sub'][:34])}</text>" if n["sub"] else ""
        parts.append(f"<g class='node' data-id='{e(n['id'])}'><title>{e(n['label'])}{' - ' + e(n['sub']) if n['sub'] else ''}</title>"
                     f"<rect x='{x}' y='{y}' width='{W}' height='{H}' rx='6' fill='{_FILL[n['type']]}' stroke='{_STROKE[n['type']]}'/>"
                     f"<text x='{x + 10}' y='{y + 19}' font-weight='600' fill='#0f172a'>{e(n['label'][:30] + ('…' if len(n['label']) > 30 else ''))}</text>{sub}</g>")
    return "".join(parts) + "</svg>"


LINEAGE_CSS = ("svg.lineage .node{cursor:pointer}svg.lineage.hl .dim{opacity:.15}svg.lineage .edge.on{stroke:#2563eb;stroke-width:2.5}"
               "svg.lineage .node.sel rect{stroke-width:3}")
# Click a node: highlight it with everything upstream and downstream; click it again or the background to reset.
LINEAGE_JS = """(function(){document.querySelectorAll('svg.lineage').forEach(function(svg){
var nodes=[].slice.call(svg.querySelectorAll('.node')),edges=[].slice.call(svg.querySelectorAll('.edge')),sel=null;
function walk(start,from,to){var seen={},q=[start];seen[start]=1;while(q.length){var c=q.pop();edges.forEach(function(e){
if(e.dataset[from]===c&&!seen[e.dataset[to]]){seen[e.dataset[to]]=1;q.push(e.dataset[to]);}});}return seen;}
function paint(){if(!sel){svg.classList.remove('hl');nodes.concat(edges).forEach(function(x){x.classList.remove('dim','on','sel');});return;}
var up=walk(sel,'to','from'),dn=walk(sel,'from','to');svg.classList.add('hl');
nodes.forEach(function(n){var i=n.dataset.id;n.classList.toggle('dim',!(up[i]||dn[i]));n.classList.toggle('sel',i===sel);});
edges.forEach(function(e){var on=(up[e.dataset.from]&&up[e.dataset.to])||(dn[e.dataset.from]&&dn[e.dataset.to]);
e.classList.toggle('dim',!on);e.classList.toggle('on',!!on);});}
nodes.forEach(function(n){n.addEventListener('click',function(ev){ev.stopPropagation();sel=sel===n.dataset.id?null:n.dataset.id;paint();});});
svg.addEventListener('click',function(){sel=null;paint();});});})();"""


def lineage_html(snapshot: dict, analysis: dict) -> str:
    g = build_lineage(snapshot, analysis)
    ws = snapshot["workspace"]
    legend = "".join(f"<span style='background:{_FILL[t]};border:1px solid {_STROKE[t]};padding:2px 8px;border-radius:4px;margin-right:8px'>{t}</span>" for t in _COLS)
    body = lineage_svg(g) if g["nodes"] else "<p>No dataflows found.</p>"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Dataflow lineage</title><style>body{{font-family:system-ui,sans-serif;margin:24px;background:#fff;color:#0f172a}}"
            f"{LINEAGE_CSS}.mut{{color:#64748b}}.box{{overflow:auto;border:1px solid #e2e8f0;border-radius:8px;padding:8px}}</style></head><body>"
            f"<h1>{html.escape(ws.get('name', ws['id']))} - lineage</h1><p class='mut'>Sources → dataflows (left to right in run order) → datasets → reports. <b>Click a node</b> to highlight everything upstream and downstream of it; click the background to reset. {legend}</p>"
            f"<div class='box'>{body}</div><script>{LINEAGE_JS}</script></body></html>")
