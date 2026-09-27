from pbi_profiler.loaders.base import QueryExecutor
from pbi_profiler.loaders.live_loader import LiveModelLoader, PowerBiRestClient, PowerBiAuth


class FakeExecutor(QueryExecutor):
    def __init__(self, table_rows):
        self.table_rows = table_rows

    def run_dax(self, query):
        for marker, rows in self.table_rows.items():
            if marker in query:
                return rows
        return []


def test_live_loader_builds_model_from_info_view_rows():
    rows = {
        "INFO.VIEW.TABLES": [{"[Name]": "Sales", "[IsHidden]": False}],
        "INFO.VIEW.COLUMNS": [
            {
                "[Table]": "Sales",
                "[Column]": "Amount",
                "[DataType]": "Double",
                "[Type]": "Data",
                "[SummarizeBy]": "Sum",
                "[IsHidden]": False,
            }
        ],
        "INFO.VIEW.MEASURES": [
            {"[Table]": "Sales", "[Name]": "Total Sales", "[Expression]": "SUM(Sales[Amount])"}
        ],
        "INFO.VIEW.RELATIONSHIPS": [
            {
                "[FromTable]": "Sales",
                "[FromColumn]": "RegionKey",
                "[ToTable]": "Region",
                "[ToColumn]": "RegionKey",
                "[IsActive]": True,
                "[CrossFilteringBehavior]": "Single",
            }
        ],
        "INFO.ROLES": [{"[Name]": "Viewer", "[ModelPermission]": "Read"}],
    }
    loader = LiveModelLoader(FakeExecutor(rows), model_name="LiveModel")
    model = loader.load()

    assert model.name == "LiveModel"
    assert model.source_kind == "live"
    sales = model.get_table("Sales")
    assert sales is not None
    assert sales.get_column("Amount").data_type == "Double"
    assert sales.get_measure("Total Sales").expression == "SUM(Sales[Amount])"
    assert len(model.relationships) == 1
    assert model.relationships[0].to_table == "Region"
    assert model.roles[0].name == "Viewer"


def test_live_loader_tolerates_unsupported_info_functions():
    class AlwaysFailsExecutor(QueryExecutor):
        def run_dax(self, query):
            raise RuntimeError("function not supported on this engine")

    loader = LiveModelLoader(AlwaysFailsExecutor())
    model = loader.load()  # should not raise
    assert model.tables == []
    assert model.relationships == []


def test_power_bi_rest_client_posts_expected_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"tables": [{"rows": [{"[RowCount]": 5}]}]}]}

    class FakeSession:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    class FakeAuth:
        def get_token(self):
            return "fake-token"

    client = PowerBiRestClient("ws-id", "ds-id", FakeAuth(), session=FakeSession())
    rows = client.run_dax("EVALUATE ROW(\"RowCount\", COUNTROWS(Sales))")

    assert rows == [{"[RowCount]": 5}]
    assert captured["url"].endswith("/groups/ws-id/datasets/ds-id/executeQueries")
    assert captured["headers"]["Authorization"] == "Bearer fake-token"
    assert captured["json"]["queries"][0]["query"].startswith("EVALUATE")


def test_power_bi_auth_requires_tenant_and_client_id(monkeypatch):
    import pytest

    monkeypatch.delenv("PBI_TENANT_ID", raising=False)
    monkeypatch.delenv("PBI_CLIENT_ID", raising=False)
    with pytest.raises(ValueError):
        PowerBiAuth(tenant_id=None, client_id=None)
