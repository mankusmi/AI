from pbi_profiler.loaders.tmdl_loader import TmdlModelLoader
from pbi_profiler.model import Model, Table, Measure, Relationship
from pbi_profiler.profiling.graph import MeasureDependencyEdge, build_measure_dependency_edges
from pbi_profiler.report.graph_svg import render_measure_dependency_svg, render_relationship_svg
from .conftest import TMDL_SAMPLE


def test_build_measure_dependency_edges_against_tmdl_fixture():
    model = TmdlModelLoader(TMDL_SAMPLE).load()
    edges = build_measure_dependency_edges(model)
    assert set(edges) == {
        MeasureDependencyEdge("Sales[Margin Pct]", "Sales[Total Sales]"),
        MeasureDependencyEdge("Sales[Sales YTD]", "Sales[Total Sales]"),
    }
    # sorted deterministically
    assert edges == sorted(edges, key=lambda e: (e.from_measure, e.to_measure))


def test_no_edge_for_unrelated_measure():
    t = Table(name="T")
    t.measures = [
        Measure(name="A", table="T", expression="SUM(T[X])"),
        Measure(name="B", table="T", expression="SUM(T[Y])"),  # doesn't reference A
    ]
    model = Model(name="M", tables=[t])
    assert build_measure_dependency_edges(model) == []


def test_ambiguous_same_name_measure_links_both_tables():
    t1 = Table(name="T1")
    t1.measures = [Measure(name="Shared", table="T1")]
    t2 = Table(name="T2")
    t2.measures = [Measure(name="Shared", table="T2")]
    t3 = Table(name="T3")
    t3.measures = [Measure(name="Caller", table="T3", expression="[Shared] * 2")]
    model = Model(name="M", tables=[t1, t2, t3])

    edges = set(build_measure_dependency_edges(model))
    assert edges == {
        MeasureDependencyEdge("T3[Caller]", "T1[Shared]"),
        MeasureDependencyEdge("T3[Caller]", "T2[Shared]"),
    }


def test_render_measure_dependency_svg_empty_and_nonempty():
    assert render_measure_dependency_svg([]) is None

    edges = [MeasureDependencyEdge("Sales[Margin Pct]", "Sales[Total Sales]")]
    svg = render_measure_dependency_svg(edges)
    assert svg.startswith("<svg")
    assert svg.count("<g class=\"dep-node\">") == 2
    assert "Sales[Margin Pct]" in svg
    assert "Sales[Total Sales]" in svg
    assert 'marker-end="url(#dep-arrow-measure)"' in svg


def test_render_relationship_svg_empty_and_nonempty():
    assert render_relationship_svg([]) is None

    rel = Relationship(from_table="Sales", from_column="DateKey", to_table="Date", to_column="Date")
    svg = render_relationship_svg([rel])
    assert svg.startswith("<svg")
    assert svg.count("<g class=\"dep-node\">") == 2
    assert "Sales" in svg and "Date" in svg


def test_relationship_svg_marks_inactive_and_bidirectional():
    rel = Relationship(
        from_table="Sales",
        from_column="Region",
        to_table="Date",
        to_column="MonthName",
        is_active=False,
        cross_filtering_behavior="bothDirections",
    )
    svg = render_relationship_svg([rel])
    assert "dep-edge-inactive" in svg
    assert svg.count('marker-start="url(#dep-arrow-rel)"') == 1
    assert svg.count('marker-end="url(#dep-arrow-rel)"') == 1


def test_relationship_svg_shows_cardinality_label():
    rel = Relationship(
        from_table="Sales",
        from_column="DateKey",
        to_table="Date",
        to_column="Date",
        from_cardinality="many",
        to_cardinality="one",
    )
    svg = render_relationship_svg([rel])
    assert "many:one" in svg


def test_parallel_relationship_edges_between_same_tables_fixture():
    model = TmdlModelLoader(TMDL_SAMPLE).load()
    svg = render_relationship_svg(model.relationships)
    # two relationships between Sales and Date -> two distinct <path> edges
    assert svg.count('class="dep-edge') == 2


def test_svg_rendering_is_deterministic():
    model = TmdlModelLoader(TMDL_SAMPLE).load()
    edges = build_measure_dependency_edges(model)
    assert render_measure_dependency_svg(edges) == render_measure_dependency_svg(edges)
    assert render_relationship_svg(model.relationships) == render_relationship_svg(model.relationships)


def test_cyclic_measure_edges_do_not_hang_or_raise():
    edges = [
        MeasureDependencyEdge("A[X]", "A[Y]"),
        MeasureDependencyEdge("A[Y]", "A[X]"),
    ]
    svg = render_measure_dependency_svg(edges)
    assert svg is not None
    assert svg.startswith("<svg")
    assert svg.count("<g class=\"dep-node\">") == 2
