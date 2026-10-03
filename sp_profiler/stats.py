"""Descriptive statistics (stdlib only)."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Iterable


def describe(values: Iterable[float]) -> dict:
    v = sorted(x for x in values if x is not None)
    if not v:
        return {"count": 0}
    q = statistics.quantiles(v, n=100, method="inclusive") if len(v) > 1 else [v[0]] * 99
    return {
        "count": len(v), "sum": sum(v), "min": v[0], "p25": q[24], "median": statistics.median(v),
        "p75": q[74], "p90": q[89], "p99": q[98], "max": v[-1],
        "mean": statistics.fmean(v), "std": statistics.stdev(v) if len(v) > 1 else 0.0,
    }


def group_describe(rows: list[dict], key: str, value: str) -> dict[str, dict]:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r[value])
    return {k: describe(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def counts(values: Iterable) -> dict:
    return dict(Counter(values).most_common())
