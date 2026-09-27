from pbi_profiler.model import Model, Table, Column, Measure
from pbi_profiler.profiling.dax_deps import find_references, analyze_model


def test_find_references_qualified_and_bare():
    expr = "CALCULATE(SUM(Sales[Amount]), 'Date'[Year] = 2024) + [Some Measure]"
    qualified, bare = find_references(expr)
    assert ("Sales", "Amount") in qualified
    assert ("Date", "Year") in qualified
    assert "Some Measure" in bare


def test_bare_ref_not_double_counted_as_qualified():
    expr = "Sales[Amount] + [Total]"
    qualified, bare = find_references(expr)
    assert qualified == {("Sales", "Amount")}
    assert bare == {"Total"}


def _sample_model() -> Model:
    sales = Table(name="Sales")
    sales.columns = [
        Column(name="Amount", table="Sales"),
        Column(name="Unused", table="Sales"),
    ]
    sales.measures = [
        Measure(name="Total Sales", table="Sales", expression="SUM(Sales[Amount])"),
        Measure(name="Double Total", table="Sales", expression="[Total Sales] * 2"),
    ]
    return Model(name="M", tables=[sales])


def test_analyze_model_marks_used_and_unused():
    model = _sample_model()
    usage = analyze_model(model)
    assert usage.is_column_used("Sales", "Amount") is True
    assert usage.is_column_used("Sales", "Unused") is False
    assert usage.is_measure_used("Total Sales") is True
    assert usage.is_measure_used("Double Total") is False
