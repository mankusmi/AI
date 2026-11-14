"""Dashboard manifest: one CSV row per legacy dashboard (report + its model)."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

STATUSES = ("stay", "replace", "retire")
REPORT_SOURCES = ("pbir", "legacy-layout")
MODEL_SOURCES = ("tmdl", "bim")
COLUMNS = ["dashboard", "status", "report_source", "report_path", "model_source", "model_path", "owner", "notes"]


@dataclass
class DashboardEntry:
    dashboard: str
    status: str  # stay | replace | retire
    report_source: str
    report_path: Path
    model_source: str
    model_path: Path
    owner: str = ""
    notes: str = ""


def load_manifest(path: str | Path) -> list[DashboardEntry]:
    """Read the manifest. Relative paths resolve against the manifest's folder."""
    path = Path(path)
    base = path.parent
    entries: list[DashboardEntry] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"dashboard", "status", "report_source", "report_path", "model_source", "model_path"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"manifest is missing column(s): {', '.join(sorted(missing))}")
        for n, row in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in row.items() if k}
            if not row.get("dashboard"):
                continue
            status = row["status"].lower()
            if status not in STATUSES:
                raise ValueError(f"manifest line {n} ({row['dashboard']}): status {row['status']!r} must be one of {STATUSES}"
                                 + (" -- fill in the status column" if not status else ""))
            if row["report_source"] not in REPORT_SOURCES:
                raise ValueError(f"manifest line {n}: report_source must be one of {REPORT_SOURCES}")
            if row["model_source"] not in MODEL_SOURCES:
                raise ValueError(f"manifest line {n}: model_source must be one of {MODEL_SOURCES}")
            entries.append(DashboardEntry(
                dashboard=row["dashboard"], status=status,
                report_source=row["report_source"], report_path=_resolve(base, row["report_path"]),
                model_source=row["model_source"], model_path=_resolve(base, row["model_path"]),
                owner=row.get("owner", ""), notes=row.get("notes", ""),
            ))
    names = [e.dashboard for e in entries]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate dashboard name(s) in manifest: {', '.join(sorted(dupes))}")
    return entries


def _resolve(base: Path, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else base / q


def write_template(path: str | Path) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        w.writerow(["Sales Overview", "replace", "pbir", "reports/Sales.Report", "tmdl", "models/Sales.SemanticModel", "j.smith", ""])
        w.writerow(["Finance Pack", "stay", "pbir", "reports/Finance.Report", "bim", "models/Finance/Model.bim", "a.jones", ""])


def load_aliases(path: str | Path) -> dict[str, str]:
    """Optional CSV `datapoint,canonical` (qualified names like `Sales[Net Rev]`)
    merging differently-named datapoints into one canonical shopping-list row."""
    out: dict[str, str] = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            a, c = (row.get("datapoint") or "").strip(), (row.get("canonical") or "").strip()
            if a and c:
                out[a] = c
    return out
