"""CLI.

    sp-profile local --path "C:/Users/me/Contoso/Bordereaux - Documents" --out out/
    sp-profile graph --site-url https://contoso.sharepoint.com/sites/Claims --folder "Bordereaux/2024" \\
        --db profile.duckdb          # opens the browser to sign in; token cached in ~/.sp_profiler
"""
from __future__ import annotations

import argparse
import os
import sys

from .profiler import profile
from .report import write_outputs
from .sources import CACHE_PATH, DEFAULT_CLIENT_ID, BrowserAuth, GraphSource, LocalSource
from .store import BlobSink, export_file, open_db, save_run
from .stats import human_size


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sp-profile", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="source", required=True)

    def common(sp):
        sp.add_argument("--db", default="sp_profile.duckdb", help="DuckDB file (appended to; one run_id per run)")
        sp.add_argument("--out", default="", help="also write CSV/JSON/HTML exports to this directory")
        sp.add_argument("--ext", nargs="*", help="extensions to open (default: xlsx xlsm xltx xltm xls xlsb)")
        sp.add_argument("--scan-rows", type=int, default=50, help="rows scanned from the top to find the header row")
        sp.add_argument("--min-headers", type=int, default=3, help="min text cells for a row to count as header")
        sp.add_argument("--no-content", action="store_true", help="do not store file bytes in DuckDB")
        sp.add_argument("--max-content-mb", type=int, default=200, help="skip storing files larger than this")
        sp.add_argument("--excel-only", action="store_true", help="omit non-Excel files from the inventory")

    lo = sub.add_parser("local", help="local folder or OneDrive-synced SharePoint library")
    lo.add_argument("--path", required=True)
    common(lo)

    g = sub.add_parser("graph", help="SharePoint via Microsoft Graph")
    g.add_argument("--site-url", required=True, help="e.g. https://contoso.sharepoint.com/sites/Claims")
    g.add_argument("--library", default="Documents", help="document library name")
    g.add_argument("--folder", default="", help="folder inside the library (default: whole library)")
    g.add_argument("--tenant-id", default=os.environ.get("AZURE_TENANT_ID", "organizations"))
    g.add_argument("--client-id", default=os.environ.get("AZURE_CLIENT_ID", DEFAULT_CLIENT_ID),
                   help="public-client app id (default: Microsoft Graph Command Line Tools)")
    g.add_argument("--logout", action="store_true", help="delete the cached sign-in and exit")
    common(g)

    sv = sub.add_parser("serve", help="local browser UI (127.0.0.1 only)")
    sv.add_argument("--db", default="sp_profile.duckdb")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")

    db = sub.add_parser("export-databricks", help="write Parquet + a Databricks load notebook/SQL")
    db.add_argument("--db", default="sp_profile.duckdb")
    db.add_argument("--out", default="databricks_export", help="folder to create the export in")
    db.add_argument("--tables", nargs="*", help="tables to export (default: all df_* tables + file/layout/load metadata)")
    db.add_argument("--incremental", action="store_true", help="df_* tables: only sheets loaded since the last export")
    db.add_argument("--include-blobs", action="store_true", help="also export the original Excel bytes (file_blobs)")
    db.add_argument("--prefix", default="", help="prefix for the Databricks table names")
    db.add_argument("--catalog", default="main")
    db.add_argument("--schema", default="bordereaux")
    db.add_argument("--volume-path", default="/Volumes/<catalog>/<schema>/<volume>/sp_profiler",
                    help="where you will upload the export (baked into the generated notebook/SQL)")

    ex = sub.add_parser("export", help="write a stored file's bytes back out of DuckDB")
    ex.add_argument("--db", default="sp_profile.duckdb")
    ex.add_argument("--rel-path", required=True)
    ex.add_argument("--dest", default=".")

    a = p.parse_args(argv)
    if a.source == "serve":
        from .webapp import serve
        serve(a.db, a.port, not a.no_browser)
        return 0
    if a.source == "export-databricks":
        from .export import export_databricks
        con = open_db(a.db)
        m = export_databricks(con, a.out, a.tables, a.incremental, a.include_blobs, a.prefix, a.catalog, a.schema, a.volume_path)
        con.close()
        print(f"Export {m['export_id']} ({m['mode']}) -> {m['path']}")
        for tb in m["tables"]:
            print(f"  {tb['name']:28s} {tb['rows']:>10,} rows  {len(tb['files'])} file(s)")
        for s in m["skipped"]:
            print(f"  skipped {s['table']}: {s['reason']}")
        print("Upload that folder to Databricks, then import databricks_load.py as a notebook (or run databricks_load.sql).")
        return 0
    if a.source == "export":
        con = open_db(a.db)
        print(export_file(con, a.rel_path, a.dest))
        con.close()
        return 0
    if a.source == "local":
        src, root, extra = LocalSource(a.path), str(LocalSource(a.path).root), {}
    else:
        if a.logout:
            CACHE_PATH.unlink(missing_ok=True)
            print("Cached sign-in removed.")
            return 0
        src = GraphSource(a.site_url, a.folder, a.library, BrowserAuth(a.tenant_id, a.client_id))
        root, extra = f"{a.site_url}/{a.library}/{a.folder}".rstrip("/"), \
            {"site_url": a.site_url, "library": a.library, "folder": a.folder}

    def progress(n, path):
        print(f"[{n}] {path}", file=sys.stderr, flush=True)

    con = open_db(a.db)
    sink = None if a.no_content else BlobSink(con, a.max_content_mb * 1024 * 1024)
    res = profile(src, a.ext, a.scan_rows, a.min_headers, not a.excel_only, progress, sink)
    run_id = save_run(con, res, a.source, root, params={"scan_rows": a.scan_rows, "ext": a.ext,
                                                       "min_headers": a.min_headers}, **extra)
    con.close()
    paths = write_outputs(res, a.out) if a.out else {}
    s = res["summary"]
    print(f"\n{s['total_files']} files, {human_size(s['size_bytes'].get('sum', 0))}; "
          f"{s['inspected_files']} Excel inspected {s['status_counts']}")
    print(f"{s['distinct_layouts']} distinct layouts in {s['layout_families']} families "
          f"({s['files_with_multiple_layouts']} files hold >1 layout); {s['duplicate_files']} duplicate files")
    print(f"  duckdb     {a.db}  (run_id {run_id})")
    for k, v in paths.items():
        print(f"  {k:10s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
