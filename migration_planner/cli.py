"""pbi-migrate: build a datapoint shopping list from legacy dashboards."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analysis import analyze
from .manifest import load_aliases, load_manifest, write_template
from .render import write_all


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pbi-migrate", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("init", help="write a template dashboards.csv manifest")
    t.add_argument("path", nargs="?", default="dashboards.csv")
    b = sub.add_parser("build", help="analyse the manifest and write the shopping list")
    b.add_argument("-m", "--manifest", required=True)
    b.add_argument("-o", "--output", default="migration_output")
    b.add_argument("--aliases", help="CSV (datapoint,canonical) merging differently named datapoints")
    args = p.parse_args(argv)

    if args.cmd == "init":
        if Path(args.path).exists():
            print(f"{args.path} already exists", file=sys.stderr)
            return 1
        write_template(args.path)
        print(f"wrote {args.path}")
        return 0
    try:
        entries = load_manifest(args.manifest)
        a = analyze(entries, load_aliases(args.aliases) if args.aliases else None)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for f in write_all(a, Path(args.output)):
        print(f)
    shop = a.shopping_list()
    print(f"{len(shop)} datapoints to source ({sum(r['decision'].startswith('SHARED') for r in shop)} shared with staying dashboards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
