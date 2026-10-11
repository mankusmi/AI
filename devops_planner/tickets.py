"""Load tickets from JSON or CSV exports (Azure DevOps, Jira, GitHub, or hand-written)."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# canonical field -> accepted column/key names (lower-cased, spaces/underscores ignored)
ALIASES = {
    "id": ["id", "key", "issuekey", "workitemid", "number"],
    "title": ["title", "summary", "name"],
    "type": ["type", "workitemtype", "issuetype"],
    "priority": ["priority", "severity"],
    "estimate": ["estimate", "storypoints", "effort", "points", "originalestimate", "size"],
    "state": ["state", "status"],
    "assignee": ["assignee", "assignedto", "owner"],
    "labels": ["labels", "tags"],
    "area": ["area", "areapath", "component"],
    "parent": ["parent", "parentid", "parentworkitemid", "parentkey", "epiclink"],
    "changed": ["changeddate", "updated", "lastupdated", "statechangedate", "modified"],
    "days": ["days", "durationdays", "effortdays"],
    "depends_on": ["dependson", "dependencies", "predecessors", "blockedby", "blocks_inverse"],
}
DONE_STATES = {"done", "closed", "resolved", "completed", "removed", "cancelled", "canceled"}
PRIORITY_RANK = {"critical": 0, "blocker": 0, "highest": 0, "urgent": 0, "p0": 0, "1": 0,
                 "high": 1, "p1": 1, "2": 1,
                 "medium": 2, "normal": 2, "p2": 2, "3": 2,
                 "low": 3, "p3": 3, "4": 3, "lowest": 4, "p4": 4, "5": 4}


@dataclass
class Ticket:
    id: str
    title: str = ""
    type: str = "task"
    priority: int = 2
    estimate: float | None = None
    state: str = "todo"
    assignee: str = ""
    labels: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    area: str = ""
    parent: str = ""
    changed: datetime | None = None
    days: float | None = None

    @property
    def area_parts(self) -> list[str]:
        return [p.strip() for p in re.split(r"[\\/>]+", self.area) if p.strip()]

    @property
    def done(self) -> bool:
        return self.state.lower() in DONE_STATES


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]", "", s).lower()


def _split(v) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [p.strip() for p in re.split(r"[;,|\s]+", str(v)) if p.strip()]


TYPE_MAP = {"user story": "story", "product backlog item": "story", "pbi": "story", "backlog item": "story",
            "improvement": "story", "new feature": "feature", "sub-task": "task", "subtask": "task", "defect": "bug"}


def _date(v) -> datetime | None:
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    for f in (datetime.fromisoformat, lambda x: datetime.strptime(x, "%d/%m/%Y"), lambda x: datetime.strptime(x, "%m/%d/%Y")):
        try:
            return f(s).replace(tzinfo=None)
        except ValueError:
            pass
    return None


def _number(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ticket(row: dict) -> Ticket:
    flat = {_norm(str(k)): v for k, v in row.items()}
    got = {}
    for canon, names in ALIASES.items():
        for n in names:
            if flat.get(_norm(n)) not in (None, ""):
                got[canon] = flat[_norm(n)]
                break
    if "id" not in got:
        raise ValueError(f"ticket without an id: {row}")
    prio = str(got.get("priority", "medium")).strip().lower()
    return Ticket(
        id=str(got["id"]).strip(),
        title=str(got.get("title", "")).strip(),
        type=TYPE_MAP.get(str(got.get("type", "task")).strip().lower(), str(got.get("type", "task")).strip().lower()),
        priority=PRIORITY_RANK.get(prio, 2),
        estimate=_number(got.get("estimate")),
        state=str(got.get("state", "todo")).strip(),
        assignee=str(got.get("assignee", "")).strip(),
        labels=_split(got.get("labels")),
        depends_on=_split(got.get("depends_on")),
        area=str(got.get("area", "")).strip(),
        parent=str(got.get("parent", "")).strip(),
        changed=_date(got.get("changed")),
        days=_number(got.get("days")),
    )


def load_tickets(path: str | Path) -> list[Ticket]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("tickets") or data.get("issues") or data.get("value") or []
        rows = data
    else:
        with p.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    tickets = [_ticket(r) for r in rows]
    seen: set[str] = set()
    for t in tickets:
        if t.id in seen:
            raise ValueError(f"duplicate ticket id: {t.id}")
        seen.add(t.id)
    return tickets
