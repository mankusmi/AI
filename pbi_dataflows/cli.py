"""pbi-dataflows CLI: fetch a workspace's dataflows, or regenerate docs from a previous fetch."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .analysis import analyze
from .render import write_docs
from .store import load_snapshot, save_snapshot

# Microsoft's public Azure CLI app registration; works for device-code sign-in without registering your own app.
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


def _token_provider(a):
    if a.token or os.environ.get("PBI_ACCESS_TOKEN"):
        tok = a.token or os.environ["PBI_ACCESS_TOKEN"]
        return lambda: tok
    from pbi_profiler.loaders.live_loader import PowerBiAuth
    secret = a.client_secret or os.environ.get("PBI_CLIENT_SECRET")
    tenant = a.tenant_id or os.environ.get("PBI_TENANT_ID")
    client = a.client_id or os.environ.get("PBI_CLIENT_ID") or (None if secret else DEFAULT_CLIENT_ID)
    if secret and not (tenant and client):
        raise ValueError("service-principal auth needs --tenant-id and --client-id (or PBI_TENANT_ID / PBI_CLIENT_ID)")
    return PowerBiAuth(tenant or "organizations", client, secret).get_token


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pbi-dataflows", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="connect to a workspace, save dataflow JSON in run order, write docs")
    f.add_argument("--workspace-id", required=True)
    f.add_argument("-o", "--output", default="pbi_dataflows_output")
    f.add_argument("--tenant-id")
    f.add_argument("--client-id")
    f.add_argument("--client-secret", help="service principal secret; omit for interactive device-code sign-in")
    f.add_argument("--token", help="existing bearer token (or set PBI_ACCESS_TOKEN)")
    f.add_argument("--no-refresh-history", action="store_true", help="skip refresh history (faster)")
    f.add_argument("--no-users", action="store_true", help="skip workspace access list")
    d = sub.add_parser("document", help="regenerate docs from a folder written by 'fetch' (offline)")
    d.add_argument("directory")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "fetch":
            from .client import DataflowClient, fetch_workspace
            snap = fetch_workspace(DataflowClient(_token_provider(a)), a.workspace_id, progress=lambda m: print(m, file=sys.stderr),
                                   include_transactions=not a.no_refresh_history, include_users=not a.no_users)
            out, analysis = save_snapshot(snap, a.output)
        else:
            out = Path(a.directory)
            snap = load_snapshot(out)
            analysis = analyze(snap)
        write_docs(out, snap, analysis)
    except Exception as e:  # noqa: BLE001 - CLI boundary: show a message, not a traceback
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{len(analysis['flows'])} dataflow(s) -> {out}  (open {out / 'summary.html'})")
    for w in analysis["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
