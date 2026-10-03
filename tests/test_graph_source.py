"""GraphSource / BrowserAuth against a fake Graph (no network)."""
import io
import json
import os
import stat
import sys
import types
from pathlib import Path

import openpyxl
import pytest
import requests

from sp_profiler import sources
from sp_profiler.profiler import profile
from sp_profiler.sources import GRAPH, BrowserAuth, GraphSource

SITE = "https://contoso.sharepoint.com/sites/Claims"


def xlsx_bytes(headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Title row"])
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class Resp:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.raw = io.BytesIO(content)
        self.closed = False

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class FakeAuth:
    def __init__(self):
        self.calls = 0

    def token(self):
        self.calls += 1
        return f"tok{self.calls}"


class FakeSession:
    """Routes by URL substring; handlers may be a Resp, a list of Resps (consumed in order) or a callable."""

    def __init__(self, routes):
        self.routes, self.log = routes, []

    def get(self, url, headers=None, stream=False, timeout=None):
        self.log.append((url, dict(headers or {})))
        for key, h in self.routes:
            if key in url:
                if isinstance(h, list):
                    h = h.pop(0) if len(h) > 1 else h[0]
                return h(url) if callable(h) else h
        raise AssertionError(f"unexpected URL {url}")


A = xlsx_bytes(["Policy No", "Premium", "Currency"], [["P1", 10, "GBP"], ["P2", 20, "EUR"]])
B = xlsx_bytes(["Policy No", "Premium", "Currency"], [["P3", 5, "USD"]])


def item(name, size, id_, url=None, folder=False):
    d = {"id": id_, "name": name, "size": size, "webUrl": f"{SITE}/Shared Documents/{name}",
         "createdDateTime": "2024-01-01T00:00:00Z", "lastModifiedDateTime": "2024-02-01T00:00:00Z",
         "eTag": f'"{id_}"', "cTag": f'"c{id_}"',
         "createdBy": {"user": {"displayName": "Ann"}},
         "lastModifiedBy": {"user": {"displayName": "Bob", "email": "bob@x.com"}}}
    if folder:
        d["folder"] = {"childCount": 1}
    else:
        d["file"] = {"mimeType": "application/vnd.ms-excel", "hashes": {"quickXorHash": "qx" + id_}}
        if url:
            d["@microsoft.graph.downloadUrl"] = url
    return d


def routes(extra=()):
    return [
        (f"/sites/contoso.sharepoint.com:/sites/Claims", Resp(body={"id": "site1"})),
        ("/sites/site1/drives", Resp(body={"value": [{"id": "d0", "name": "Other"}, {"id": "d1", "name": "Documents"}]})),
        ("/drives/d1/root:/Bordereaux/2024:/children", Resp(body={
            "value": [item("jan.xlsx", len(A), "i1", "https://dl.example/jan"), item("Q2", 0, "f1", folder=True)],
            "@odata.nextLink": f"{GRAPH}/page2"})),
        ("/page2", Resp(body={"value": [item("feb.xlsx", len(B), "i2", "https://dl.example/feb")]})),
        ("/drives/d1/root:/Bordereaux/2024/Q2:/children", Resp(body={"value": [item("apr.xlsx", len(B), "i3", "https://dl.example/apr")]})),
        ("https://dl.example/jan", Resp(content=A)),
        ("https://dl.example/feb", Resp(content=B)),
        ("https://dl.example/apr", Resp(content=B)),
        *extra,
    ]


def make(extra=(), sleeps=None):
    s = FakeSession(routes(extra))
    src = GraphSource(SITE, "Bordereaux/2024", "Documents", auth=FakeAuth(), session=s)
    src._sleep = (sleeps.append if sleeps is not None else (lambda x: None))
    return src, s


def test_listing_pagination_recursion_and_metadata():
    src, _ = make()
    assert src.drive_id == "d1"
    files = {f.rel_path: f for f in src.iter_files()}
    assert sorted(files) == ["Q2/apr.xlsx", "feb.xlsx", "jan.xlsx"]
    jan = files["jan.xlsx"]
    assert jan.size_bytes == len(A) and jan.modified_by == "Bob" and jan.extension == ".xlsx"
    assert jan.meta["item_id"] == "i1" and jan.meta["created_by"] == "Ann" and jan.meta["quickxor_hash"] == "qxi1"
    assert jan.meta["etag"] == '"i1"' and jan.folder == "" and files["Q2/apr.xlsx"].folder == "Q2"


def test_library_not_found_lists_available():
    s = FakeSession(routes()[:2])
    with pytest.raises(LookupError, match="Other"):
        GraphSource(SITE, "", "Missing", auth=FakeAuth(), session=s)


def test_download_materialises_then_cleans_up_and_sends_no_bearer_to_signed_url():
    src, s = make()
    jan = next(f for f in src.iter_files() if f.name == "jan.xlsx")
    with jan.materialize() as p:
        assert p.read_bytes() == A
        tmp = p.parent
    assert not tmp.exists()
    url, headers = [x for x in s.log if x[0] == "https://dl.example/jan"][0]
    assert "Authorization" not in headers


def test_throttling_retry_after_and_server_errors():
    sleeps = []
    src, s = make(extra=[("/throttled", [Resp(429, headers={"Retry-After": "7"}), Resp(503), Resp(200, body={"ok": 1})])], sleeps=sleeps)
    assert src._get(f"{GRAPH}/throttled") == {"ok": 1}
    assert sleeps == [7, 2]                                   # Retry-After honoured, then exponential backoff


def test_gives_up_after_repeated_failures():
    src, _ = make(extra=[("/always503", Resp(503))])
    with pytest.raises(RuntimeError, match="Giving up"):
        src._request(f"{GRAPH}/always503", attempts=3)


def test_network_error_is_retried():
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("reset")
        return Resp(body={"v": 1})
    src, _ = make(extra=[("/flaky", flaky)])
    assert src._get(f"{GRAPH}/flaky") == {"v": 1} and calls["n"] == 3


def test_expired_download_link_is_refreshed():
    fresh = item("jan.xlsx", len(A), "i1", "https://dl.example/jan-fresh")
    src, s = make(extra=[("/items/i1", Resp(body=fresh)), ("https://dl.example/jan-fresh", Resp(content=A)),
                         ("https://dl.example/old", Resp(403))])
    entry = [f for f in src.iter_files() if f.name == "jan.xlsx"][0]
    entry._materialize = src._download({**item("jan.xlsx", len(A), "i1", "https://dl.example/old")})
    with entry.materialize() as p:
        assert p.read_bytes() == A


def test_profile_end_to_end_over_graph():
    src, _ = make()
    res = profile(src, include_all_files=False)
    s = res["summary"]
    assert s["total_files"] == 3 and s["status_counts"] == {"ok": 3} and s["distinct_layouts"] == 1
    assert {f["rel_path"] for f in res["files"]} == {"jan.xlsx", "feb.xlsx", "Q2/apr.xlsx"}
    assert all(f["item_id"] and f["sp_path"] is not None for f in res["files"])


# ----------------------------------------------------------------- BrowserAuth
class FakeMsalApp:
    instances = []

    def __init__(self, client_id, authority=None, token_cache=None):
        self.client_id, self.authority, self.cache = client_id, authority, token_cache
        self.accounts, self.silent_calls, self.interactive_calls = [], 0, 0
        FakeMsalApp.instances.append(self)

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account):
        self.silent_calls += 1
        return {"access_token": "silent-token"}

    def acquire_token_interactive(self, scopes):
        self.interactive_calls += 1
        self.accounts = [{"username": "me"}]
        return {"access_token": "interactive-token"}


