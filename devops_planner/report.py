"""State-of-play report for a head of delivery: progress, RAG per epic, flow, forecast, and what needs a decision."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .tickets import Ticket
from .datalead import DataLead, build_datalead
from .triage import Config, review

ACTIVE = {"active", "in progress", "doing", "committed", "in review", "in development", "resolved", "testing"}
RAG_ORDER = {"red": 0, "amber": 1, "green": 2}


def category(t: Ticket) -> str:
    s = t.state.lower()
    if t.done:
        return "done"
    if s == "blocked" or any(x.lower() == "blocked" for x in t.labels):
        return "blocked"
    return "active" if s in ACTIVE else "todo"


@dataclass
class Row:
    id: str
    title: str
    total: int = 0
    done: int = 0
    pct: float | None = None
    active: int = 0
    blocked: int = 0
    unsized: int = 0
    last_activity: datetime | None = None
    rag: str = "green"
    reasons: list[str] = field(default_factory=list)


@dataclass
class Report:
    as_of: datetime
    overall: str = "green"
    headlines: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    pct_done: float | None = None
    rows: list[Row] = field(default_factory=list)
    group_by: str = "epic"
    weekly: list[tuple[str, int, float]] = field(default_factory=list)   # week start, tickets closed, days closed
    remaining_days: float = 0
    eta_weeks: float | None = None
    eta_date: datetime | None = None
    blocked: list[Ticket] = field(default_factory=list)
    aging: list[tuple[Ticket, int]] = field(default_factory=list)
    health_pct: float = 100
    health_issues: list[tuple[str, int]] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    lead: DataLead | None = None
    titles: dict[str, str] = field(default_factory=dict)


def build_report(tickets: list[Ticket], cfg: Config | None = None, as_of: datetime | None = None,
                 weeks: int = 6, aging_days: int = 14, previous: list[Ticket] | None = None, wip_limit: int = 3) -> Report:
    cfg = cfg or Config()
    now = as_of or datetime.now()
    rv = review(tickets, cfg, now)
    by_id = {t.id: t for t in tickets}
    kids: dict[str, list[str]] = {t.id: [] for t in tickets}
    for t in tickets:
        if t.parent in by_id:
            kids[t.parent].append(t.id)
    leaves = [t for t in tickets if not kids[t.id]]
    rep = Report(as_of=now)
    cats = {t.id: category(t) for t in tickets}
    rep.counts = dict(Counter(cats[t.id] for t in leaves))
    for k in ("done", "active", "blocked", "todo"):
        rep.counts.setdefault(k, 0)

    def weight(t):
        return rv.sizes.get(t.id)

    def pct(ts):
        w = [(weight(t), cats[t.id] == "done") for t in ts if weight(t)]
        if w:
            return 100 * sum(x for x, d in w if d) / sum(x for x, _ in w)
        return 100 * sum(cats[t.id] == "done" for t in ts) / len(ts) if ts else None

    rep.pct_done = pct(leaves)

    def descendants(i):
        out = []
        for c in kids[i]:
            out += [c] + descendants(c)
        return out

    # --- group rows: epics, else top two area levels ---
    epics = [t for t in tickets if t.type == "epic" and not t.done]
    groups: list[tuple[str, str, list[Ticket]]] = []
    if epics:
        for e in epics:
            ds = [by_id[d] for d in descendants(e.id)]
            groups.append((e.id, e.title, [d for d in ds if not kids[d.id]] or [e]))
    else:
        rep.group_by = "area"
        by_area: dict[str, list[Ticket]] = {}
        for t in leaves:
            by_area.setdefault(" \\ ".join(t.area_parts[:2]) or "(no area)", []).append(t)
        groups = [(a, a, ts) for a, ts in by_area.items()]
    for gid, title, ts in groups:
        row = Row(gid, title, total=len(ts), done=sum(cats[t.id] == "done" for t in ts), pct=pct(ts),
                  active=sum(cats[t.id] == "active" for t in ts), blocked=sum(cats[t.id] == "blocked" for t in ts),
                  unsized=sum(1 for t in ts if not weight(t) and cats[t.id] != "done"))
        row.last_activity = max((t.closed or t.changed for t in ts if (t.closed or t.changed)), default=None)
        openn = row.total - row.done
        if row.blocked:
            row.rag = "red"; row.reasons.append(f"{row.blocked} blocked")
        if openn and row.last_activity and (now - row.last_activity).days > 30:
            row.rag = "red"; row.reasons.append(f"no movement for {(now - row.last_activity).days} days")
        if any(t.priority == 0 and t.type == "bug" and cats[t.id] != "done" for t in ts):
            row.rag = "red"; row.reasons.append("open critical bug")
        if row.rag == "green":
            if openn and row.unsized / openn > 0.25:
                row.rag = "amber"; row.reasons.append(f"{row.unsized} of {openn} open items unsized")
            if openn and row.active == 0 and row.done < row.total:
                row.rag = "amber"; row.reasons.append("nothing in progress")
        rep.rows.append(row)
    rep.rows.sort(key=lambda r: (RAG_ORDER[r.rag], -r.total))
    open_rows = [r for r in rep.rows if r.total - r.done]
    if open_rows:
        red = sum(r.rag == "red" for r in open_rows) / len(open_rows)
        amber = sum(r.rag == "amber" for r in open_rows) / len(open_rows)
        rep.overall = "red" if red >= 0.25 else "amber" if red or amber >= 0.25 else "green"

    # --- flow ---
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_starts = []
    for w in range(weeks, 0, -1):          # completed weeks only; the current partial week would drag the average down
        a, b = start - timedelta(weeks=w), start - timedelta(weeks=w - 1)
        week_starts.append(a)
        cl = [t for t in leaves if t.closed and a <= t.closed < b]
        rep.weekly.append((a.strftime("%d %b"), len(cl), sum(weight(t) or 0 for t in cl)))
    rep.remaining_days = sum(weight(t) or 0 for t in leaves if cats[t.id] != "done")
    recent = [d for _, _, d in rep.weekly[-4:]]
    avg = sum(recent) / len(recent) if recent else 0
    if avg > 0 and rep.remaining_days:
        rep.eta_weeks = rep.remaining_days / avg
        rep.eta_date = now + timedelta(weeks=rep.eta_weeks)
    rep.blocked = [t for t in leaves if cats[t.id] == "blocked"]
    rep.aging = sorted(((t, (now - t.changed).days) for t in leaves
                        if cats[t.id] == "active" and t.changed and (now - t.changed).days > aging_days), key=lambda x: -x[1])

    # --- backlog health ---
    open_ids = {t.id for t in tickets if not t.done}
    bad = {f.id for f in rv.findings if f.severity == "warn" and f.id in open_ids}
    rep.health_pct = 100 * (1 - len(bad) / len(open_ids)) if open_ids else 100
    rep.health_issues = Counter(f.rule for f in rv.findings if f.id in open_ids and f.severity == "warn").most_common(5)

    # --- decisions / headlines ---
    for r in rep.rows:
        if r.rag == "red":
            rep.decisions.append(f"{r.id} {r.title}: {', '.join(r.reasons)}")
    n_wrong = sum(1 for i in rv.suggested_type if i in open_ids)
    if n_wrong:
        rep.decisions.append(f"{n_wrong} open item{'s' if n_wrong != 1 else ''} mis-typed (epic/feature/story); reclassify before relying on roll-ups")
    n_split = sum(1 for f in rv.findings if f.rule in ("split", "too-big") and f.id in open_ids)
    if n_split:
        rep.decisions.append(f"{n_split} item{'s' if n_split != 1 else ''} too large for their type; split")
    done_n, tot = rep.counts["done"], len(leaves)
    rep.headlines.append(f"{rep.pct_done:.0f}% of the work is complete ({done_n} of {tot} work items); "
                         f"{rep.counts['active']} in progress, {rep.counts['blocked']} blocked, {rep.counts['todo']} not started."
                         if rep.pct_done is not None else "No work items found.")
    if rep.eta_date:
        rep.headlines.append(f"At the last-4-week pace ({avg:.1f} days of work closed per week), the {rep.remaining_days:g} days remaining "
                             f"finish around {rep.eta_date:%d %b %Y} (~{rep.eta_weeks:.0f} weeks).")
    elif rep.remaining_days:
        rep.headlines.append(f"{rep.remaining_days:g} days of work remain but nothing closed recently, so no forecast is possible.")
    if open_rows:
        rep.headlines.append(f"{sum(r.rag == 'red' for r in open_rows)} of {len(open_rows)} open {rep.group_by}s are red, "
                             f"{sum(r.rag == 'amber' for r in open_rows)} amber.")
    rep.headlines.append(f"Backlog hygiene: {rep.health_pct:.0f}% of open items are free of problems.")
    rep.titles = {t.id: t.title for t in tickets}
    rep.lead = build_datalead(leaves, tickets, cats, rv, rep.weekly, week_starts, now, wip_limit, aging_days, previous, category)
    return rep
