"""Render a triage Review as Markdown / JSON / CSV of suggested changes."""
from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict

from .triage import Review


def _by_rule(r: Review) -> dict[str, list]:
    d = defaultdict(list)
    for f in r.findings:
        d[f.rule].append(f)
    return d


def to_markdown(r: Review, title: str = "Backlog review") -> str:
    by = _by_rule(r)
    types = Counter(t.type for t in r.tickets.values())
    out = [f"# {title}", "", f"{len(r.tickets)} tickets ({', '.join(f'{n} {k}' for k, n in types.most_common())}); "
           f"{len(r.findings)} findings on {len({f.id for f in r.findings})} tickets; "
           f"{len(r.suggested_type)} type changes suggested.", ""]
    if r.suggested_type:
        out += ["## Suggested reclassification", "", "| Ticket | Title | Now | Suggested | Size (days) | Why |", "|---|---|---|---|---|---|"]
        for f in by.get("wrong-type", []):
            t = r.tickets[f.id]
            sz = r.sizes[f.id]
            out.append(f"| {t.id} | {t.title} | {t.type} | **{r.suggested_type[f.id]}** | {'' if sz is None else f'{sz:g}'} | {f.message.split('(', 1)[-1].rstrip(')')} |")
        out.append("")
    titles = {"too-big": "Larger than a quarter", "bad-hierarchy": "Hierarchy violations", "closed-parent": "Open work under closed parents",
              "broken-parent": "Parent not found", "orphan": "Orphans (no parent)", "area-mismatch": "Area outside parent's area",
              "no-area": "No area path", "shallow-area": "Area too shallow", "duplicate": "Possible duplicates", "stale": "Stale",
              "active-unassigned": "In progress, unassigned", "weak-title": "Weak titles", "thin-container": "Single-child containers",
              "empty-container": "Containers with no children", "unsized": "Not sized"}
    for rule, head in titles.items():
        fs = by.get(rule)
        if not fs:
            continue
        out += [f"## {head} ({len(fs)})", ""]
        for f in fs:
            t = r.tickets[f.id]
            out.append(f"- **{f.id}** {t.title}: {f.message}" + (f" — _{f.suggestion}_" if f.suggestion else ""))
        out.append("")
    out += ["## Areas", "", "| Area | " + " | ".join(["epic", "feature", "story", "other"]) + " |", "|---|---|---|---|---|"]
    for area, c in sorted(r.area_counts.items()):
        other = sum(n for k, n in c.items() if k not in ("epic", "feature", "story"))
        out.append(f"| {area} | {c.get('epic', 0)} | {c.get('feature', 0)} | {c.get('story', 0)} | {other} |")
    return "\n".join(out) + "\n"


def to_json(r: Review) -> str:
    return json.dumps({"findings": [vars(f) for f in r.findings], "suggested_type": r.suggested_type,
                       "suggested_parent": r.suggested_parent, "sizes": r.sizes}, indent=2)


def changes_csv(r: Review) -> str:
    """One row per ticket needing a change, ready for a bulk update."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Title", "Current Type", "Suggested Type", "Suggested Parent", "Issues"])
    ids = {f.id for f in r.findings}
    for i in sorted(ids):
        t = r.tickets[i]
        w.writerow([i, t.title, t.type, r.suggested_type.get(i, ""), ";".join(r.suggested_parent.get(i, [])[:1]),
                    " | ".join(f"{f.rule}: {f.message}" for f in r.findings if f.id == i)])
    return buf.getvalue()
