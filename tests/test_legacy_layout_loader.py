from pbi_profiler.report_loaders.legacy_layout_loader import LegacyLayoutReportLoader
from .conftest import LEGACY_LAYOUT_SAMPLE


def load():
    return LegacyLayoutReportLoader(LEGACY_LAYOUT_SAMPLE).load()


def test_report_basics():
    report = load()
    assert report.source_kind == "legacy-layout"
    assert len(report.pages) == 1
    page = report.pages[0]
    assert page.display_name == "Page 1"
    assert len(page.visuals) == 2


def test_alias_resolution_via_from_clause():
    report = load()
    vis1 = report.pages[0].visuals[0]
    assert vis1.visual_type == "columnChart"
    assert vis1.title == "Sales by Region (legacy)"
    by_role = {f.role: f for f in vis1.fields}
    assert by_role["Category"].table == "Sales"
    assert by_role["Category"].field == "Region"
    assert by_role["Y"].table == "Sales"
    assert by_role["Y"].field == "Amount"
    assert by_role["Y"].kind == "aggregation"


def test_second_visual_has_no_title():
    report = load()
    vis2 = report.pages[0].visuals[1]
    assert vis2.title is None
    assert vis2.fields[0].field == "UnknownCol"
