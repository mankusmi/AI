"""HTML (inline SVG + CSS bars, no dependencies) and Markdown for the data-lead monitoring section."""
from __future__ import annotations

from html import escape

from .datalead import HEAT_COLS, DataLead

ICON = {"good": "✔", "warning": "◆", "serious": "▲", "critical": "✖"}
WORD = {"good": "OK", "warning": "Watch", "serious": "Serious", "critical": "Critical"}
HEAT_STEPS = [("#b7d3f6", "ink"), ("#86b6ef", "ink"), ("#5598e7", "ink"), ("#256abf", "white"), ("#104281", "white")]


def e(s) -> str:
    return escape(str(s), quote=True)


def _nice(v: float) -> float:
    if v <= 0:
        return 1
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000):
        if v <= step * 4:
            return step * 4
    return v


def _table(headers: list[str], rows: list[list]) -> str:
    h = "".join(f"<th>{e(x)}" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{e(c)}" for c in r) for r in rows)
    return f"<details class=tv><summary>Table view</summary><div class=wrap><table><tr>{h}{b}</table></div></details>"


def _sev_chip(sev: str) -> str:
    return f"<span class='sevchip s-{sev}'><span class=ic>{ICON[sev]}</span>{WORD[sev]}</span>"


def _tile(t: dict) -> str:
    spark = ""
    vals = t.get("spark")
    if vals and len(vals) > 1:
        mx = max(vals) or 1
        pts = [(4 + i * 92 / (len(vals) - 1), 24 - 20 * v / mx) for i, v in enumerate(vals)]
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        spark = (f"<svg class=spark viewBox='0 0 100 28' aria-hidden=true><polyline points='{line}' fill=none stroke='var(--s1)' "
                 f"stroke-width=2 stroke-linejoin=round stroke-linecap=round /><circle cx='{pts[-1][0]:.1f}' cy='{pts[-1][1]:.1f}' r=4 "
                 f"fill='var(--s1)' stroke='var(--surface)' stroke-width=2 /></svg>")
    return (f"<div class='tile s-{t['status']}'><div class=tl>{e(t['label'])}</div><div class=tv2>{e(t['value'])}</div>"
            f"<div class=ts><span class=ic>{ICON[t['status']]}</span><b>{WORD[t['status']]}</b> · {e(t['note'])}</div>{spark}</div>")


def _flags(dl: DataLead, titles: dict[str, str]) -> str:
    if not dl.flags:
        return "<div class=allgood><span class='ic s-good'>✔</span> No issues flagged. Nothing is blocked, quiet, over the WIP limit or missing an owner.</div>"
    out = ["<ul class=flags>"]
    for f in dl.flags:
        chips = "".join(f"<span class=idc data-tip='{e(i + ': ' + titles.get(i, ''))}' tabindex=0>{e(i)}</span>" for i in f.ids[:10])
        more = f"<span class=more>+{len(f.ids) - 10} more</span>" if len(f.ids) > 10 else ""
        out.append(f"<li class='flag f-{f.severity}'><span class='ic s-{f.severity}'>{ICON[f.severity]}</span><div>"
                   f"<b>{e(f.title)}</b> {_sev_chip(f.severity)}<p>{e(f.detail)}</p>" + (f"<p class=ids>{chips}{more}</p>" if f.ids else "") +
                   "</div></li>")
    out.append("</ul>")
    return "".join(out)


def _changes(dl: DataLead) -> str:
    c = dl.changes
    if c is None:
        return ""
    if c.empty:
        return "<h3>Since the last run</h3><p class=sub>No changes.</p>"
    groups = [("Closed", c.closed, "good"), ("Started", c.started, "good"), ("Newly blocked", c.newly_blocked, "critical"),
              ("Unblocked", c.unblocked, "good"), ("Added", c.added, "warning"), ("Reopened", c.reopened, "serious")]
    items = []
    for name, ts, sev in groups:
        if ts:
            items.append(f"<div class=chg><span class='ic s-{sev}'>{ICON[sev]}</span><b>{len(ts)}</b> {name.lower()}"
                         f"<span class=sub> · {e(', '.join(t.id for t in ts[:6]))}{' …' if len(ts) > 6 else ''}</span></div>")
    return "<h3>Since the last run</h3><div class=chgs>" + "".join(items) + "</div>"


def _burnup(dl: DataLead) -> str:
    if not dl.burnup or dl.scope_days <= 0:
        return "<p class=sub>No sized work to chart.</p>"
    W, H, L, R, T, B = 480, 220, 40, 14, 16, 30
    ymax = _nice(max(dl.scope_days, max(v for _, v in dl.burnup)))
    n = len(dl.burnup)
    step = (W - L - R) / n
    X = lambda i: L + step * (i + 0.5)
    Y = lambda v: T + (H - T - B) * (1 - v / ymax)
    g = []
    for k in range(5):
        v = ymax * k / 4
        g.append(f"<line x1={L} x2={W - R} y1={Y(v):.1f} y2={Y(v):.1f} stroke='var(--grid)' stroke-width=1 />"
                 f"<text x={L - 8} y={Y(v) + 4:.1f} text-anchor=end class=ax>{v:g}</text>")
    pts = [(X(i), Y(v)) for i, (_, v) in enumerate(dl.burnup)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pts[0][0]:.1f},{Y(0):.1f} {line} {pts[-1][0]:.1f},{Y(0):.1f}"
    xl = "".join(f"<text x={X(i):.1f} y={H - 8} text-anchor=middle class=ax>{e(lbl)}</text>" for i, (lbl, _) in enumerate(dl.burnup))
    hits = []
    for i, (lbl, v) in enumerate(dl.burnup):
        wk = dl.weekly_days[i][1]
        tip = f"Week of {lbl}\nDone to date: {v:g} days\nClosed that week: {wk:g} days\nScope today: {dl.scope_days:g} days"
        hits.append(f"<g class=hit tabindex=0 data-tip='{e(tip)}'><rect x={L + step * i:.1f} y={T} width={step:.1f} height={H - T - B} fill=transparent />"
                    f"<line class=xh x1={X(i):.1f} x2={X(i):.1f} y1={T} y2={H - B} stroke='var(--axis)' stroke-width=1 /></g>")
    sy = Y(dl.scope_days)
    svg = (f"<div class=hscroll><svg viewBox='0 0 {W} {H}' class=chart role=img aria-label='Burn-up: cumulative days of work done against scope'>"
           f"{''.join(g)}<line x1={L} x2={W - R} y1={sy:.1f} y2={sy:.1f} stroke='var(--muted)' stroke-width=2 />"
           f"<text x={W - R} y={sy - 6:.1f} text-anchor=end class=lbl>Scope today: {dl.scope_days:g} days</text>"
           f"<polygon points='{area}' fill='var(--s1)' opacity=.10 /><polyline points='{line}' fill=none stroke='var(--s1)' stroke-width=2 "
           f"stroke-linejoin=round stroke-linecap=round /><circle cx={pts[-1][0]:.1f} cy={pts[-1][1]:.1f} r=4 fill='var(--s1)' "
           f"stroke='var(--surface)' stroke-width=2 /><text x={pts[-1][0]:.1f} y={pts[-1][1] - 10:.1f} text-anchor=middle class=lbl>{dl.burnup[-1][1]:g} done</text>"
           f"{xl}{''.join(hits)}</svg></div>")
    tbl = _table(["Week of", "Closed that week (days)", "Done to date (days)"], [[wk, dl.weekly_days[i][1], v] for i, (wk, v) in enumerate(dl.burnup)])
    return svg + tbl


def _throughput(dl: DataLead) -> str:
    if not dl.weekly_days:
        return "<p class=sub>No history.</p>"
    mx = max(max(d for _, d in dl.weekly_days), dl.avg4, 1)
    cols = []
    for i, (lbl, d) in enumerate(dl.weekly_days):
        last = i == len(dl.weekly_days) - 1
        tip = f"Week of {lbl}\n{d:g} days of work closed"
        cols.append(f"<div class=col data-tip='{e(tip)}' tabindex=0><span class=cv>{d:g}</span><i class='cb{' cur' if last else ''}' style='--h:{d / mx:.3f}'></i></div>")
    labels = "".join(f"<span>{e(wk)}</span>" for wk, _ in dl.weekly_days)
    avg = f"<div class=avg style='--h:{dl.avg4 / mx:.3f}'><em>4-week average {dl.avg4:.1f}</em></div>" if dl.avg4 else ""
    return (f"<div class=plot>{avg}{''.join(cols)}</div><div class=xl>{labels}</div>" +
            _table(["Week of", "Days of work closed"], [[wk, d] for wk, d in dl.weekly_days]))


def _workload(dl: DataLead) -> str:
    people = [p for p in dl.people if p.open_days or p.active or p.blocked or p.todo][:12]
    if not people:
        return "<p class=sub>No open work.</p>"
    mx = max(p.open_days for p in people) or 1
    leg = ("<div class=legend><span><i class='sw blk'></i>Blocked</span><span><i class='sw act'></i>In progress</span>"
           "<span><i class='sw todo'></i>Not started</span></div>")
    rows = []
    for p in people:
        segs = []
        for cls, label, d, n in (("blk", "Blocked", p.blocked_days, p.blocked), ("act", "In progress", p.active_days, p.active),
                                 ("todo", "Not started", p.todo_days, p.todo)):
            if n:
                tip = f"{p.name}\n{label}: {n} item{'s' if n != 1 else ''}, {d:g} days"
                segs.append(f"<i class='sg {cls}' style='flex:0 0 max(3px,{100 * d / mx:.2f}%)' data-tip='{e(tip)}' tabindex=0></i>")
        warn = (f"<span class=tag><span class='ic s-serious'>▲</span>{p.active + p.blocked} in flight (limit {dl.wip_limit})</span>" if p.over_limit else "")
        rows.append(f"<div class=row><div class=nm><span>{e(p.name)}</span>{warn}</div><div class=trk>{''.join(segs)}</div>"
                    f"<div class=tot>{p.open_days:g}d</div></div>")
    tbl = _table(["Person", "Blocked", "In progress", "Not started", "Open days"],
                 [[p.name, f"{p.blocked} ({p.blocked_days:g}d)", f"{p.active} ({p.active_days:g}d)", f"{p.todo} ({p.todo_days:g}d)", p.open_days] for p in people])
    return leg + "".join(rows) + tbl


def _ageing(dl: DataLead) -> str:
    if not dl.aged:
        return "<p class=sub>Nothing in progress.</p>"
    thr = dl.aging_days
    top = dl.aged[:12]
    mx = max(max(a.days for a in top), 2 * thr)
    leg = ("<div class=legend><span><i class='sw ok'></i>Recent</span><span><i class='sw ser'></i>Quiet</span>"
           f"<span><i class='sw blk'></i>Stalled or blocked</span><span class=sub>line = {thr} days</span></div>")
    rows = []
    for a in top:
        sev = "critical" if a.blocked or a.days > 2 * thr else "serious" if a.days > thr else "good"
        cls = {"critical": "blk", "serious": "ser", "good": "ok"}[sev]
        tip = f"{a.ticket.id}: {a.ticket.title}\n{a.days} days since last update\n{a.ticket.assignee or 'Unassigned'}" + ("\nBlocked" if a.blocked else "")
        icon = f"<span class='ic s-{sev}'>{ICON[sev]}</span>" if sev != "good" else ""
        rows.append(f"<div class=row data-tip='{e(tip)}' tabindex=0><div class=nm><b>{e(a.ticket.id)}</b><span class=ell>{e(a.ticket.title)}</span></div>"
                    f"<div class=trk><i class='sg {cls}' style='flex:0 0 max(3px,{100 * a.days / mx:.2f}%)'></i>"
                    f"<u class=thr style='left:{100 * thr / mx:.2f}%'></u></div><div class=tot>{icon}{a.days}d</div></div>")
    tbl = _table(["Ticket", "Title", "Owner", "Days since update", "Blocked"],
                 [[a.ticket.id, a.ticket.title, a.ticket.assignee or "Unassigned", a.days, "yes" if a.blocked else ""] for a in dl.aged])
    return leg + "".join(rows) + tbl


def _heat(dl: DataLead) -> str:
    if not dl.heat_rows:
        return "<p class=sub>No problems found in any area.</p>"
    mx = max((v for _, r in dl.heat_rows for v in r.values()), default=0)
    head = "".join(f"<div class=hc>{c}</div>" for c in HEAT_COLS)
    body = []
    for area, r in dl.heat_rows:
        cells = []
        for c in HEAT_COLS:
            v = r[c]
            if v == 0 or mx == 0:
                cells.append(f"<div class='cell zero' data-tip='{e(area + chr(10) + c + ': 0')}' tabindex=0>·</div>")
            else:
                bg, fg = HEAT_STEPS[max(0, min(len(HEAT_STEPS) - 1, -int(-v * len(HEAT_STEPS) // mx) - 1))]
                cells.append(f"<div class='cell {fg}' style='background:{bg}' data-tip='{e(area + chr(10) + c + ': ' + str(v))}' tabindex=0>{v}</div>")
        body.append(f"<div class=an>{e(area)}</div>{''.join(cells)}")
    scale = ("<div class=legend><span class=sub>Items affected</span><span>1" +
             "".join(f"<i class=sw style='background:{bg}'></i>" for bg, _ in HEAT_STEPS) + f"{mx}</span></div>")
    grid = f"<div class=hscroll><div class=heat><div></div>{head}{''.join(body)}</div></div>"
    tbl = _table(["Area"] + HEAT_COLS, [[a] + [r[c] for c in HEAT_COLS] for a, r in dl.heat_rows])
    return scale + grid + tbl


def lead_html(dl: DataLead, titles: dict[str, str]) -> str:
    card = lambda title, sub, body, cls="": f"<section class='card {cls}'><h3>{title}</h3><p class=sub>{sub}</p>{body}</section>"
    return (
        "<section id=lead><h2 class=big>Data lead view</h2>"
        f"<div class=tiles>{''.join(_tile(t) for t in dl.tiles)}</div>"
        f"<h3>Needs attention</h3>{_flags(dl, titles)}{_changes(dl)}"
        "<div class=grid>" +
        card("Burn-up", "Cumulative days of work done against today's scope", _burnup(dl)) +
        card("Throughput", "Days of work closed per week; latest week highlighted", _throughput(dl)) +
        card("Workload by person", "Open days of work; ▲ marks people over the WIP limit", _workload(dl)) +
        card("Stuck and ageing work", "Days since last update for everything in progress", _ageing(dl)) +
        card("Where the problems are", "Open items with each issue, by area", _heat(dl), "wide") +
        "</div></section>")


def lead_markdown(dl: DataLead) -> str:
    o = ["## Data lead view", ""]
    o += [f"- {ICON[t['status']]} **{t['label']}:** {t['value']} ({t['note']})" for t in dl.tiles] + [""]
    o += ["### Needs attention", ""]
    o += [f"- {ICON[f.severity]} **{WORD[f.severity]}:** {f.title}. {f.detail}" + (f" ({', '.join(f.ids[:8])}{'…' if len(f.ids) > 8 else ''})" if f.ids else "")
          for f in dl.flags] or ["- ✔ No issues flagged."]
    o += [""]
    if dl.changes is not None:
        c = dl.changes
        o += ["### Since the last run", ""]
        parts = [(n, ts) for n, ts in (("closed", c.closed), ("started", c.started), ("newly blocked", c.newly_blocked), ("unblocked", c.unblocked),
                                        ("added", c.added), ("reopened", c.reopened)) if ts]
        o += [f"- {len(ts)} {n}: {', '.join(t.id for t in ts[:8])}" for n, ts in parts] or ["- No changes."]
        o += [""]
    o += ["### Workload", "", "| Person | Blocked | In progress | Not started | Open days |", "|---|---|---|---|---|"]
    o += [f"| {p.name}{' ▲' if p.over_limit else ''} | {p.blocked} | {p.active} | {p.todo} | {p.open_days:g} |" for p in dl.people[:12]]
    o += ["", "### Oldest work in progress", ""]
    o += [f"- {a.ticket.id} {a.ticket.title}: {a.days} days{' (blocked)' if a.blocked else ''}" for a in dl.aged[:8]] or ["- None."]
    return "\n".join(o) + "\n"
