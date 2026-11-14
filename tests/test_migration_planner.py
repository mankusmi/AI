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
