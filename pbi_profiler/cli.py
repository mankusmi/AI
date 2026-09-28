"""Command-line interface for pbi_profiler.

    pbi-profile profile --source tmdl --path ./MyModel.SemanticModel --html
    pbi-profile profile --source bim --path ./Model.bim
    pbi-profile profile --source live --workspace-id <guid> --dataset-id <guid> --tenant-id <guid> --client-id <guid>

    # optionally, also analyze the report's visuals (pages/visual field usage,
    # cross-checked against the model):
    pbi-profile profile --source tmdl --path ./MyModel.SemanticModel \\
        --report-source pbir --report-path ./MyReport.Report --html

    pbi-profile list-rules
    pbi-profile list-report-rules
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .loaders import BimModelLoader, LiveModelLoader, PowerBiAuth, PowerBiRestClient, TmdlModelLoader
from .report_loaders import LegacyLayoutReportLoader, PbirReportLoader
from .profile_runner import run_profile
from .profiling import list_report_rules, list_rules
from .report import render_html


def _build_loader(args):
    if args.source == "tmdl":
        if not args.path:
            raise SystemExit("--path is required for --source tmdl")
        return TmdlModelLoader(args.path), None
    if args.source == "bim":
        if not args.path:
            raise SystemExit("--path is required for --source bim")
        return BimModelLoader(args.path), None
    if args.source == "live":
        missing = [
            name
            for name, val in (
                ("--workspace-id", args.workspace_id),
                ("--dataset-id", args.dataset_id),
                ("--tenant-id", args.tenant_id),
                ("--client-id", args.client_id),
            )
            if not val
        ]
        if missing:
            raise SystemExit(f"--source live requires: {', '.join(missing)}")
        auth = PowerBiAuth(
            tenant_id=args.tenant_id,
            client_id=args.client_id,
            client_secret=args.client_secret,
        )
        executor = PowerBiRestClient(args.workspace_id, args.dataset_id, auth)
        loader = LiveModelLoader(executor, model_name=args.model_name or args.dataset_id)
        return loader, executor
    raise SystemExit(f"Unknown --source {args.source!r}")


def _build_report_loader(args):
    if not args.report_source:
        return None
    if not args.report_path:
        raise SystemExit("--report-path is required when --report-source is given")
    if args.report_source == "pbir":
        return PbirReportLoader(args.report_path)
    if args.report_source == "legacy-layout":
        return LegacyLayoutReportLoader(args.report_path)
    raise SystemExit(f"Unknown --report-source {args.report_source!r}")


def cmd_profile(args) -> int:
    loader, executor = _build_loader(args)
    model = loader.load()

    if args.source != "live":
        executor = None
    if args.no_data_profile:
        executor = None

    report_loader = _build_report_loader(args)
    report = report_loader.load() if report_loader is not None else None

    result = run_profile(
        model, executor=executor, report=report, include_dependency_graph=args.dependency_graph
    )

    out_dir = Path(args.output) if args.output else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "profile.json"
    json_path.write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")

    print(f"Model: {result.model_name}  (source: {result.source_kind})")
    print(
        f"Tables: {result.schema.table_count}  Columns: {result.schema.column_count}  "
        f"Measures: {result.schema.measure_count}  Relationships: {result.schema.relationship_count}"
    )
    if result.report is not None:
        print(
            f"Report: {result.report.report_name}  Pages: {result.report.page_count}  "
            f"Visuals: {result.report.visual_count}"
        )
    if result.dependency_graph is not None:
        print(
            f"Dependency graph: {len(result.dependency_graph.measure_edges)} measure edges, "
            f"{len(result.dependency_graph.relationships)} relationships"
        )
    sev_counts: dict[str, int] = {}
    for f in result.findings:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
    print(
        "Findings: "
        + ", ".join(f"{sev}={sev_counts.get(sev, 0)}" for sev in ("error", "warning", "info"))
    )
    print(f"Wrote {json_path}")

    if args.html:
        html_path = out_dir / "report.html"
        html_path.write_text(render_html(result), encoding="utf-8")
        print(f"Wrote {html_path}")

    return 1 if sev_counts.get("error") else 0


def cmd_list_rules(args) -> int:
    for rule in list_rules():
        data_flag = " (needs live data)" if rule["requires_data"] else ""
        print(f"[{rule['severity']:7}] {rule['id']:28} {rule['category']:15} {rule['name']}{data_flag}")
    return 0


def cmd_list_report_rules(args) -> int:
    for rule in list_report_rules():
        model_flag = " (needs model cross-check)" if rule["requires_model"] else ""
        print(f"[{rule['severity']:7}] {rule['id']:28} {rule['category']:15} {rule['name']}{model_flag}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pbi-profile", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("profile", help="Profile a semantic model and write JSON/HTML output")
    p.add_argument("--source", choices=["tmdl", "bim", "live"], required=True)
    p.add_argument("--path", help="Path to a TMDL definition/.SemanticModel folder, or a Model.bim file")
    p.add_argument("--workspace-id", help="Power BI workspace (group) GUID, for --source live")
    p.add_argument("--dataset-id", help="Power BI dataset GUID, for --source live")
    p.add_argument("--model-name", help="Friendly name to use in the report, for --source live")
    p.add_argument("--tenant-id", help="AAD tenant id, for --source live (or set PBI_TENANT_ID)")
    p.add_argument("--client-id", help="AAD app/client id, for --source live (or set PBI_CLIENT_ID)")
    p.add_argument(
        "--client-secret",
        help="AAD client secret for a service principal, for --source live "
        "(or set PBI_CLIENT_SECRET; omit to use an interactive device-code login)",
    )
    p.add_argument("--output", default="./pbi_profile_output", help="Output directory (default: ./pbi_profile_output)")
    p.add_argument("--html", action="store_true", help="Also write an HTML report")
    p.add_argument(
        "--no-data-profile",
        action="store_true",
        help="Skip the data (row/null/distinct-count) profile even in --source live",
    )
    p.add_argument(
        "--report-source",
        choices=["pbir", "legacy-layout"],
        help="Also analyze a report's visuals: 'pbir' for a <Name>.Report/definition folder, "
        "'legacy-layout' for a standalone extracted Layout JSON file",
    )
    p.add_argument("--report-path", help="Path to the report artifact named by --report-source")
    p.add_argument(
        "--dependency-graph",
        action="store_true",
        help="Also compute measure-dependency and table-relationship graphs; with --html, "
        "embeds them as SVG diagrams in report.html",
    )
    p.set_defaults(func=cmd_profile)

    lr = sub.add_parser("list-rules", help="List the available model best-practice rules")
    lr.set_defaults(func=cmd_list_rules)

    lrr = sub.add_parser("list-report-rules", help="List the available report/visual best-practice rules")
    lrr.set_defaults(func=cmd_list_report_rules)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
