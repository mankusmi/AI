"""devops-plan CLI."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .planner import build_plan
from .render import to_json, to_markdown
from .tickets import load_tickets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="devops-plan", description="Generate a sprint plan from a DevOps ticket export (CSV or JSON).")
    ap.add_argument("tickets", help="ticket export (.csv or .json)")
    ap.add_argument("-c", "--capacity", type=float, default=20, help="story points per sprint (default 20)")
    ap.add_argument("-f", "--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("-o", "--output", help="write to this file instead of stdout")
    ap.add_argument("--title", default="Delivery plan")
    ap.add_argument("--include-done", action="store_true", help="plan tickets that are already closed too")
    a = ap.parse_args(argv)
    try:
        plan = build_plan(load_tickets(a.tickets), a.capacity, a.include_done)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    text = to_json(plan) if a.format == "json" else to_markdown(plan, a.title)
    if a.output:
        Path(a.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
