"""Build a plan: dependency order, critical path, sprint packing, and risks."""
from __future__ import annotations

from dataclasses import dataclass, field

from .tickets import Ticket

# fallback sizing (points) for unestimated tickets, by type
DEFAULT_ESTIMATE = {"bug": 2, "task": 3, "story": 5, "feature": 8, "epic": 13}


@dataclass
class Plan:
    sprints: list[list[Ticket]] = field(default_factory=list)
    critical_path: list[str] = field(default_factory=list)
    critical_points: float = 0
    risks: list[str] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    capacity: float = 0
    points: dict[str, float] = field(default_factory=dict)
    skipped_done: int = 0


def _points(t: Ticket, risks: list[str]) -> float:
    if t.estimate is not None:
        return t.estimate
    d = DEFAULT_ESTIMATE.get(t.type, 3)
    risks.append(f"{t.id} has no estimate; assumed {d} points ({t.type})")
    return d


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan SCC; any SCC with >1 node (or a self-loop) is a cycle."""
    index, low, on, stack, out, n = {}, {}, set(), [], [], [0]

    def visit(v):
        index[v] = low[v] = n[0]; n[0] += 1
        stack.append(v); on.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w); low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop(); on.discard(w); comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in graph[v]:
                out.append(sorted(comp))

    for v in graph:
        if v not in index:
            visit(v)
    return out


def build_plan(tickets: list[Ticket], capacity: float = 20, include_done: bool = False) -> Plan:
    plan = Plan(capacity=capacity)
    all_ids = {t.id for t in tickets}
    done_ids = {t.id for t in tickets if t.done}
    plan.skipped_done = len(done_ids)
    open_ = [t for t in tickets if include_done or not t.done]
    by_id = {t.id: t for t in open_}

    # dependency edges restricted to open tickets; done / unknown deps are satisfied
    deps: dict[str, list[str]] = {}
    for t in open_:
        ds = []
        for d in dict.fromkeys(t.depends_on):
            if d not in all_ids:
                plan.risks.append(f"{t.id} depends on unknown ticket {d} (ignored)")
            elif d in by_id and d != t.id:
                ds.append(d)
            elif d == t.id:
                plan.risks.append(f"{t.id} depends on itself (ignored)")
        deps[t.id] = ds

    plan.cycles = _find_cycles(deps)
    for c in plan.cycles:
        plan.risks.append("dependency cycle: " + " -> ".join(c + [c[0]]) + " (scheduled together, order arbitrary)")
    cyc_ids = {i for c in plan.cycles for i in c}

    plan.points = {t.id: _points(t, plan.risks) for t in open_}
    for t in open_:
        if plan.points[t.id] > capacity:
            plan.risks.append(f"{t.id} ({plan.points[t.id]:g} pts) exceeds sprint capacity {capacity:g}; split it")
        if not t.assignee:
            plan.risks.append(f"{t.id} is unassigned")

    # critical path: longest estimate-weighted chain (cycle members' internal edges are ignored)
    dag = {i: [d for d in ds if not (i in cyc_ids and d in cyc_ids)] for i, ds in deps.items()}
    best: dict[str, tuple[float, str | None]] = {}

    def longest(i):
        if i not in best:
            prev = max(dag[i], key=lambda d: longest(d)[0], default=None)
            best[i] = (plan.points[i] + (longest(prev)[0] if prev else 0), prev)
        return best[i]

    import sys
    sys.setrecursionlimit(max(sys.getrecursionlimit(), len(open_) * 3 + 100))
    if open_:
        end = max(dag, key=lambda i: longest(i)[0])
        plan.critical_points = best[end][0]
        path, cur = [], end
        while cur:
            path.append(cur); cur = best[cur][1]
        plan.critical_path = path[::-1]
    crit = set(plan.critical_path)

    # priority-aware topological order (Kahn). A ticket's "level" = how many tickets depend on it, so
    # unblocking work floats up among equals.
    dependents: dict[str, int] = {i: 0 for i in deps}
    for ds in deps.values():
        for d in ds:
            dependents[d] += 1
    order_key = lambda i: (by_id[i].priority, i not in crit, -dependents[i], i)
    remaining = {i: set(d for d in ds if d not in cyc_ids or i not in cyc_ids) for i, ds in deps.items()}
    scheduled: list[str] = []
    while remaining:
        ready = sorted((i for i, r in remaining.items() if not r), key=order_key)
        if not ready:  # defensive: only reachable via cycles spanning multiple SCC edges
            ready = sorted(remaining, key=order_key)[:1]
        i = ready[0]
        scheduled.append(i); del remaining[i]
        for r in remaining.values():
            r.discard(i)

    # sprint packing: a ticket goes in the earliest sprint >= its deps' sprints with room
    sprint_of: dict[str, int] = {}
    loads: list[float] = []
    for i in scheduled:
        floor = max((sprint_of[d] for d in deps[i] if d in sprint_of), default=0)
        pts = plan.points[i]
        s = floor
        while s < len(loads) and loads[s] + pts > capacity and loads[s] > 0:
            s += 1
        if s == len(loads):
            loads.append(0)
        loads[s] += pts
        sprint_of[i] = s
        if len(plan.sprints) <= s:
            plan.sprints.extend([] for _ in range(s + 1 - len(plan.sprints)))
        plan.sprints[s].append(by_id[i])
    return plan
