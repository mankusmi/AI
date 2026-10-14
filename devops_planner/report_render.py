"""Render a Report as Markdown or a self-contained HTML page."""
from __future__ import annotations

from html import escape as e

from .datalead_render import lead_html, lead_markdown
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
    if r.lead is not None:
        o += [""] + lead_markdown(r.lead).splitlines()
    return "\n".join(o) + "\n"


CSS = """:root{--bg:#f9f9f7;--surface:#fcfcfb;--fg:#0b0b0b;--sec:#52514e;--mut:#6b6a65;--line:#e1e0d9;--grid:#e1e0d9;--axis:#c3c2b7;--card:#f3f2ee;
--s1:#2a78d6;--s250:#86b6ef;--s500:#256abf;--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0d0d0d;--surface:#1a1a19;--fg:#f0efec;--sec:#c3c2b7;--mut:#a3a29a;--line:#2c2c2a;--grid:#2c2c2a;--axis:#383835;--card:#222220;--s1:#3987e5;--s250:#6da7ec;--s500:#3987e5}}
:root[data-theme=dark]{--bg:#0d0d0d;--surface:#1a1a19;--fg:#f0efec;--sec:#c3c2b7;--mut:#a3a29a;--line:#2c2c2a;--grid:#2c2c2a;--axis:#383835;--card:#222220;--s1:#3987e5;--s250:#6da7ec;--s500:#3987e5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:0 16px 56px}h1{margin:0 0 4px;font-size:26px}h2{margin:32px 0 10px;font-size:17px}h2.big{font-size:22px;margin:0 0 14px}
h3{margin:22px 0 6px;font-size:15px}.sub{color:var(--mut);font-size:13px}p{margin:2px 0}
nav{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);margin:0 -16px 20px;padding:10px 16px;display:flex;gap:18px}
nav a{color:var(--sec);text-decoration:none;font-weight:600;font-size:14px}nav a:hover{color:var(--fg)}
section{scroll-margin-top:52px}#lead{margin-top:44px;padding-top:8px}
.chip,.sevchip{display:inline-flex;align-items:center;gap:5px;padding:1px 9px;border-radius:99px;font-weight:600;font-size:12.5px;background:var(--surface);border:1px solid var(--line);color:var(--fg)}
.chip.red:before{content:"✖";color:var(--critical)}.chip.amber:before{content:"▲";color:var(--serious)}.chip.green:before{content:"✔";color:var(--good)}
.s-good{--c:var(--good)}.s-warning{--c:var(--warning)}.s-serious{--c:var(--serious)}.s-critical{--c:var(--critical)}.ic{color:var(--c,var(--sec));font-style:normal}
.kpis,.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.kpi,.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px;position:relative}.kpi b{display:block;font-size:24px}.kpi span{color:var(--mut);font-size:13px}
.tl{color:var(--sec);font-size:13px}.tv2{font-size:30px;font-weight:600;line-height:1.2;margin:2px 0}.ts{font-size:12.5px;color:var(--sec)}.ts b{color:var(--fg)}
.tile{border-top:3px solid var(--c,var(--line))}.spark{position:absolute;right:10px;top:40px;width:64px;height:20px}
ul{padding-left:20px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600}.wrap,.hscroll{overflow-x:auto}.bar{background:var(--line);border-radius:4px;height:8px;min-width:80px}.bar i{display:block;height:8px;border-radius:4px;background:var(--s1)}
.dec{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--critical);padding:10px 14px;border-radius:6px}
.flags{list-style:none;padding:0;margin:8px 0;display:grid;gap:8px}.flag{display:flex;gap:10px;background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:8px;padding:10px 12px}
.f-critical{--c:var(--critical)}.f-serious{--c:var(--serious)}.f-warning{--c:var(--warning)}.flag .ic{font-size:16px;line-height:1.5}.flag p{color:var(--sec);font-size:14px}
.sevchip{font-size:11.5px;padding:0 8px;margin-left:6px;vertical-align:1px}.ids{margin-top:6px!important}.idc{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:0 7px;margin:0 4px 4px 0;font-size:12.5px;color:var(--fg)}
.more{font-size:12.5px;color:var(--mut)}.allgood{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px}
.chgs{display:flex;flex-wrap:wrap;gap:8px 18px}.chg{font-size:14px}.chg .sub{margin-left:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:14px;margin-top:22px}.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.card h3{margin:0}.card.wide{grid-column:1/-1}.chart{width:100%;min-width:440px;height:auto;display:block;margin-top:6px}.ax{font-size:11.5px;fill:var(--mut)}.lbl{font-size:12px;fill:var(--fg)}
.xh{opacity:0}.hit:hover .xh,.hit:focus .xh{opacity:1}.hit{outline:none}
.plot{position:relative;height:150px;display:flex;justify-content:space-around;align-items:flex-end;margin-top:10px;border-bottom:1px solid var(--axis)}
.col{height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;flex:1;outline:none}.cv{font-size:12px;color:var(--sec);height:18px}
.cb{display:block;width:24px;max-width:70%;height:calc((100% - 18px)*var(--h));min-height:2px;background:var(--s250);border-radius:4px 4px 0 0}.cb.cur{background:var(--s1)}
.col:hover .cb,.col:focus .cb{filter:brightness(.88)}.avg{position:absolute;left:0;right:0;bottom:calc((100% - 18px)*var(--h));border-top:1px solid var(--mut);pointer-events:none}
.avg em{position:absolute;right:0;top:-18px;font-size:11.5px;color:var(--sec);font-style:normal;background:var(--surface);padding:0 4px}.xl{display:flex;justify-content:space-around;font-size:11.5px;color:var(--mut);margin-top:4px}.xl span{flex:1;text-align:center}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--sec);margin:8px 0}.legend span{display:inline-flex;align-items:center;gap:6px}.sw{display:inline-block;width:12px;height:12px;border-radius:3px}
.sw.blk,.sg.blk{background:var(--critical)}.sw.act,.sg.act{background:var(--s1)}.sw.todo,.sg.todo{background:var(--s250)}.sw.ok,.sg.ok{background:var(--s250)}.sw.ser,.sg.ser{background:var(--serious)}
.row{display:grid;grid-template-columns:minmax(110px,190px) 1fr 62px;align-items:center;gap:10px;padding:5px 0;outline:none}.row:hover,.row:focus{background:var(--card)}
.nm{display:flex;flex-direction:column;min-width:0;font-size:13.5px}.nm .ell{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--sec);font-size:12.5px}
.tag{font-size:11.5px;color:var(--sec)}.trk{display:flex;gap:2px;height:14px;position:relative}.sg{display:block;height:14px;min-width:3px;outline:none}.sg:last-of-type{border-radius:0 4px 4px 0}
.sg:hover,.sg:focus{filter:brightness(.88)}.thr{position:absolute;top:-4px;bottom:-4px;width:0;border-left:1px solid var(--fg);text-decoration:none;opacity:.6}
.tot{text-align:right;font-size:13px;color:var(--sec)}
.heat{display:grid;grid-template-columns:minmax(150px,1.6fr) repeat(7,minmax(54px,1fr));gap:2px;min-width:560px;margin-top:6px}.hc{font-size:11.5px;color:var(--mut);text-align:center;padding:4px 0}
.an{font-size:13px;display:flex;align-items:center;padding-right:8px;overflow-wrap:anywhere}.cell{height:30px;display:flex;align-items:center;justify-content:center;border-radius:4px;font-size:13px;font-weight:600;outline:none}
.cell.ink{color:#0b0b0b}.cell.white{color:#fff}.cell.zero{background:var(--card);color:var(--mut);font-weight:400}.cell:hover,.cell:focus{box-shadow:inset 0 0 0 2px var(--fg)}
.tv{margin-top:10px;font-size:13px}.tv summary{cursor:pointer;color:var(--sec)}
#tip{position:fixed;z-index:20;pointer-events:none;background:var(--fg);color:var(--bg);padding:6px 10px;border-radius:6px;font-size:12.5px;white-space:pre-line;max-width:320px;box-shadow:0 2px 8px rgba(0,0,0,.25)}
#tip[hidden]{display:none}
@media (max-width:560px){.chart{min-width:0}.ax,.lbl{font-size:16px}.heat{min-width:480px;grid-template-columns:minmax(110px,1.4fr) repeat(7,minmax(46px,1fr))}}
@media print{nav{display:none}.chart{min-width:0}}"""


