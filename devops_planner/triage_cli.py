"""devops-triage CLI."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .tickets import load_tickets
from .triage import Config, review
from .triage_render import changes_csv, to_json, to_markdown


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="devops-triage", description="Review a DevOps backlog: classify epics/features/user stories and flag hygiene issues.")
    ap.add_argument("tickets", help="ticket export (.csv or .json)")
    ap.add_argument("--story-max", type=float, default=5, help="max days for a user story (default 5)")
    ap.add_argument("--feature-max", type=float, default=10, help="max days for a feature, i.e. one sprint (default 10)")
    ap.add_argument("--epic-max", type=float, default=65, help="max days for an epic, i.e. one quarter (default 65)")
    ap.add_argument("--days-per-point", type=float, default=1, help="convert story points to working days (default 1)")
    ap.add_argument("--stale-days", type=int, default=60)
    ap.add_argument("--min-area-depth", type=int, default=2, help="minimum levels in an area path (default 2)")
    ap.add_argument("-f", "--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("-o", "--output", help="write the report here instead of stdout")
    ap.add_argument("--csv", dest="csv_out", help="also write suggested changes as CSV for bulk update")
    a = ap.parse_args(argv)
    cfg = Config(a.story_max, a.feature_max, a.epic_max, days_per_point=a.days_per_point, stale_days=a.stale_days,
                 min_area_depth=a.min_area_depth)
    try:
        r = review(load_tickets(a.tickets), cfg)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    text = to_json(r) if a.format == "json" else to_markdown(r)
    Path(a.output).write_text(text, encoding="utf-8") if a.output else print(text)
    if a.csv_out:
        Path(a.csv_out).write_text(changes_csv(r), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
