import json

from pbi_dataflows.analysis import analyze, parse_queries
from pbi_dataflows.cli import main
from pbi_dataflows.client import DataflowClient, fetch_workspace
from pbi_dataflows.store import load_snapshot

WS = "11111111-1111-1111-1111-111111111111"
A, B, C = ("aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002", "cccccccc-0000-0000-0000-000000000003")


def model(name, mashup, entities):
    return {"name": name, "pbi:mashup": {"document": "section Section1;\n" + mashup}, "entities": entities}


DEFS = {
    # C is listed first alphabetically-last but must run last; B depends on A via linked entity; C on B via M code
    A: model("Raw", 'shared Orders = let Source = Sql.Database("srv", "sales"), r = Table.SelectRows(Source, each true) in r;',
             [{"$type": "LocalEntity", "name": "Orders", "attributes": [{"name": "Id"}]}]),
    B: model("Clean", 'shared Orders = let s = PowerPlatform.Dataflows(null), d = s{[workspaceId="%s"]}[Data]{[dataflowId="%s"]}[Data] in d;' % (WS, A),
             [{"$type": "ReferenceEntity", "name": "Orders", "source": "Orders"}]),
    C: model("Agg", 'shared #"Sales Sum" = let s = Dataflows{[dataflowId="%s"]}, g = Table.Group(s, {}, {}) in g;' % B,
             [{"$type": "LocalEntity", "name": "Sales Sum", "attributes": []}]),
}


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.headers, self.text = data, status, {}, json.dumps(data)

    def json(self):
        return self.data


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        path = url.split("/myorg")[1]
        self.calls.append(path)
        flows = [{"objectId": i, "name": DEFS[i]["name"]} for i in (C, A, B)]
        if path == "/groups":
            return Resp({"value": [{"id": WS, "name": "Sales WS", "type": "Workspace"}]})
        if path.endswith("/dataflows"):
            return Resp({"value": flows})
        if path.endswith("/datasets"):
            return Resp({"value": [{"id": "d1", "name": "Model"}]})
        if path.endswith("/datasets/upstreamDataflows"):
            return Resp({"value": [{"datasetObjectId": "d1", "dataflowObjectId": C}]})
        if path.endswith("/users"):
            return Resp({"error": "no"}, 403)
        for i in DEFS:
            if path == f"/groups/{WS}/dataflows/{i}":
                return Resp(DEFS[i])
            if path.endswith(f"{i}/transactions"):
                return Resp({"value": [{"status": "Success", "startTime": "2026-01-01T00:00:00Z"}]})
        return Resp({"value": []})


def snapshot():
    return fetch_workspace(DataflowClient(lambda: "t", session=FakeSession()), WS)


def test_run_order_follows_dependencies_not_listing_order():
    res = analyze(snapshot())
    assert [f["name"] for f in res["flows"]] == ["Raw", "Clean", "Agg"]
    assert [f["stage"] for f in res["flows"]] == [1, 2, 3]
    assert res["flows"][2]["datasets"] == ["Model"]
    assert res["flows"][0]["downstream"] == [B]


def test_permission_failure_becomes_warning():
    assert any(w.startswith("users") for w in snapshot()["warnings"])


def test_parse_queries_finds_sources_and_steps():
    q = parse_queries(DEFS[A])[0]
    assert q["sources"][0]["kind"] == "SQL Server" and q["sources"][0]["detail"] == "srv / sales"
    assert q["steps"] == {"filters rows": 1}
    assert parse_queries(DEFS[C])[0]["name"] == "Sales Sum"


def test_cycle_does_not_hang():
    snap = snapshot()
    snap["dataflows"][1]["upstream"] = [{"targetDataflowId": C}]  # A <- C <- B <- A
    res = analyze(snap)
    assert len(res["flows"]) == 3 and any("cycle" in w for w in res["warnings"])


def test_cli_roundtrip(tmp_path, monkeypatch):
    import pbi_dataflows.client as c
    monkeypatch.setattr(c, "DataflowClient", lambda tp: DataflowClient(tp, session=FakeSession()))
    assert main(["fetch", "--workspace-id", WS, "--token", "x", "-o", str(tmp_path)]) == 0
    out = tmp_path / "Sales_WS"
    assert sorted(p.name for p in (out / "dataflows").iterdir()) == ["01_Raw.json", "02_Clean.json", "03_Agg.json"]
    assert "Sales WS" in (out / "summary.html").read_text()
    assert "Dataflow run order" in (out / "SUMMARY.md").read_text()
    assert len(list((out / "docs").iterdir())) == 3
    assert load_snapshot(out)["dataflows"][0]["definition"]["name"] == "Raw"
    assert main(["document", str(out)]) == 0
