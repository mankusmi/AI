from pbi_profiler.loaders.base import QueryExecutor
from pbi_profiler.model import Model, Table, Column
from pbi_profiler.profiling.data import compute_data_profile


class FakeExecutor(QueryExecutor):
    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    def run_dax(self, query):
        self.queries.append(query)
        for match, rows in self.responses:
            if match in query:
                return rows
        raise AssertionError(f"Unexpected query: {query}")


def _model():
    t = Table(name="Sales")
    t.columns = [Column(name="Amount", table="Sales", data_type="double")]
    return Model(name="M", tables=[t])


def test_data_profile_happy_path():
    executor = FakeExecutor(
        [("ROW(", [{"[RowCount]": 100, "[c0_distinct]": 10, "[c0_nulls]": 5, "[c0_min]": 1, "[c0_max]": 500}])]
    )
    profile = compute_data_profile(_model(), executor)
    assert len(profile.tables) == 1
    t = profile.tables[0]
    assert t.row_count == 100
    assert t.error is None
    col = t.columns[0]
    assert col.distinct_count == 10
    assert col.null_count == 5
    assert col.null_percentage == 5.0
    assert col.min_value == 1
    assert col.max_value == 500


def test_data_profile_falls_back_to_row_count_on_error():
    class FlakyExecutor(QueryExecutor):
        def __init__(self):
            self.calls = 0

        def run_dax(self, query):
            self.calls += 1
            if "DISTINCTCOUNT" in query:
                raise RuntimeError("engine rejected query")
            return [{"[RowCount]": 42}]

    executor = FlakyExecutor()
    profile = compute_data_profile(_model(), executor)
    t = profile.tables[0]
    assert t.row_count == 42
    assert t.error is not None
    assert "per-column stats failed" in t.error
