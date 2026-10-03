"""Orchestrates: enumerate files -> inspect Excel files -> layouts -> statistics."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from .inspect_excel import EXCEL_EXTS, LEGACY_EXTS, inspect_workbook
from .layouts import Layout, build_layouts, diff_to_seed, header_frequency
from .stats import counts, describe, group_describe


META_KEYS = ("site_url", "library", "drive_id", "item_id", "sp_path", "mime_type", "created_by",
             "modified_by_email", "etag", "ctag", "quickxor_hash", "sha1_hash")


def _base_row(entry, is_target: bool) -> dict:
    return {
        "rel_path": entry.rel_path, "name": entry.name, "folder": entry.folder,
        "depth": entry.depth, "extension": entry.extension, "size_bytes": entry.size_bytes,
        "modified": entry.modified, "created": entry.created, "modified_by": entry.modified_by,
        "location": entry.location, "inspected": is_target,
        "status": "not_inspected", "error": "", "sha256": "", "sheet_count": None,
        "data_sheet_count": None, "total_data_rows": None, "primary_sheet": "",
        "primary_layout_hash": "", "layout_hashes": "", "has_macros": None, "content_stored": False,
        "warnings": "", "reused_from": None,
        **{k: entry.meta.get(k) for k in META_KEYS},
    }


def _inspect_entry(entry, row: dict, scan_rows, min_headers, include_hidden, exact_rows, blob_sink):
    """Download (if needed), open and inspect one Excel file. Runs in a worker thread."""
    sheet_rows: list[dict] = []
    try:
        with entry.materialize() as local:
            wb = inspect_workbook(local, scan_rows, min_headers, include_hidden, exact_rows)
            if blob_sink and wb.sha256:
                row["content_stored"] = bool(blob_sink(local, wb.sha256))
    except Exception as e:  # download or IO failure must not abort the run
        row.update(status="error", error=f"{type(e).__name__}: {e}")
        return row, sheet_rows
    row.update(status=wb.status, error=wb.error, sha256=wb.sha256, has_macros=wb.has_macros,
               sheet_count=len(wb.sheets), warnings="; ".join(wb.warnings))
    data = [s for s in wb.sheets if s.is_data_sheet]
    row["data_sheet_count"] = len(data)
    row["total_data_rows"] = sum(s.data_rows for s in data)
    if data:
        main = max(data, key=lambda s: (s.data_rows, len(s.norm_headers)))
        row["primary_sheet"], row["primary_layout_hash"] = main.name, main.layout_hash
        row["layout_hashes"] = "|".join(sorted({s.layout_hash for s in data}))
    for s in wb.sheets:
        sheet_rows.append({
            "rel_path": entry.rel_path, "sheet": s.name, "state": s.state,
            "max_row": s.max_row, "max_col": s.max_col, "header_row": s.header_row,
            "data_rows": s.data_rows, "header_count": len(s.headers),
            "layout_hash": s.layout_hash, "set_hash": s.set_hash,
            "headers": s.headers, "norm_headers": s.norm_headers,
        })
    return row, sheet_rows


def profile(source, extensions: Optional[Iterable[str]] = None, scan_rows: int = 50,
            min_headers: int = 3, include_all_files: bool = True,
            progress: Optional[Callable[[int, str], None]] = None,
            blob_sink: Optional[Callable[[object, str], bool]] = None, *,
            workers: int = 1, include_hidden: bool = False, exact_rows: bool = False,
            previous: Optional[Callable[[object], Optional[tuple]]] = None,
            on_file: Optional[Callable[[dict, list], None]] = None) -> dict:
    """Profile every file a source yields.

    ``blob_sink(local_path, sha256) -> stored?`` is called while each file is still on disk (from worker threads).
    ``previous(entry)`` may return ``(file_row, sheet_rows)`` from an earlier run for an unchanged file, which skips the
    download and inspection. ``on_file(row, sheets)`` is called, in order, as each file completes (used to save progress).
    ``workers > 1`` inspects files in parallel (useful for SharePoint downloads).
    """
    wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions} if extensions \
        else EXCEL_EXTS | LEGACY_EXTS
    files: list[dict] = []
    sheets: list[dict] = []
    reused = 0
    jobs: list[tuple] = []          # (entry, row, is_target, reuse_result)
    for entry in source.iter_files():
        is_target = entry.extension in wanted
        if not is_target and not include_all_files:
            continue
        row = _base_row(entry, is_target)
        reuse = previous(entry) if (is_target and previous) else None
        jobs.append((entry, row, is_target, reuse))

    def work(job):
        entry, row, is_target, reuse = job
        if reuse:
            prev_row, prev_sheets = reuse
            merged = {**row, **prev_row}                  # inspection results from the earlier run ...
            merged.update(rel_path=entry.rel_path, name=entry.name, folder=entry.folder, depth=entry.depth,
                          extension=entry.extension, location=entry.location, size_bytes=entry.size_bytes,
                          modified=entry.modified, created=entry.created, modified_by=entry.modified_by,
                          inspected=True, **{k: entry.meta.get(k) for k in META_KEYS})     # ... current identity
            return merged, [{**s, "rel_path": entry.rel_path} for s in prev_sheets], True
        if not is_target:
            return row, [], False
        r, sh = _inspect_entry(entry, row, scan_rows, min_headers, include_hidden, exact_rows, blob_sink)
        return r, sh, False

    def consume(n, entry, result):
        nonlocal reused
        row, sheet_rows, was_reused = result
        reused += was_reused
        files.append(row)
        sheets.extend(sheet_rows)
        if on_file:
            on_file(row, sheet_rows)
        if progress and row["inspected"]:
            progress(n, entry.rel_path + (" (unchanged)" if was_reused else ""))

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, j) for j in jobs]
            for n, (job, fut) in enumerate(zip(jobs, futures), 1):
                consume(n, job[0], fut.result())
    else:
        for n, job in enumerate(jobs, 1):
            consume(n, job[0], work(job))

    data_sheets = [s for s in sheets if s["header_row"] is not None]
    layouts = build_layouts(data_sheets)
    by_hash = {lay.layout_hash: lay for lay in layouts}
    for f in files:
        lay = by_hash.get(f["primary_layout_hash"])
        f["primary_layout"] = lay.layout_id if lay else ""
        f["primary_family"] = lay.family if lay else ""
    for s in data_sheets:
        s["layout"] = by_hash[s["layout_hash"]].layout_id
    summary = summarise(files, sheets, layouts, data_sheets)
    summary["reused_files"] = reused
    summary["files_with_warnings"] = sum(1 for f in files if f.get("warnings"))
    return {
        "files": files, "sheets": sheets, "layouts": layouts, "summary": summary,
        "header_frequency": header_frequency(data_sheets),
        "layout_diffs": {lay.layout_id: diff_to_seed(lay, layouts) for lay in layouts},
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
        "layout_families": len({lay.family for lay in layouts}),
        "singleton_layouts": sum(1 for lay in layouts if len(lay.files) == 1),
        "layout_coverage": {lay.layout_id: {"files": len(lay.files), "sheets": lay.sheets,
                                          "pct_of_files": round(100 * len(lay.files) / max(len(ok), 1), 1),
                                          "family": lay.family, "columns": len(lay.norm_headers)}
                            for lay in layouts},
        "primary_layout_file_counts": dict(files_per_layout.most_common()),
        "duplicate_file_groups": len(dups),
        "duplicate_files": sum(len(p) - 1 for p in dups.values()),
        "duplicates": list(dups.values()),
    }
