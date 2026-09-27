from pbi_profiler.loaders.bim_loader import BimModelLoader
from .conftest import BIM_SAMPLE


def load():
    return BimModelLoader(BIM_SAMPLE).load()


def test_model_basics():
    model = load()
    assert model.name == "MyModel"
    assert model.source_kind == "bim"
    assert {t.name for t in model.tables} == {"Sales", "Date"}


def test_calculated_column_and_measure_list_expression():
    model = load()
    sales = model.get_table("Sales")
    full_desc = sales.get_column("Full Description")
    assert full_desc.is_calculated is True

    ytd = sales.get_measure("Sales YTD")
    assert "CALCULATE(" in ytd.expression
    assert "\n" in ytd.expression  # list-of-lines expression got joined


def test_relationships_and_roles_match_tmdl_shape():
    model = load()
    assert len(model.relationships) == 2
    inactive = [r for r in model.relationships if not r.is_active]
    assert len(inactive) == 1
    assert inactive[0].cross_filtering_behavior == "bothDirections"

    assert len(model.roles) == 1
    assert model.roles[0].table_permissions[0].table == "Sales"
