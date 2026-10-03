"""One place that runs a profiling pass (shared by the CLI and the browser UI)."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .profiler import profile
from .store import BlobSink, RunWriter, inspect_key, make_previous

log = logging.getLogger("sp_profiler")


def run_profile(con, source, source_type: str, root: str, extra: Optional[dict] = None, *,
                ext: Optional[list[str]] = None, scan_rows: int = 50, min_headers: int = 3, excel_only: bool = False,
                store_content: bool = True, max_content_mb: int = 200, blob_dir: Optional[str] = None,
                workers: int = 1, include_hidden: bool = False, exact_rows: bool = False, refresh: bool = False,
                progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """Profile ``source`` into ``con``.

    Progress is saved as the run goes (an interrupted run is not lost), unchanged files from earlier runs are reused
    instead of being downloaded and opened again (``refresh=True`` re-inspects everything), and ``workers`` files are
    inspected in parallel. Returns ``{"result": ..., "run_id": ...}``.
    """
    key = inspect_key(scan_rows, min_headers, include_hidden, exact_rows)
    params = {"scan_rows": scan_rows, "min_headers": min_headers, "include_hidden": include_hidden, "exact_rows": exact_rows,
              "ext": ext, "inspect_key": key, "workers": workers, "blob_dir": blob_dir, "refresh": refresh,
              "store_content": store_content}
    writer = RunWriter(con, source_type, root, params=params, **(extra or {}))
    sink = BlobSink(con, max_content_mb * 1024 * 1024, blob_dir) if store_content else None
    previous = None if refresh else make_previous(con, key, want_content=store_content)
    try:
        res = profile(source, ext, scan_rows, min_headers, not excel_only, progress, sink, workers=workers,
                      include_hidden=include_hidden, exact_rows=exact_rows, previous=previous, on_file=writer.add)
        run_id = writer.finish(res)
    except BaseException as e:
        writer.fail(f"{type(e).__name__}: {e}")
        raise
    if sink and sink.skipped_too_large:
        log.warning("%d file(s) were larger than %d MB and their bytes were not stored", sink.skipped_too_large, max_content_mb)
    return {"result": res, "run_id": run_id, "skipped_too_large": sink.skipped_too_large if sink else 0}
