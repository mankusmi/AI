"""Write CSV / JSON / HTML outputs."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .stats import human_size


def _csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel reads UTF-8
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("; ".join(v) if isinstance(v, list) else v) for k, v in r.items()})


def write_outputs(result: dict, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}

    paths["inventory"] = out / "file_inventory.csv"
    _csv(paths["inventory"], result["files"], [
        "rel_path", "name", "folder", "depth", "extension", "size_bytes", "modified", "created",
        "modified_by", "status", "error", "sheet_count", "data_sheet_count", "total_data_rows",
        "primary_sheet", "primary_layout", "primary_family", "layout_hashes", "has_macros",
        "sha256", "location"])

    paths["sheets"] = out / "sheet_headers.csv"
    _csv(paths["sheets"], result["sheets"], [
        "rel_path", "sheet", "state", "max_row", "max_col", "header_row", "data_rows",
        "header_count", "layout", "layout_hash", "set_hash", "headers"])

    layout_rows = []
    for l in result["layouts"]:
        d = result["layout_diffs"][l.layout_id]
        layout_rows.append({
            "layout": l.layout_id, "family": l.family, "files": len(l.files), "sheets": l.sheets,
            "columns": len(l.norm_headers), "total_data_rows": l.total_rows,
            "added_vs_family_seed": d["added"], "removed_vs_family_seed": d["removed"],
            "reordered_vs_family_seed": d["reordered"], "headers": l.headers,
            "examples": l.examples, "layout_hash": l.layout_hash})
    paths["layouts"] = out / "layouts.csv"
    _csv(paths["layouts"], layout_rows, list(layout_rows[0]) if layout_rows else ["layout"])

    hf = [{"header": h, "files": len(v["files"]), "spellings": [f"{k} ({n})" for k, n in v["spellings"].items()]}
          for h, v in sorted(result["header_frequency"].items(), key=lambda kv: -len(kv[1]["files"]))]
    paths["headers"] = out / "header_frequency.csv"
    _csv(paths["headers"], hf, ["header", "files", "spellings"])

    paths["summary"] = out / "summary.json"
    paths["summary"].write_text(json.dumps(result["summary"], indent=2, default=str), encoding="utf-8")

    paths["html"] = out / "report.html"
    paths["html"].write_text(render_html(result), encoding="utf-8")
    return paths


def _table(head: list[str], rows: list[list]) -> str:
    th = "".join(f"<th>{html.escape(str(h))}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def render_html(result: dict) -> str:
    s = result["summary"]
    sz = s["size_bytes"]
    kpis = [("Files", s["total_files"]), ("Inspected", s["inspected_files"]),
            ("Distinct layouts", s["distinct_layouts"]), ("Layout families", s["layout_families"]),
            ("Total size", human_size(sz.get("sum", 0))), ("Duplicates", s["duplicate_files"])]
    kpi_html = "".join(f"<div class=kpi><b>{v}</b><span>{k}</span></div>" for k, v in kpis)
    size_rows = [[ext, d["count"], human_size(d["min"]), human_size(d["median"]), human_size(d["mean"]),
                  human_size(d["p90"]), human_size(d["max"]), human_size(d["sum"])]
                 for ext, d in s["size_bytes_by_extension"].items()]
    lay_rows = []
    for l in result["layouts"]:
        d = result["layout_diffs"][l.layout_id]
        change = []
        if d["added"]: change.append("+" + ", ".join(d["added"][:5]))
        if d["removed"]: change.append("−" + ", ".join(d["removed"][:5]))
        if d["reordered"]: change.append("reordered")
        lay_rows.append([l.layout_id, l.family, len(l.files), l.sheets, len(l.norm_headers),
                         " | ".join(change) or "(seed)", ", ".join(l.headers[:12]) + (" …" if len(l.headers) > 12 else "")])
    problems = [[f["rel_path"], f["status"], f["error"]] for f in result["files"]
                if f["inspected"] and f["status"] != "ok"]
    return f"""<!doctype html><meta charset=utf-8><title>SharePoint file profile</title>
<style>body{{font:14px system-ui;margin:2rem;color:#222}}table{{border-collapse:collapse;margin:.5rem 0 2rem}}
td,th{{border:1px solid #ccc;padding:4px 8px;text-align:left;vertical-align:top}}th{{background:#f0f0f0}}
.kpis{{display:flex;gap:1rem;flex-wrap:wrap}}.kpi{{border:1px solid #ccc;padding:.6rem 1rem;border-radius:6px}}
.kpi b{{display:block;font-size:1.6rem}}.kpi span{{color:#666}}</style>
<h1>SharePoint file profile</h1><p>Generated {s['generated_utc']}</p><div class=kpis>{kpi_html}</div>
<h2>File size by extension</h2>{_table(['Ext','Files','Min','Median','Mean','P90','Max','Total'], size_rows)}
<h2>Layouts</h2><p>{s['distinct_layouts']} exact layouts ({s['distinct_header_sets']} ignoring column order),
grouped into {s['layout_families']} families at ≥80% header overlap.
{s['files_with_multiple_layouts']} files contain more than one layout; {s['files_with_no_header_detected']} have no detectable header.</p>
{_table(['Layout','Family','Files','Sheets','Cols','Diff vs family seed','Headers'], lay_rows)}
<h2>Problem files</h2>{_table(['File','Status','Detail'], problems) if problems else '<p>None.</p>'}
"""