JS = """(function(){var t=document.getElementById('tip');function at(el,x,y){t.textContent=el.getAttribute('data-tip');t.hidden=false;
var w=t.offsetWidth,h=t.offsetHeight;t.style.left=Math.max(8,Math.min(x+14,innerWidth-w-8))+'px';t.style.top=Math.max(8,Math.min(y+14,innerHeight-h-8))+'px'}
document.addEventListener('pointermove',function(ev){var el=ev.target.closest&&ev.target.closest('[data-tip]');if(el)at(el,ev.clientX,ev.clientY);else t.hidden=true});
document.addEventListener('focusin',function(ev){var el=ev.target.closest&&ev.target.closest('[data-tip]');if(el){var r=el.getBoundingClientRect();at(el,r.left,r.bottom)}});
document.addEventListener('focusout',function(){t.hidden=true})})();"""


def to_html(r: Report, title: str = "Delivery: state of play", view: str = "both") -> str:
    c = r.counts
    show_exec, show_lead = view in ("both", "exec"), view in ("both", "lead") and r.lead is not None
    kp = [(_pct(r.pct_done), "complete"), (c["active"], "in progress"), (c["blocked"], "blocked"), (c["todo"], "not started"),
          (f"{r.eta_date:%d %b}" if r.eta_date else "n/a", "forecast finish"), (f"{r.health_pct:.0f}%", "backlog health")]
    nav = "".join(f"<a href='#{i}'>{n}</a>" for i, n, on in (("overview", "Overview", show_exec), ("lead", "Data lead", show_lead)) if on)
    h = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>",
         f"<nav>{nav}</nav>" if show_exec and show_lead else ""]
    if show_exec:
        h += [f"<section id=overview><h1>{e(title)}</h1><div class=sub>As of {r.as_of:%d %b %Y} · overall <span class='chip {r.overall}'>{r.overall.upper()}</span></div>",
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
        if r.health_issues:
            h.append(f"<h2>Backlog hygiene</h2><p>{r.health_pct:.0f}% of open items have no issues. Most common: " +
                     e(", ".join(f"{k} ({n})" for k, n in r.health_issues)) + ".</p>")
        h.append("</section>")
    else:
        h.append(f"<h1 style='margin-top:20px'>{e(title)}</h1><div class=sub>As of {r.as_of:%d %b %Y}</div>")
    if show_lead:
        h.append(lead_html(r.lead, r.titles))
    return "".join(h) + f"<div id=tip hidden role=tooltip></div><script>{JS}</script></main></body></html>"
