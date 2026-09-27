from pbi_profiler.model import Model, Table, Column, Measure, Relationship
from pbi_profiler.profiling.data import DataProfile, TableDataProfile, ColumnDataProfile
from pbi_profiler.profiling.rules import run_rules


def _rule_ids(findings):
    return {f.rule_id for f in findings}


def test_naive_division_detected_and_divide_not_flagged():
    t = Table(name="Sales")
    t.measures = [
        Measure(name="Bad", table="Sales", expression="[A] / [B]", format_string="0", is_hidden=False, description="d"),
        Measure(name="Good", table="Sales", expression="DIVIDE([A], [B])", format_string="0", description="d"),
    ]
    model = Model(name="M", tables=[t])
    findings = run_rules(model)
    naive = [f for f in findings if f.rule_id == "naive-division"]
    assert len(naive) == 1
    assert naive[0].object_name == "Sales[Bad]"


def test_bidirectional_and_inactive_relationship_rules():
    t1 = Table(name="A")
    t2 = Table(name="B")
    rel = Relationship(
        from_table="A", from_column="Key", to_table="B", to_column="Key",
        is_active=False, cross_filtering_behavior="bothDirections",
    )
    model = Model(name="M", tables=[t1, t2], relationships=[rel])
    findings = run_rules(model)
    ids = _rule_ids(findings)
    assert "bidirectional-relationship" in ids
    assert "inactive-relationship" in ids
    assert "isolated-table" not in ids  # relationship connects both tables


def test_isolated_table_rule():
    t1 = Table(name="A")
    t2 = Table(name="B")  # no relationships at all
    model = Model(name="M", tables=[t1, t2])
    findings = run_rules(model)
    isolated = {f.object_name for f in findings if f.rule_id == "isolated-table"}
    assert isolated == {"A", "B"}


def test_data_rules_skipped_without_data_profile():
    t = Table(name="A")
    t.columns = [Column(name="C", table="A", description="d")]
    model = Model(name="M", tables=[t])
    findings = run_rules(model, data_profile=None)
    assert not any(f.rule_id in ("empty-table", "high-null-percentage", "potential-key-column") for f in findings)


def test_data_rules_fire_with_data_profile():
    t = Table(name="A")
    t.columns = [Column(name="C", table="A", description="d", is_key=False)]
    model = Model(name="M", tables=[t])
    data = DataProfile(
        tables=[
            TableDataProfile(
                table="A",
                row_count=10,
                columns=[
                    ColumnDataProfile(table="A", column="C", distinct_count=10, null_count=9, null_percentage=90.0)
                ],
            )
        ]
    )
    findings = run_rules(model, data_profile=data)
    ids = _rule_ids(findings)
    assert "potential-key-column" in ids
    assert "empty-table" not in ids  # row_count is 10, not 0


def test_empty_table_rule():
    t = Table(name="A")
    model = Model(name="M", tables=[t])
    data = DataProfile(tables=[TableDataProfile(table="A", row_count=0, columns=[])])
    findings = run_rules(model, data_profile=data)
    assert any(f.rule_id == "empty-table" for f in findings)
