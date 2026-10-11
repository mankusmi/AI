"""Review a backlog: classify tickets as epic / feature / user story by size and structure, and flag hygiene problems.

Definitions (all thresholds are configurable):
  user story - a few days of work          (<= story_max days)
  feature    - fits in one sprint          (<= feature_max days)
  epic       - roughly a quarter of work   (<= epic_max days; beyond that it should be split)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .tickets import Ticket

LEVEL = {"story": 1, "feature": 2, "epic": 3}
NAME = {1: "story", 2: "feature", 3: "epic"}


@dataclass
class Config:
    story_max: float = 5
    feature_max: float = 10
    epic_max: float = 65
    tolerance: float = 0.25        # a ticket this far outside its range is still left alone
    days_per_point: float = 1
    stale_days: int = 60
    min_area_depth: int = 2


@dataclass
class Finding:
    id: str
    rule: str
    message: str
    severity: str = "warn"          # warn | info
    suggestion: str = ""


@dataclass
class Review:
    findings: list[Finding] = field(default_factory=list)
    sizes: dict[str, float | None] = field(default_factory=dict)
    suggested_type: dict[str, str] = field(default_factory=dict)   # only where it differs from current
    suggested_parent: dict[str, list[str]] = field(default_factory=dict)
    area_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    tickets: dict[str, Ticket] = field(default_factory=dict)


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _fit(days: float, cfg: Config) -> int:
    return 1 if days <= cfg.story_max else 2 if days <= cfg.feature_max else 3


def _within(days: float, level: int, cfg: Config) -> bool:
    lo, hi = {1: (0, cfg.story_max), 2: (cfg.story_max, cfg.feature_max), 3: (cfg.feature_max, cfg.epic_max)}[level]
    return lo * (1 - cfg.tolerance) < days <= hi * (1 + cfg.tolerance) or (level == 1 and days <= hi * (1 + cfg.tolerance))


def review(tickets: list[Ticket], cfg: Config | None = None, now: datetime | None = None) -> Review:
    cfg = cfg or Config()
    now = now or datetime.now()
    r = Review(tickets={t.id: t for t in tickets})
    by_id = r.tickets
    children: dict[str, list[str]] = {t.id: [] for t in tickets}
    add = lambda *a, **k: r.findings.append(Finding(*a, **k))

    for t in tickets:
        if t.parent and t.parent not in by_id:
            add(t.id, "broken-parent", f"parent {t.parent} not found in the export")
        elif t.parent:
            children[t.parent].append(t.id)

    # sizes: own estimate, else rolled-up from descendants
    def size(i: str, seen=()) -> float | None:
        if i in r.sizes:
            return r.sizes[i]
        t = by_id[i]
        own = t.days if t.days is not None else (t.estimate * cfg.days_per_point if t.estimate is not None else None)
        if own is None and i not in seen:
            parts = [size(c, seen + (i,)) for c in children[i]]
            parts = [p for p in parts if p is not None]
            own = sum(parts) if parts else None
        r.sizes[i] = own
        return own

    for t in tickets:
        size(t.id)

    # --- classification (epic / feature / story only; bugs and tasks are left alone) ---
    for t in tickets:
        lvl = LEVEL.get(t.type)
        if lvl is None:
            continue
        d = r.sizes[t.id]
        kids = [LEVEL[by_id[c].type] for c in children[t.id] if by_id[c].type in LEVEL]
        floor = max(kids, default=0) + 1 if kids else 1     # a container must sit above its children
        if d is None:
            add(t.id, "unsized", f"{t.type} has no estimate and no sized children; cannot judge scope", "info")
            suggest = max(lvl, floor) if floor > lvl else None
            why = f"it contains {NAME[floor - 1]}s" if suggest else ""
        else:
            fit = max(_fit(d, cfg), floor)
            suggest = fit if not _within(d, lvl, cfg) or floor > lvl else None
            if d > cfg.epic_max * (1 + cfg.tolerance):
                add(t.id, "too-big", f"{d:g} days is more than a quarter; split it", suggestion="split into several epics")
            why = f"{d:g}d of work" + (f" and it contains {NAME[floor - 1]}s" if floor > lvl else "")
        if suggest and suggest != lvl:
            r.suggested_type[t.id] = NAME[suggest]
            add(t.id, "wrong-type", f"is a {t.type} but looks like a {NAME[suggest]} ({why})",
                suggestion=f"change type to {NAME[suggest]}")

    # --- hierarchy ---
    for t in tickets:
        lvl = LEVEL.get(t.type)
        p = by_id.get(t.parent)
        if p is not None:
            plvl = LEVEL.get(p.type)
            if lvl and plvl and plvl <= lvl:
                add(t.id, "bad-hierarchy", f"{t.type} sits under {p.type} {p.id}; a parent must be a larger item")
            if p.done and not t.done:
                add(t.id, "closed-parent", f"parent {p.id} is closed but this is still open")
            pa, ca = p.area_parts, t.area_parts
            if pa and ca and ca[:len(pa)] != pa:
                add(t.id, "area-mismatch", f"area '{t.area}' is outside parent {p.id}'s area '{p.area}'")
        elif not t.parent and lvl and lvl < 3 and not t.done:
            cands = sorted((c for c in tickets if LEVEL.get(c.type) == lvl + 1 and not c.done),
                           key=lambda c: -_shared(t.area_parts, c.area_parts))
            cands = [c.id for c in cands if _shared(t.area_parts, c.area_parts) > 0][:3]
            r.suggested_parent[t.id] = cands
            add(t.id, "orphan", f"{t.type} has no parent {NAME[lvl + 1]}",
                suggestion=("candidates in the same area: " + ", ".join(cands)) if cands else "")
        if lvl and lvl > 1 and len(children[t.id]) == 1:
            add(t.id, "thin-container", f"{t.type} has only one child ({children[t.id][0]}); merge or add scope", "info")
        if lvl and lvl > 1 and not children[t.id] and not t.done:
            add(t.id, "empty-container", f"{t.type} has no children; break it down", "info")

    # --- areas ---
    for t in tickets:
        parts = t.area_parts
        if not parts:
            add(t.id, "no-area", "no area path")
        elif len(parts) < cfg.min_area_depth:
            add(t.id, "shallow-area", f"area '{t.area}' is too high in the hierarchy (depth {len(parts)})")
        key = " \\ ".join(parts) or "(none)"
        r.area_counts.setdefault(key, {}).setdefault(t.type, 0)
        r.area_counts[key][t.type] += 1

    # --- hygiene ---
    seen: dict[tuple, str] = {}
    for t in tickets:
        if len(t.title) < 8:
            add(t.id, "weak-title", "title is empty or too short to be meaningful")
        k = (_norm_title(t.title), tuple(t.area_parts))
        if t.title and k in seen:
            add(t.id, "duplicate", f"same title and area as {seen[k]}", suggestion="merge or close one")
        seen.setdefault(k, t.id)
        if t.done:
            continue
        if t.changed and (now - t.changed).days > cfg.stale_days:
            add(t.id, "stale", f"not touched for {(now - t.changed).days} days", suggestion="close, or re-confirm still wanted")
        if t.type == "story" and not t.assignee and t.state.lower() in ("active", "in progress", "doing"):
            add(t.id, "active-unassigned", "in progress but nobody is assigned")
    return r


def _shared(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x.lower() != y.lower():
            break
        n += 1
    return n
