from pbi_profiler.loaders.tmdl_loader import TmdlModelLoader
from .conftest import TMDL_SAMPLE


def load():
    return TmdlModelLoader(TMDL_SAMPLE).load()


def test_model_basics():
    model = load()
    assert model.name == "MyModel"
    assert model.source_kind == "tmdl"
    assert model.culture == "en-US"
    assert {t.name for t in model.tables} == {"Sales", "Date"}


def test_sales_table_columns_and_measures():
    model = load()
    sales = model.get_table("Sales")
    assert sales is not None
    assert len(sales.columns) == 7
    assert len(sales.measures) == 3

    order_date_key = sales.get_column("OrderDateKey")
    assert order_date_key.is_hidden is True
    assert order_date_key.data_type == "int64"

    full_desc = sales.get_column("Full Description")
    assert full_desc.is_calculated is True
    assert full_desc.expression == 'Sales[ProductName] & " - " & Sales[Region]'

    ytd = sales.get_measure("Sales YTD")
    assert "CALCULATE(" in ytd.expression
    assert "DATESYTD(" in ytd.expression
    assert ytd.format_string == "#,0"


def test_date_table_hierarchy():
    model = load()
    date_table = model.get_table("Date")
    assert len(date_table.hierarchies) == 1
    hierarchy = date_table.hierarchies[0]
    assert hierarchy.name == "Date Hierarchy"
    assert hierarchy.levels == ["Year", "MonthName"]
    key_col = date_table.get_column("Date")
    assert key_col.is_key is True


def test_relationships():
    model = load()
    assert len(model.relationships) == 2
    active = [r for r in model.relationships if r.is_active]
    inactive = [r for r in model.relationships if not r.is_active]
    assert len(active) == 1
    assert len(inactive) == 1
    assert active[0].from_table == "Sales"
    assert active[0].from_column == "OrderDateKey"
    assert active[0].to_table == "Date"
    assert active[0].to_column == "Date"
    assert inactive[0].cross_filtering_behavior == "bothDirections"


def test_roles():
    model = load()
    assert len(model.roles) == 1
    role = model.roles[0]
    assert role.name == "Viewer"
    assert role.model_permission == "read"
    assert len(role.table_permissions) == 1
    assert role.table_permissions[0].table == "Sales"
    assert "Region" in role.table_permissions[0].filter_expression
