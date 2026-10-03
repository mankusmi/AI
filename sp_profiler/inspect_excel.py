"""Open an Excel file and extract per-sheet structure: header row, headers, dimensions."""
from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

EXCEL_EXTS = {".xlsx", ".xlsm", ".xltx", ".xltm"}
LEGACY_EXTS = {".xls", ".xlsb"}          # recognised, but not opened (no openpyxl support)


@dataclass
class SheetInfo:
    name: str
    state: str                       # visible / hidden / veryHidden
    max_row: int
    max_col: int
    header_row: Optional[int]        # 1-based, None if no header detected
    headers: list[str]               # raw header text
    norm_headers: list[str]          # normalised, order preserved
    data_rows: int                   # approx: max_row - header_row
    layout_hash: str = ""            # ordered normalised headers
    set_hash: str = ""               # same headers, order ignored

    @property
    def is_data_sheet(self) -> bool:
        return self.header_row is not None


@dataclass
class WorkbookInfo:
    status: str = "ok"               # ok / unsupported / encrypted / corrupt / error
    error: str = ""
    sheets: list[SheetInfo] = field(default_factory=list)
    sha256: str = ""
    defined_names: int = 0
    has_macros: bool = False


def normalise_header(text: object) -> str:
    s = re.sub(r"[^0-9a-z]+", " ", str(text).casefold())
    return re.sub(r"\s+", " ", s).strip()


def _short_hash(parts: list[str]) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:8]


def sha256_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def detect_header_row(rows: list[tuple], min_headers: int = 3) -> Optional[int]:
    """Index (0-based) of the most header-like row among the scanned rows.

    Bordereaux often carry title/logo/summary rows above the real header, so we pick
    the first row that has the most non-empty *text* cells, requiring at least
    ``min_headers`` of them and mostly-unique values.
    """
    best, best_score = None, 0
    for i, row in enumerate(rows):
        cells = [c for c in row if isinstance(c, str) and c.strip()]
        if len(cells) < min_headers:
            continue
        uniq = len({normalise_header(c) for c in cells})
        score = uniq if uniq >= 0.8 * len(cells) else 0
        if score > best_score:
            best, best_score = i, score
    return best


def _trim(row: tuple) -> list:
    r = list(row)
    while r and (r[-1] is None or (isinstance(r[-1], str) and not r[-1].strip())):
        r.pop()
    return r


def inspect_workbook(path, scan_rows: int = 50, min_headers: int = 3) -> WorkbookInfo:
    from pathlib import Path
    path = Path(path)
    ext = path.suffix.lower()
    info = WorkbookInfo()
    try:
        info.sha256 = sha256_file(path)
    except OSError as e:
        info.status, info.error = "error", str(e)
        return info
    if ext in LEGACY_EXTS:
        info.status, info.error = "unsupported", f"{ext} not opened (convert to .xlsx)"
        return info
    if ext not in EXCEL_EXTS:
        info.status, info.error = "unsupported", "not an Excel file"
        return info

    import openpyxl
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
        info.has_macros = "xl/vbaProject.bin" in names
    except zipfile.BadZipFile:
        # Password-protected workbooks are OLE2 containers, not zips
        with open(path, "rb") as fh:
            magic = fh.read(8)
        if magic.startswith(b"\xd0\xcf\x11\xe0"):
            info.status, info.error = "encrypted", "password-protected or legacy OLE container"
        else:
            info.status, info.error = "corrupt", "not a valid xlsx zip"
        return info

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # openpyxl raises many types for damaged files
        info.status, info.error = "corrupt", f"{type(e).__name__}: {e}"
        return info
    try:
        info.defined_names = len(wb.defined_names)
        for ws in wb.worksheets:
            try:
                info.sheets.append(_inspect_sheet(ws, scan_rows, min_headers))
            except Exception as e:
                info.status, info.error = "error", f"sheet {ws.title!r}: {type(e).__name__}: {e}"
    finally:
        wb.close()
    return info


def _inspect_sheet(ws, scan_rows: int, min_headers: int) -> SheetInfo:
    state = getattr(ws, "sheet_state", "visible")
    if getattr(ws, "max_row", None) is None:
        ws.reset_dimensions()
    max_row, max_col = ws.max_row or 0, ws.max_column or 0
    head = [tuple(r) for r in ws.iter_rows(min_row=1, max_row=scan_rows, values_only=True)]
    idx = detect_header_row(head, min_headers)
    if idx is None:
        return SheetInfo(ws.title, state, max_row, max_col, None, [], [], 0)
    raw = _trim(head[idx])
    raw_txt = ["" if c is None else str(c).strip() for c in raw]
    norm = [normalise_header(c) for c in raw_txt]
    # Blank header cells between named ones still occupy a column position
    norm = [n if n else f"<blank{i + 1}>" for i, n in enumerate(norm)]
    return SheetInfo(
        name=ws.title, state=state, max_row=max_row, max_col=max_col,
        header_row=idx + 1, headers=raw_txt, norm_headers=norm,
        data_rows=max(0, max_row - (idx + 1)),
        layout_hash=_short_hash(norm), set_hash=_short_hash(sorted(norm)),
    )
