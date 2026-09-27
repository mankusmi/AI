from pbi_profiler.report_loaders.pbir_loader import PbirReportLoader
from .conftest import PBIR_SAMPLE


def load():
    return PbirReportLoader(PBIR_SAMPLE).load()


def test_report_basics():
    report = load()
    assert report.name == "SalesReport"
    assert report.source_kind == "pbir"
    assert [p.name for p in report.pages] == ["PageA", "PageB"]


def test_page_metadata_and_hidden_flag():
    report = load()
    page_a, page_b = report.pages
    assert page_a.display_name == "Overview"
    assert page_a.is_hidden is False
    assert page_b.display_name == "Drillthrough"
    assert page_b.is_hidden is True


def test_visual_fields_and_roles():
    report = load()
    page_a = report.pages[0]
    vis1 = next(v for v in page_a.visuals if v.name == "vis1")
    assert vis1.visual_type == "columnChart"
    assert vis1.title == "Sales by Region"

    by_role = {f.role: f for f in vis1.fields}
    assert by_role["Category"].table == "Sales"
    assert by_role["Category"].field == "Region"
    assert by_role["Category"].kind == "column"
    assert by_role["Y"].field == "Total Sales"
    assert by_role["Y"].kind == "measure"


def test_aggregation_field_unwraps_to_underlying_column():
    report = load()
    vis2 = next(v for v in report.pages[0].visuals if v.name == "vis2")
    assert vis2.title is None
    assert len(vis2.fields) == 1
    f = vis2.fields[0]
    assert f.kind == "aggregation"
    assert f.table == "Sales"
    assert f.field == "Cost"
