import json
from pathlib import Path

import openpyxl
import pytest

from sp_profiler.cli import main
from sp_profiler.inspect_excel import detect_header_row, inspect_workbook, normalise_header
from sp_profiler.profiler import profile
from sp_profiler.sources import LocalSource

BASE = ["Policy No", "Insured", "Inception Date", "Premium", "Currency", "Commission"]


def make(path: Path, headers, rows=3, title_rows=0, extra_sheet=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for i in range(title_rows):
        ws.append([f"Bordereau title line {i}"])
    ws.append(headers)
    for r in range(rows):
        ws.append([f"v{r}{c}" for c in range(len(headers))])
    if extra_sheet:
        ws2 = wb.create_sheet("Notes")
        ws2.append(["just one cell"])
    wb.save(path)


@pytest.fixture
def tree(tmp_path):
    make(tmp_path / "A/jan.xlsx", BASE, rows=5)
    make(tmp_path / "A/feb.xlsx", BASE, rows=7, title_rows=3)          # same layout, title rows
    make(tmp_path / "A/mar.xlsx", BASE, rows=6)
    (tmp_path / "A/mar_copy.xlsx").write_bytes((tmp_path / "A/mar.xlsx").read_bytes())  # duplicate
    make(tmp_path / "B/apr.xlsx", BASE[::-1], rows=4)                   # reordered
    make(tmp_path / "B/may.xlsx", BASE + ["Region"], rows=4)            # extra column -> same family
    make(tmp_path / "B/jun.xlsx", ["Ref", "Name", "Amount", "Ccy"], rows=2, extra_sheet=True)
    (tmp_path / "B/broken.xlsx").write_bytes(b"not a zip")
    (tmp_path / "B/old.xls").write_bytes(b"\xd0\xcf\x11\xe0legacy")
    (tmp_path / "B/readme.txt").write_text("hi")
    return tmp_path


def test_normalise_header():
    assert normalise_header("  Policy_No. ") == "policy no"
    assert normalise_header("Gross  Premium (GBP)") == "gross premium gbp"


def test_header_detection_skips_title_rows():
    rows = [("Title", None), (None, None), ("a", "b", "c", "d"), (1, 2, 3, 4)]
    assert detect_header_row(rows) == 2
    assert detect_header_row([(1, 2, 3), ("x",)]) is None


def test_inspect_workbook_title_rows(tmp_path):
    f = tmp_path / "x.xlsx"
    make(f, BASE, rows=7, title_rows=3)
    info = inspect_workbook(f)
    assert info.status == "ok"
    s = info.sheets[0]
    assert s.header_row == 4 and s.data_rows == 7 and s.headers == BASE


def test_profile_layouts_and_stats(tree):
    res = profile(LocalSource(tree))
    s = res["summary"]
    assert s["total_files"] == 10
    assert s["inspected_files"] == 9
    assert s["status_counts"] == {"ok": 7, "corrupt": 1, "unsupported": 1}
    # BASE, reversed, BASE+Region, 4-col  => 4 exact layouts, reordered shares a set hash
    assert s["distinct_layouts"] == 4
    assert s["distinct_header_sets"] == 3
    # BASE/reversed/+Region cluster (jaccard >= .8); 4-col layout is its own family
    assert s["layout_families"] == 2
    top = res["layouts"][0]
    assert len(top.files) == 4 and top.layout_id == "L01"
    assert s["duplicate_file_groups"] == 1 and s["duplicate_files"] == 1
    assert s["size_bytes"]["count"] == 10
    jun = next(f for f in res["files"] if f["name"] == "jun.xlsx")
    assert jun["sheet_count"] == 2 and jun["data_sheet_count"] == 1
    diffs = res["layout_diffs"]
    assert any(d["reordered"] for d in diffs.values())
    assert any(d["added"] == ["region"] for d in diffs.values())


def test_excel_only_filter(tree):
    res = profile(LocalSource(tree), include_all_files=False)
    assert all(f["extension"] in {".xlsx", ".xls"} for f in res["files"])


def test_cli_writes_outputs(tree, tmp_path):
    out = tmp_path / "out"
    assert main(["local", "--path", str(tree), "--out", str(out)]) == 0
    for name in ["file_inventory.csv", "sheet_headers.csv", "layouts.csv",
                 "header_frequency.csv", "summary.json", "report.html"]:
        assert (out / name).exists()
    assert json.loads((out / "summary.json").read_text())["distinct_layouts"] == 4