@pytest.fixture
def fake_msal(monkeypatch):
    mod = types.SimpleNamespace(PublicClientApplication=FakeMsalApp, SerializableTokenCache=_Cache)
    monkeypatch.setitem(sys.modules, "msal", mod)
    FakeMsalApp.instances.clear()
    return mod


class _Cache:
    def __init__(self):
        self.has_state_changed = True
        self.data = "{}"

    def deserialize(self, s):
        self.data = s

    def serialize(self):
        return '{"cached": true}'


def test_browser_auth_interactive_then_silent_and_cache_file(fake_msal, tmp_path):
    cache = tmp_path / "sub" / "cache.json"
    auth = BrowserAuth("tenant-guid", "client-guid", cache_path=cache)
    app = FakeMsalApp.instances[0]
    assert app.authority.endswith("/tenant-guid") and app.client_id == "client-guid"
    assert auth.token() == "interactive-token" and app.interactive_calls == 1
    assert json.loads(cache.read_text()) == {"cached": True}
    if os.name != "nt":
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert auth.token() == "silent-token" and app.interactive_calls == 1      # account known -> silent refresh


def test_browser_auth_failure_message(fake_msal, tmp_path, monkeypatch):
    monkeypatch.setattr(FakeMsalApp, "acquire_token_interactive",
                        lambda self, scopes: {"error": "access_denied", "error_description": "admin consent required"})
    auth = BrowserAuth(cache_path=tmp_path / "c.json")
    with pytest.raises(RuntimeError, match="admin consent required"):
        auth.token()
