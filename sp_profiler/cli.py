"""CLI.

    sp-profile local --path "C:/Users/me/Contoso/Bordereaux - Documents" --out out/
    sp-profile graph --site-url https://contoso.sharepoint.com/sites/Claims --folder "Bordereaux/2024" \\
        --tenant-id <guid> --client-id <guid> [--client-secret <s>] --out out/
"""
from __future__ import annotations

import argparse
import os
import sys

from .profiler import profile
from .report import write_outputs
from .sources import GraphSource, LocalSource
from .stats import human_size


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sp-profile", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="source", required=True)

    def common(sp):
        sp.add_argument("--out", default="sp_profile_output", help="output directory")
        sp.add_argument("--ext", nargs="*", help="extensions to open (default: xlsx xlsm xltx xltm xls xlsb)")
        sp.add_argument("--scan-rows", type=int, default=50, help="rows scanned from the top to find the header row")
        sp.add_argument("--min-headers", type=int, default=3, help="min text cells for a row to count as header")
        sp.add_argument("--excel-only", action="store_true", help="omit non-Excel files from the inventory")

    lo = sub.add_parser("local", help="local folder or OneDrive-synced SharePoint library")
    lo.add_argument("--path", required=True)
    common(lo)

    g = sub.add_parser("graph", help="SharePoint via Microsoft Graph")
    g.add_argument("--site-url", required=True, help="e.g. https://contoso.sharepoint.com/sites/Claims")
    g.add_argument("--library", default="Documents", help="document library name")
    g.add_argument("--folder", default="", help="folder inside the library (default: whole library)")
    g.add_argument("--tenant-id", default=os.environ.get("AZURE_TENANT_ID", ""))
    g.add_argument("--client-id", default=os.environ.get("AZURE_CLIENT_ID", ""))
    g.add_argument("--client-secret", default=os.environ.get("AZURE_CLIENT_SECRET", ""))
    g.add_argument("--token", default=os.environ.get("GRAPH_TOKEN", ""), help="pre-acquired bearer token")
    common(g)

    a = p.parse_args(argv)
    if a.source == "local":
        src = LocalSource(a.path)
    else:
        src = GraphSource(a.site_url, a.folder, a.library, a.tenant_id, a.client_id, a.client_secret, a.token)

    def progress(n, path):
        print(f"[{n}] {path}", file=sys.stderr, flush=True)

    res = profile(src, a.ext, a.scan_rows, a.min_headers, not a.excel_only, progress)
    paths = write_outputs(res, a.out)
    s = res["summary"]
    print(f"\n{s['total_files']} files, {human_size(s['size_bytes'].get('sum', 0))}; "
          f"{s['inspected_files']} Excel inspected {s['status_counts']}")
    print(f"{s['distinct_layouts']} distinct layouts in {s['layout_families']} families "
          f"({s['files_with_multiple_layouts']} files hold >1 layout); {s['duplicate_files']} duplicate files")
    for k, v in paths.items():
        print(f"  {k:10s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
