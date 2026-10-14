"""devops-report CLI."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .report import build_report
from .report_render import to_html, to_markdown
from .tickets import load_tickets
from .triage import Config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="devops-report", description="State-of-play report for a head of delivery, from a ticket export.")
    ap.add_argument("tickets", help="ticket export (.csv or .json); needs Parent, State and ideally Closed Date / Changed Date columns")
    ap.add_argument("-f", "--format", choices=["html", "markdown"], default="html")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    ap.add_argument("--title", default="Delivery: state of play")
    ap.add_argument("--as-of", help="report date, YYYY-MM-DD (default today)")
    ap.add_argument("--weeks", type=int, default=6, help="weeks of throughput history (default 6)")
    ap.add_argument("--aging-days", type=int, default=14, help="in-progress items untouched this long are flagged (default 14)")
    ap.add_argument("--previous", help="yesterday's export; adds a 'since the last run' panel to the data lead view")
    ap.add_argument("--wip-limit", type=int, default=3, help="max items in flight per person before they are flagged (default 3)")
    ap.add_argument("--view", choices=["both", "exec", "lead"], default="both", help="exec = head of delivery only, lead = data lead only (default both)")
    ap.add_argument("--days-per-point", type=float, default=1)
    a = ap.parse_args(argv)
    try:
        as_of = datetime.strptime(a.as_of, "%Y-%m-%d") if a.as_of else None
        prev = load_tickets(a.previous) if a.previous else None
        rep = build_report(load_tickets(a.tickets), Config(days_per_point=a.days_per_point), as_of, a.weeks, a.aging_days, prev, a.wip_limit)
    except (OSError, ValueError) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
    text = to_html(rep, a.title, a.view) if a.format == "html" else to_markdown(rep, a.title)
    Path(a.output).write_text(text, encoding="utf-8") if a.output else print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
