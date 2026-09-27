from pbi_profiler.model import Model, Table, Column
from pbi_profiler.report_model import ReportModel, Page, Visual, FieldRef
from pbi_profiler.profiling.report_rules import run_report_rules


def _rule_ids(findings):
    return {f.rule_id for f in findings}


def test_missing_title_skips_slicers():
    page = Page(
        name="p1",
        visuals=[
            Visual(name="v1", page="p1", visual_type="card", title=None),
            Visual(name="v2", page="p1", visual_type="slicer", title=None),
            Visual(name="v3", page="p1", visual_type="card", title="Has a title"),
        ],
    )
    report = ReportModel(name="R", pages=[page])
    findings = run_report_rules(report)
    missing_title = {f.object_name for f in findings if f.rule_id == "visual-missing-title"}
    assert missing_title == {"p1/v1"}


def test_empty_page_rule():
    report = ReportModel(name="R", pages=[Page(name="empty"), Page(name="full", visuals=[Visual(name="v", page="full")])])
    findings = run_report_rules(report)
    empty = {f.object_name for f in findings if f.rule_id == "empty-page"}
    assert empty == {"empty"}


def test_high_field_count_rule():
    many_fields = [FieldRef(table="T", field=f"C{i}") for i in range(9)]
    page = Page(name="p1", visuals=[Visual(name="big", page="p1", fields=many_fields)])
    report = ReportModel(name="R", pages=[page])
    findings = run_report_rules(report)
    assert any(f.rule_id == "high-field-count-visual" for f in findings)


def test_duplicate_visual_rule():
    fields = [FieldRef(table="T", field="C1")]
    page = Page(
        name="p1",
        visuals=[
            Visual(name="v1", page="p1", visual_type="card", fields=list(fields)),
            Visual(name="v2", page="p1", visual_type="card", fields=list(fields)),
        ],
    )
    report = ReportModel(name="R", pages=[page])
    findings = run_report_rules(report)
    dup = [f for f in findings if f.rule_id == "duplicate-visual"]
    assert len(dup) == 1
    assert dup[0].object_name == "p1/v2"


def test_broken_field_reference_requires_model():
    page = Page(
        name="p1",
        visuals=[Visual(name="v1", page="p1", fields=[FieldRef(table="Nope", field="X")])],
    )
    report = ReportModel(name="R", pages=[page])

    findings_without_model = run_report_rules(report, model=None)
    assert "broken-field-reference" not in _rule_ids(findings_without_model)

    model = Model(name="M", tables=[Table(name="Sales")])
    findings_with_model = run_report_rules(report, model=model)
    broken = [f for f in findings_with_model if f.rule_id == "broken-field-reference"]
    assert len(broken) == 1
    assert "Nope" in broken[0].message


def test_broken_field_reference_detects_missing_column_and_measure():
    sales = Table(name="Sales")
    sales.columns = [Column(name="Amount", table="Sales")]
    model = Model(name="M", tables=[sales])

    page = Page(
        name="p1",
        visuals=[
            Visual(
                name="v1",
                page="p1",
                fields=[
                    FieldRef(table="Sales", field="Amount", kind="column"),
                    FieldRef(table="Sales", field="MissingCol", kind="column"),
                    FieldRef(table="Sales", field="MissingMeasure", kind="measure"),
                ],
            )
        ],
    )
    report = ReportModel(name="R", pages=[page])
    findings = run_report_rules(report, model=model)
    broken_objects = {f.object_name for f in findings if f.rule_id == "broken-field-reference"}
    assert "p1/v1: Sales[Amount]" not in broken_objects
    assert "p1/v1: Sales[MissingCol]" in broken_objects
    assert "p1/v1: Sales[MissingMeasure]" in broken_objects
