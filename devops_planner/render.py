"""Render a Plan as Markdown or JSON."""
from __future__ import annotations

import json

from .planner import Plan

PRIO = ["critical", "high", "medium", "low", "lowest"]


def to_json(plan: Plan) -> str:
    return json.dumps({
        "capacity": plan.capacity,
        "critical_path": plan.critical_path,
        "critical_path_points": plan.critical_points,
        "cycles": plan.cycles,
        "risks": plan.risks,
        "sprints": [{"sprint": n + 1, "points": sum(plan.points[t.id] for t in s),
                     "tickets": [{"id": t.id, "title": t.title, "type": t.type, "priority": PRIO[min(t.priority, 4)],
                                  "points": plan.points[t.id], "assignee": t.assignee, "depends_on": t.depends_on}
                                 for t in s]} for n, s in enumerate(plan.sprints)],
    }, indent=2)


def to_markdown(plan: Plan, title: str = "Delivery plan") -> str:
    total = sum(plan.points.values())
    out = [f"# {title}", "",
           f"- **Open tickets:** {sum(len(s) for s in plan.sprints)} ({plan.skipped_done} already done, skipped)",
           f"- **Total effort:** {total:g} pts across {len(plan.sprints)} sprint(s) at capacity {plan.capacity:g}",
           f"- **Critical path:** {' → '.join(plan.critical_path) or 'n/a'} ({plan.critical_points:g} pts)", ""]
    crit = set(plan.critical_path)
    for n, s in enumerate(plan.sprints, 1):
        out += [f"## Sprint {n} — {sum(plan.points[t.id] for t in s):g} pts", "",
                "| Ticket | Title | Type | Priority | Pts | Owner | Needs |", "|---|---|---|---|---|---|---|"]
        for t in s:
            mark = " ⚑" if t.id in crit else ""
            out.append(f"| {t.id}{mark} | {t.title} | {t.type} | {PRIO[min(t.priority, 4)]} | {plan.points[t.id]:g} | "
                       f"{t.assignee or '—'} | {', '.join(t.depends_on) or '—'} |")
        out.append("")
    if crit:
        out += ["⚑ = on the critical path", ""]
    if plan.risks:
        out += ["## Risks & data issues", ""] + [f"- {r}" for r in plan.risks] + [""]
    return "\n".join(out)
