"""Orchestrates: enumerate files -> inspect Excel files -> layouts -> statistics."""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from .inspect_excel import EXCEL_EXTS, LEGACY_EXTS, inspect_workbook, sha256_file
from .layouts import Layout, build_layouts, diff_to_seed, header_frequency
from .stats import counts, describe, group_describe


def profile(source, extensions: Optional[Iterable[str]] = None, scan_rows: int = 50,
            min_headers: int = 3, include_all_files: bool = True,
            progress: Optional[Callable[[int, str], None]] = None) -> dict:
    wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions} if extensions \
        else EXCEL_EXTS | LEGACY_EXTS
    files: list[dict] = []
    sheets: list[dict] = []
    n = 0
    for entry in source.iter_files():
        n += 1
        is_target = entry.extension in wanted
        if not is_target and not include_all_files:
            continue
        row = {
            "rel_path": entry.rel_path, "name": entry.name, "folder": entry.folder,
            "depth": entry.depth, "extension": entry.extension, "size_bytes": entry.size_bytes,
            "modified": entry.modified, "created": entry.created, "modified_by": entry.modified_by,
            "location": entry.location, "inspected": is_target,
            "status": "not_inspected", "error": "", "sha256": "", "sheet_count": None,
            "data_sheet_count": None, "total_data_rows": None, "primary_sheet": "",
            "primary_layout_hash": "", "layout_hashes": "", "has_macros": None,
        }
        if is_target:
            if progress:
                progress(n, entry.rel_path)
            try:
                with entry.materialize() as local:
                    wb = inspect_workbook(local, scan_rows, min_headers)
            except Exception as e:  # download or IO failure must not abort the run
                wb = None
                row.update(status="error", error=f"{type(e).__name__}: {e}")
            if wb is not None:
                row.update(status=wb.status, error=wb.error, sha256=wb.sha256, has_macros=wb.has_macros,
                           sheet_count=len(wb.sheets))
                data = [s for s in wb.sheets if s.is_data_sheet]
                row["data_sheet_count"] = len(data)
                row["total_data_rows"] = sum(s.data_rows for s in data)
                if data:
                    main = max(data, key=lambda s: (s.data_rows, len(s.norm_headers)))
                    row["primary_sheet"], row["primary_layout_hash"] = main.name, main.layout_hash
                    row["layout_hashes"] = "|".join(sorted({s.layout_hash for s in data}))
                for s in wb.sheets:
                    sheets.append({
                        "rel_path": entry.rel_path, "sheet": s.name, "state": s.state,
                        "max_row": s.max_row, "max_col": s.max_col, "header_row": s.header_row,
                        "data_rows": s.data_rows, "header_count": len(s.headers),
                        "layout_hash": s.layout_hash, "set_hash": s.set_hash,
                        "headers": s.headers, "norm_headers": s.norm_headers,
                    })
        files.append(row)

    data_sheets = [s for s in sheets if s["header_row"] is not None]
    layouts = build_layouts(data_sheets)
    by_hash = {l.layout_hash: l for l in layouts}
    for f in files:
        l = by_hash.get(f["primary_layout_hash"])
        f["primary_layout"] = l.layout_id if l else ""
        f["primary_family"] = l.family if l else ""
    for s in data_sheets:
        s["layout"] = by_hash[s["layout_hash"]].layout_id
    return {
        "files": files, "sheets": sheets, "layouts": layouts,
        "summary": summarise(files, sheets, layouts, data_sheets),
        "header_frequency": header_frequency(data_sheets),
        "layout_diffs": {l.layout_id: diff_to_seed(l, layouts) for l in layouts},
    }


def summarise(files: list[dict], sheets: list[dict], layouts: list[Layout], data_sheets: list[dict]) -> dict:
    inspected = [f for f in files if f["inspected"]]
    ok = [f for f in inspected if f["status"] == "ok"]
    now = datetime.now(timezone.utc)
    ages = []
    for f in files:
        if f["modified"]:
            try:
                ages.append((now - datetime.fromisoformat(f["modified"].replace("Z", "+00:00"))).days)
            except ValueError:
                pass
    dup_groups: dict[str, list[str]] = defaultdict(list)
    for f in ok:
        dup_groups[f["sha256"]].append(f["rel_path"])
    dups = {h: p for h, p in dup_groups.items() if len(p) > 1}
    ranks = {l.layout_hash: l for l in layouts}
    files_per_layout = Counter(f["primary_layout"] for f in ok if f["primary_layout"])
    return {
        "generated_utc": now.isoformat(timespec="seconds"),
        "total_files": len(files),
        "inspected_files": len(inspected),
        "status_counts": counts(f["status"] for f in inspected),
        "extension_counts": counts(f["extension"] or "(none)" for f in files),
        "folder_count": len({f["folder"] for f in files}),
        "max_depth": max((f["depth"] for f in files), default=0),
        "size_bytes": describe(f["size_bytes"] for f in files),
        "size_bytes_by_extension": group_describe(files, "extension", "size_bytes"),
        "size_bytes_by_folder": group_describe(files, "folder", "size_bytes"),
        "age_days": describe(ages),
        "sheets_per_file": describe(f["sheet_count"] for f in ok),
        "data_rows_per_file": describe(f["total_data_rows"] for f in ok),
        "files_with_no_header_detected": sum(1 for f in ok if not f["data_sheet_count"]),
        "files_with_multiple_layouts": sum(1 for f in ok if len(f["layout_hashes"].split("|")) > 1),
        "files_with_macros": sum(1 for f in ok if f["has_macros"]),
        "distinct_layouts": len(layouts),
        "distinct_header_sets": len({s["set_hash"] for s in data_sheets}),
        "layout_families": len({l.family for l in layouts}),
        "singleton_layouts": sum(1 for l in layouts if len(l.files) == 1),
        "layout_coverage": {l.layout_id: {"files": len(l.files), "sheets": l.sheets,
                                          "pct_of_files": round(100 * len(l.files) / max(len(ok), 1), 1),
                                          "family": l.family, "columns": len(l.norm_headers)}
                            for l in layouts},
        "primary_layout_file_counts": dict(files_per_layout.most_common()),
        "duplicate_file_groups": len(dups),
        "duplicate_files": sum(len(p) - 1 for p in dups.values()),
        "duplicates": list(dups.values()),
    }
