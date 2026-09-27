from pbi_profiler.model import Model, Table, Column, Measure
from pbi_profiler.report_model import ReportModel, Page, Visual, FieldRef
from pbi_profiler.profiling.dax_deps import analyze_model, combine_usage
from pbi_profiler.profiling.visuals import compute_report_profile, usage_from_report
from pbi_profiler.profiling.schema import compute_schema_profile
from .conftest import PBIR_SAMPLE
from pbi_profiler.report_loaders.pbir_loader import PbirReportLoader


def test_compute_report_profile_counts():
    report = PbirReportLoader(PBIR_SAMPLE).load()
    profile = compute_report_profile(report)
    assert profile.page_count == 2
    assert profile.visual_count == 3
    assert profile.hidden_page_count == 1
    assert profile.visuals_by_type == {"columnChart": 1, "card": 1, "slicer": 1}


def _model_with_report_only_column() -> tuple[Model, ReportModel]:
    t = Table(name="Sales")
    t.columns = [
        Column(name="ReportOnlyCol", table="Sales"),  # referenced only by a visual
        Column(name="TrulyUnused", table="Sales"),  # referenced nowhere
    ]
    t.measures = [Measure(name="Total", table="Sales", expression="1 + 1")]  # no field references at all
    model = Model(name="M", tables=[t])

    visual = Visual(
        name="v1",
        page="p1",
        visual_type="card",
        fields=[FieldRef(table="Sales", field="ReportOnlyCol", kind="column", role="Values")],
    )
    report = ReportModel(name="R", pages=[Page(name="p1", visuals=[visual])])
    return model, report


def test_usage_from_report_and_combine_usage_fixes_blind_spot():
    model, report = _model_with_report_only_column()

    model_usage = analyze_model(model)
    assert model_usage.is_column_used("Sales", "ReportOnlyCol") is False  # not referenced by any DAX

    report_usage = usage_from_report(report)
    assert report_usage.is_column_used("Sales", "ReportOnlyCol") is True
    assert report_usage.is_column_used("Sales", "TrulyUnused") is False

    combined = combine_usage(model_usage, report_usage)
    assert combined.is_column_used("Sales", "ReportOnlyCol") is True
    assert combined.is_column_used("Sales", "TrulyUnused") is False


def test_schema_profile_unused_columns_shrink_with_report_usage():
    model, report = _model_with_report_only_column()
    model_usage = analyze_model(model)
    combined = combine_usage(model_usage, usage_from_report(report))

    dax_only_profile = compute_schema_profile(model, model_usage)
    combined_profile = compute_schema_profile(model, combined)

    assert "Sales[ReportOnlyCol]" in dax_only_profile.unused_visible_columns
    assert "Sales[ReportOnlyCol]" not in combined_profile.unused_visible_columns
    assert "Sales[TrulyUnused]" in combined_profile.unused_visible_columns
