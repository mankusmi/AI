"""Day-to-day monitoring analytics for a data lead: flags, workload, ageing, burn-up, hygiene heatmap, and change since last run."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .tickets import Ticket

SEV_ORDER = {"critical": 0, "serious": 1, "warning": 2}
HEAT_COLS = ["Blocked", "Aged WIP", "Unassigned", "Unsized", "Stale", "Orphan", "Mis-typed"]


@dataclass
class Flag:
    severity: str                 # critical | serious | warning
    title: str
    detail: str
    ids: list[str] = field(default_factory=list)


@dataclass
class Person:
    name: str
    active: int = 0
    blocked: int = 0
    todo: int = 0
    active_days: float = 0
    blocked_days: float = 0
    todo_days: float = 0
    over_limit: bool = False

    @property
    def open_days(self) -> float:
        return self.active_days + self.blocked_days + self.todo_days


@dataclass
class Changes:
    closed: list[Ticket] = field(default_factory=list)
    started: list[Ticket] = field(default_factory=list)
    newly_blocked: list[Ticket] = field(default_factory=list)
    unblocked: list[Ticket] = field(default_factory=list)
    added: list[Ticket] = field(default_factory=list)
    reopened: list[Ticket] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any((self.closed, self.started, self.newly_blocked, self.unblocked, self.added, self.reopened))


@dataclass
class Aged:
    ticket: Ticket
    days: int
    blocked: bool


@dataclass
class DataLead:
    flags: list[Flag] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    aged: list[Aged] = field(default_factory=list)
    aging_days: int = 14
    wip_limit: int = 3
    burnup: list[tuple[str, float]] = field(default_factory=list)       # week-end label, cumulative days done
    scope_days: float = 0
    weekly_days: list[tuple[str, float]] = field(default_factory=list)
    avg4: float = 0
    last_week: float | None = None
    heat_rows: list[tuple[str, dict[str, int]]] = field(default_factory=list)
    tiles: list[dict] = field(default_factory=list)
    changes: Changes | None = None


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def person_name(t: Ticket) -> str:
    n = re.sub(r"\s*<[^>]*>", "", t.assignee).strip()
    return n or "Unassigned"


def diff(prev: list[Ticket], cur: list[Ticket], cat) -> Changes:
    p = {t.id: t for t in prev}
    ch = Changes()
    for t in cur:
        o = p.get(t.id)
        if o is None:
            ch.added.append(t)
            continue
        a, b = cat(o), cat(t)
        if a == b:
            continue
        if b == "done":
            ch.closed.append(t)
        elif a == "done":
            ch.reopened.append(t)
        elif b == "blocked":
            ch.newly_blocked.append(t)
        elif a == "blocked":
            ch.unblocked.append(t)
        elif b == "active" and a == "todo":
            ch.started.append(t)
    return ch


def build_datalead(leaves: list[Ticket], all_tickets: list[Ticket], cats: dict[str, str], rv, weekly: list[tuple[str, int, float]],
                   weeks_starts: list[datetime], now: datetime, wip_limit: int, aging_days: int,
                   previous: list[Ticket] | None, cat) -> DataLead:
    w = lambda t: rv.sizes.get(t.id) or 0
    dl = DataLead(aging_days=aging_days, wip_limit=wip_limit)
    open_leaves = [t for t in leaves if cats[t.id] != "done"]
    wip = [t for t in leaves if cats[t.id] in ("active", "blocked")]

    # --- people ---
    people: dict[str, Person] = {}
    for t in open_leaves:
        p = people.setdefault(person_name(t), Person(person_name(t)))
        c = cats[t.id]
        setattr(p, c, getattr(p, c) + 1)
        setattr(p, c + "_days", getattr(p, c + "_days") + w(t))
    for p in people.values():
        p.over_limit = p.name != "Unassigned" and (p.active + p.blocked) > wip_limit
    dl.people = sorted(people.values(), key=lambda p: -p.open_days)

    # --- ageing (active + blocked work, oldest first) ---
    no_date = []
    for t in wip:
        if t.changed is None:
            no_date.append(t)
            continue
        dl.aged.append(Aged(t, max((now - t.changed).days, 0), cats[t.id] == "blocked"))
    dl.aged.sort(key=lambda a: -a.days)

    # --- flow ---
    dl.weekly_days = [(lbl, d) for lbl, _, d in weekly]
    dl.last_week = weekly[-1][2] if weekly else None
    prior = [d for _, _, d in weekly[-5:-1]]
    dl.avg4 = sum(prior) / len(prior) if prior else 0
    dl.scope_days = sum(w(t) for t in leaves)
    base = sum(w(t) for t in leaves if cats[t.id] == "done" and t.closed and weeks_starts and t.closed < weeks_starts[0])
    cum = base
    for (lbl, _, d) in weekly:
        cum += d
        dl.burnup.append((lbl, cum))

    # --- findings by id ---
    by_rule: dict[str, set[str]] = {}
    for f in rv.findings:
        by_rule.setdefault(f.rule, set()).add(f.id)
    open_ids = {t.id for t in all_tickets if cats[t.id] != "done"}
    blocked = [t for t in leaves if cats[t.id] == "blocked"]
    aged_over = [a for a in dl.aged if a.days > aging_days]
    unassigned_active = [t for t in leaves if cats[t.id] == "active" and not t.assignee.strip()]
    crit_bugs = [t for t in open_leaves if t.type == "bug" and t.priority == 0]
    stale = by_rule.get("stale", set()) & open_ids
    unsized = [t for t in open_leaves if not rv.sizes.get(t.id)]
    orphans = by_rule.get("orphan", set()) & open_ids
    mistyped = {i for i in rv.suggested_type if i in open_ids}

    # --- flags ---
    F = dl.flags
    if blocked:
        oldest = max(((now - t.changed).days for t in blocked if t.changed), default=None)
        F.append(Flag("critical", f"{len(blocked)} blocked", "Nothing moves until these are unblocked" +
                      (f"; the longest has been untouched for {oldest} days." if oldest is not None else "."), [t.id for t in blocked]))
    if crit_bugs:
        F.append(Flag("critical", f"{len(crit_bugs)} critical bug{'s' if len(crit_bugs) != 1 else ''} open", "Critical priority and not closed.",
                      [t.id for t in crit_bugs]))
    very_old = [a for a in aged_over if a.days > 2 * aging_days and not a.blocked]
    if very_old:
        F.append(Flag("critical", f"{len(very_old)} in progress for over {2 * aging_days} days without an update",
                      "Probably stalled or forgotten.", [a.ticket.id for a in very_old]))
    mid = [a for a in aged_over if a not in very_old and not a.blocked]
    if mid:
        F.append(Flag("serious", f"{len(mid)} in progress and gone quiet (over {aging_days} days)", "Check they are still moving.", [a.ticket.id for a in mid]))
    over = [p for p in dl.people if p.over_limit]
    if over:
        F.append(Flag("serious", f"{len(over)} {'person is' if len(over) == 1 else 'people are'} over the WIP limit of {wip_limit}",
                      ", ".join(f"{p.name} ({p.active + p.blocked})" for p in over) + ". Too much in flight slows everything."))
    if unassigned_active:
        F.append(Flag("serious", f"{len(unassigned_active)} in progress with no owner", "Nobody is accountable.", [t.id for t in unassigned_active]))
    if len(weekly) >= 3 and dl.avg4 > 0 and dl.last_week is not None and dl.last_week < 0.5 * dl.avg4:
        F.append(Flag("serious", "Throughput dropped", f"Last full week closed {dl.last_week:g} days of work against a 4-week average of {dl.avg4:.1f}."))
    if dl.avg4 == 0 and open_leaves and weekly and not any(d for _, _, d in weekly):
        F.append(Flag("serious", "Nothing closed recently", "No work closed in the history window, so no forecast is possible."))
    done_no_date = [t for t in leaves if cats[t.id] == "done" and t.closed is None]
    if done_no_date:
        F.append(Flag("warning", f"{_n(len(done_no_date), 'closed item')} with no Closed Date", "They are missing from throughput and the forecast.",
                      [t.id for t in done_no_date]))
    if no_date:
        F.append(Flag("warning", f"{_n(len(no_date), 'in-progress item')} with no Changed Date", "Their ageing can't be checked.", [t.id for t in no_date]))
    if open_leaves and len(unsized) / len(open_leaves) > 0.25:
        F.append(Flag("warning", f"{len(unsized)} of {len(open_leaves)} open items {'has' if len(unsized) == 1 else 'have'} no estimate", "Progress and forecast are less reliable.", [t.id for t in unsized]))
    if stale:
        F.append(Flag("warning", f"{_n(len(stale), 'item')} untouched for 60+ days", "Close them or re-confirm they are wanted.", sorted(stale)))
    if orphans:
        F.append(Flag("warning", f"{_n(len(orphans), 'item')} with no parent", "Their work doesn't roll up to any feature or epic.", sorted(orphans)))
    if mistyped:
        F.append(Flag("warning", f"{_n(len(mistyped), 'item')} that look{'s' if len(mistyped) == 1 else ''} like the wrong type", "Epic, feature and story sizes don't match the definitions.", sorted(mistyped)))
    F.sort(key=lambda f: SEV_ORDER[f.severity])

    # --- heatmap rows: area x issue class (open leaf work) ---
    aged_ids = {a.ticket.id for a in aged_over}
    rows: dict[str, dict[str, int]] = {}
    for t in open_leaves:
        key = " \\ ".join(t.area_parts[:3]) or "(no area)"
        r = rows.setdefault(key, {c: 0 for c in HEAT_COLS})
        flags = {"Blocked": cats[t.id] == "blocked", "Aged WIP": t.id in aged_ids, "Unassigned": cats[t.id] == "active" and not t.assignee.strip(),
                 "Unsized": not rv.sizes.get(t.id), "Stale": t.id in stale, "Orphan": t.id in orphans, "Mis-typed": t.id in mistyped}
        for c, on in flags.items():
            r[c] += int(on)
    dl.heat_rows = sorted(((k, v) for k, v in rows.items() if sum(v.values())), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:12]

    # --- tiles ---
    n_wip = sum(cats[t.id] == "active" for t in leaves)
    tot_open = len(open_ids)
    bad = {f.id for f in rv.findings if f.severity == "warn" and f.id in open_ids}
    health = 100 * (1 - len(bad) / tot_open) if tot_open else 100
    ok = lambda cond, bad_level="serious": "good" if not cond else bad_level
    dl.tiles = [
        {"label": "In progress", "value": str(n_wip), "status": "good" if len(over) == 0 else "serious", "note": f"WIP limit {wip_limit} per person"},
        {"label": "Blocked", "value": str(len(blocked)), "status": ok(blocked, "critical"), "note": "work waiting on someone"},
        {"label": f"Quiet over {aging_days}d", "value": str(len(aged_over)), "status": ok(aged_over), "note": "in progress, no update"},
        {"label": "Closed last week", "value": f"{dl.last_week:g}d" if dl.last_week is not None else "n/a",
         "status": "serious" if any(f.title == "Throughput dropped" for f in F) else "good",
         "note": f"4-week avg {dl.avg4:.1f}d", "spark": [d for _, d in dl.weekly_days]},
        {"label": "No owner", "value": str(len(unassigned_active)), "status": ok(unassigned_active), "note": "in progress, unassigned"},
        {"label": "Backlog health", "value": f"{health:.0f}%", "status": "good" if health >= 85 else "warning" if health >= 70 else "serious",
         "note": "open items without issues"},
    ]
    if previous is not None:
        dl.changes = diff(previous, all_tickets, cat)
    return dl
