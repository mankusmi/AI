import csv

from migration_planner.analysis import analyze
from migration_planner.cli import main
from migration_planner.manifest import load_manifest

from .conftest import PBIR_SAMPLE, TMDL_SAMPLE


def _manifest(tmp_path, statuses):
    p = tmp_path / "dashboards.csv"
    rows = ["dashboard,status,report_source,report_path,model_source,model_path"]
    for name, st in statuses.items():
        rows.append(f"{name},{st},pbir,{PBIR_SAMPLE},tmdl,{TMDL_SAMPLE}")
    p.write_text("\n".join(rows))
    return p


def test_replaced_dashboard_expands_measures_to_columns(tmp_path):
    a = analyze(load_manifest(_manifest(tmp_path, {"A": "replace"})))
    keys = {r["datapoint"]: r for r in a.shopping_list()}
    assert keys["Sales[Total Sales]"]["kind"] == "measure"
    assert "SUM(Sales[Amount])" in keys["Sales[Total Sales]"]["definition"]
    assert keys["Sales[Amount]"]["required_via_measures"] == "Sales[Total Sales]"
    assert keys["Sales[Region]"]["kind"] == "column"


def test_stay_only_excluded_and_shared_flagged(tmp_path):
    a = analyze(load_manifest(_manifest(tmp_path, {"A": "replace", "B": "stay"})))
    assert all(r["decision"].startswith("SHARED") for r in a.shopping_list())
    a = analyze(load_manifest(_manifest(tmp_path, {"A": "stay"})))
    assert a.shopping_list() == [] and a.stay_only()
    a = analyze(load_manifest(_manifest(tmp_path, {"A": "retire"})))
    assert a.shopping_list() == [] and a.stay_only() == []


def test_priority_counts_dashboards(tmp_path):
    a = analyze(load_manifest(_manifest(tmp_path, {"A": "replace", "B": "replace", "C": "replace"})))
    assert {r["priority"] for r in a.shopping_list()} == {"High"}


def test_cli_build(tmp_path):
    m = _manifest(tmp_path, {"A": "replace"})
    assert main(["build", "-m", str(m), "-o", str(tmp_path / "out")]) == 0
    with (tmp_path / "out" / "shopping_list.csv").open(encoding="utf-8-sig") as fh:
        assert list(csv.DictReader(fh))
    assert (tmp_path / "out" / "shopping_list.html").exists()


def test_snowflake_catalog_matching_and_xlsx(tmp_path):
    m = _manifest(tmp_path, {"A": "replace"})
    cat = tmp_path / "cols.csv"
    cat.write_text("TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE\n"
                   "DW,MART,SALES,AMOUNT,NUMBER\nDW,MART,SALES,REGION,TEXT\n"
                   "DW,MART,OTHER,REGION,TEXT\nDW,MART,FACT,COST,NUMBER\nDW,X,FACT2,COST,NUMBER\n")
    out = tmp_path / "out"
    assert main(["build", "-m", str(m), "-o", str(out), "--snowflake-columns", str(cat)]) == 0
    with (out / "shopping_list.csv").open(encoding="utf-8-sig") as fh:
        rows = {r["datapoint"]: r for r in csv.DictReader(fh)}
    assert rows["Sales[Amount]"]["snowflake_table"] == "SALES" and rows["Sales[Amount]"]["match"] == "exact"
    assert rows["Sales[Region]"]["match"] == "exact"  # exact table wins over OTHER
    assert rows["Sales[Cost]"]["match"].startswith("ambiguous")
    assert rows["Sales[Total Sales]"]["decision"] == "recreate logic"
    from openpyxl import load_workbook
    wb = load_workbook(out / "shopping_list.xlsx")
    assert wb.sheetnames[:2] == ["Columns to source", "Measures & logic"]


def test_scan_pbip_folder(tmp_path):
    import json
    import shutil
    proj = tmp_path / "proj"
    shutil.copytree(PBIR_SAMPLE, proj / "Sales.Report")
    shutil.copytree(TMDL_SAMPLE, proj / "Sales.SemanticModel")
    (proj / "Sales.Report" / "definition.pbir").write_text(
        json.dumps({"datasetReference": {"byPath": {"path": "../Sales.SemanticModel"}}}))
    live = proj / "Live.Report"
    shutil.copytree(PBIR_SAMPLE, live)
    (live / "definition.pbir").write_text(json.dumps({"datasetReference": {"byConnection": {"connectionString": "x"}}}))
    mf = tmp_path / "dashboards.csv"
    assert main(["scan", str(proj), "-o", str(mf)]) == 0
    with mf.open() as fh:
        rows = list(csv.DictReader(fh))
    assert [r["dashboard"] for r in rows] == ["Sales"]  # live-connected one skipped with warning
    assert rows[0]["report_source"] == "pbir" and rows[0]["model_source"] == "tmdl"
    import pytest
    with pytest.raises(ValueError, match="fill in the status"):
        load_manifest(mf)
    rows[0]["status"] = "replace"
    with mf.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert analyze(load_manifest(mf)).shopping_list()
