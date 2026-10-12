"""Render a Report as Markdown or a self-contained HTML page."""
from __future__ import annotations

from html import escape as e

from .report import Report

DOT = {"red": "🔴", "amber": "🟠", "green": "🟢"}


def _pct(p):
    return "n/a" if p is None else f"{p:.0f}%"


def to_markdown(r: Report, title: str = "Delivery: state of play") -> str:
    o = [f"# {title}", "", f"_As of {r.as_of:%d %b %Y}_ — overall {DOT[r.overall]} **{r.overall.upper()}**", ""]
    o += [f"- {h}" for h in r.headlines] + [""]
    if r.decisions:
        o += ["## Needs attention / decisions", ""] + [f"1. {d}" for d in r.decisions] + [""]
    o += [f"## Progress by {r.group_by}", "", f"| {r.group_by.title()} | Status | Done | Progress | In progress | Blocked | Last activity | Why |",
          "|---|---|---|---|---|---|---|---|"]
    for x in r.rows:
        o.append(f"| {x.id} {x.title if x.title != x.id else ''} | {DOT[x.rag]} | {x.done}/{x.total} | {_pct(x.pct)} | {x.active} | {x.blocked} | "
                 f"{x.last_activity:%d %b} | {'; '.join(x.reasons) or '—'} |" if x.last_activity else
                 f"| {x.id} {x.title if x.title != x.id else ''} | {DOT[x.rag]} | {x.done}/{x.total} | {_pct(x.pct)} | {x.active} | {x.blocked} | — | {'; '.join(x.reasons) or '—'} |")
    o += ["", "## Throughput (work closed per week)", "", "| Week of | Items | Days of work |", "|---|---|---|"]
    o += [f"| {w} | {n} | {d:g} |" for w, n, d in r.weekly] + [""]
    if r.blocked:
        o += ["## Blocked", ""] + [f"- **{t.id}** {t.title} ({t.assignee or 'unassigned'})" for t in r.blocked] + [""]
    if r.aging:
        o += ["## In progress and gone quiet", ""] + [f"- **{t.id}** {t.title}: {d} days since last update" for t, d in r.aging[:8]] + [""]
    if r.health_issues:
        o += ["## Backlog hygiene", "", f"{r.health_pct:.0f}% of open items have no issues. Most common: " +
              ", ".join(f"{k} ({n})" for k, n in r.health_issues) + ".", ""]
    return "\n".join(o)


CSS = """:root{--bg:#fff;--fg:#1c2330;--mut:#667085;--line:#e4e7ec;--card:#f7f8fa;--red:#d92d20;--amber:#dc6803;--green:#079455;--bar:#3b6fd4}
@media (prefers-color-scheme:dark){:root{--bg:#10141b;--fg:#e6e9ef;--mut:#98a2b3;--line:#2a313c;--card:#181d26;--red:#f97066;--amber:#fdb022;--green:#32d583;--bar:#6c97ee}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}h1{margin:0 0 4px;font-size:26px}h2{margin:32px 0 10px;font-size:17px}
.sub{color:var(--mut)}.chip{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;font-size:13px;color:#fff}
.red{background:var(--red)}.amber{background:var(--amber)}.green{background:var(--green)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:18px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.kpi b{display:block;font-size:24px}.kpi span{color:var(--mut);font-size:13px}
ul{padding-left:20px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600}.wrap{overflow-x:auto}.bar{background:var(--line);border-radius:4px;height:8px;min-width:80px}.bar i{display:block;height:8px;border-radius:4px;background:var(--bar)}
.dec{background:var(--card);border-left:4px solid var(--red);padding:10px 14px;border-radius:6px}"""


def to_html(r: Report, title: str = "Delivery: state of play") -> str:
    c = r.counts
    kp = [(_pct(r.pct_done), "complete"), (c["active"], "in progress"), (c["blocked"], "blocked"), (c["todo"], "not started"),
          (f"{r.eta_date:%d %b}" if r.eta_date else "n/a", "forecast finish"), (f"{r.health_pct:.0f}%", "backlog health")]
    h = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>",
         f"<h1>{e(title)}</h1><div class=sub>As of {r.as_of:%d %b %Y} · overall <span class='chip {r.overall}'>{r.overall.upper()}</span></div>",
         "<div class=kpis>" + "".join(f"<div class=kpi><b>{e(str(v))}</b><span>{e(k)}</span></div>" for v, k in kp) + "</div>",
         "<ul>" + "".join(f"<li>{e(x)}</li>" for x in r.headlines) + "</ul>"]
    if r.decisions:
        h.append("<h2>Needs attention / decisions</h2><div class=dec><ol>" + "".join(f"<li>{e(d)}</li>" for d in r.decisions) + "</ol></div>")
    h.append(f"<h2>Progress by {r.group_by}</h2><div class=wrap><table><tr><th>{r.group_by.title()}<th>Status<th>Progress<th>Done<th>Active<th>Blocked<th>Why")
    for x in r.rows:
        p = x.pct or 0
        h.append(f"<tr><td><b>{e(x.id)}</b> {e(x.title) if x.title != x.id else ''}<td><span class='chip {x.rag}'>{x.rag}</span>"
                 f"<td><div class=bar><i style='width:{p:.0f}%'></i></div>{_pct(x.pct)}<td>{x.done}/{x.total}<td>{x.active}<td>{x.blocked}"
                 f"<td>{e('; '.join(x.reasons)) or '—'}")
    h.append("</table></div>")
    mx = max((d for _, _, d in r.weekly), default=0) or 1
    h.append("<h2>Throughput: days of work closed per week</h2><div class=wrap><table><tr><th>Week of<th>Items<th>Days<th>")
    for w, n, d in r.weekly:
        h.append(f"<tr><td>{w}<td>{n}<td>{d:g}<td style='width:50%'><div class=bar><i style='width:{100 * d / mx:.0f}%'></i></div>")
    h.append("</table></div>")
    if r.blocked:
        h.append("<h2>Blocked</h2><ul>" + "".join(f"<li><b>{e(t.id)}</b> {e(t.title)} ({e(t.assignee or 'unassigned')})</li>" for t in r.blocked) + "</ul>")
    if r.aging:
        h.append("<h2>In progress and gone quiet</h2><ul>" + "".join(f"<li><b>{e(t.id)}</b> {e(t.title)}: {d} days since last update</li>" for t, d in r.aging[:8]) + "</ul>")
    if r.health_issues:
        h.append(f"<h2>Backlog hygiene</h2><p>{r.health_pct:.0f}% of open items have no issues. Most common: " +
                 e(", ".join(f"{k} ({n})" for k, n in r.health_issues)) + ".</p>")
    return "".join(h) + "</main></body></html>"
