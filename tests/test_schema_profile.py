from pbi_profiler.loaders.tmdl_loader import TmdlModelLoader
from pbi_profiler.profiling.schema import compute_schema_profile
from .conftest import TMDL_SAMPLE


def test_schema_profile_counts_and_flags():
    model = TmdlModelLoader(TMDL_SAMPLE).load()
    profile = compute_schema_profile(model)

    assert profile.table_count == 2
    assert profile.column_count == 10
    assert profile.calculated_column_count == 1
    assert profile.measure_count == 3
    assert profile.relationship_count == 2
    assert profile.bidirectional_relationship_count == 1
    assert profile.inactive_relationship_count == 1
    assert profile.hierarchy_count == 1

    assert "Sales[UnusedFlag]" in profile.unused_visible_columns
    assert "Sales[Amount]" not in profile.unused_visible_columns
    assert "Sales[Sales YTD]" in profile.unused_measures
    assert "Sales[Total Sales]" not in profile.unused_measures

    # every visible object here is missing a description in the fixture
    assert set(profile.missing_description_tables) == {"Sales", "Date"}
    assert "Sales[Total Sales]" in profile.missing_description_measures
