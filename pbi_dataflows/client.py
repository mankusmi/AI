"""Thin Power BI REST client (GET only) plus the workspace fetcher.

The client takes a ``token_provider`` callable and an optional ``session`` so it can be unit-tested
without network or auth. ``requests``/``msal`` are only needed for real runs (``pip install -e ".[live]"``).
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Callable, Optional

API_BASE = "https://api.powerbi.com/v1.0/myorg"


class PowerBiApiError(RuntimeError):
    def __init__(self, status: int, path: str, body: str):
        super().__init__(f"HTTP {status} for {path}: {body[:300]}")
        self.status = status
        self.path = path


class DataflowClient:
    def __init__(self, token_provider: Callable[[], str], session: Any = None, api_base: str = API_BASE,
                 max_retries: int = 4, sleep: Callable[[float], None] = time.sleep):
        self._token = token_provider
        self._session = session
        self.api_base = api_base
        self.max_retries = max_retries
        self._sleep = sleep

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.api_base}{path}"
        for attempt in range(self.max_retries + 1):
            r = self.session.get(url, headers={"Authorization": f"Bearer {self._token()}"}, params=params, timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < self.max_retries:
                    try:
                        wait = float(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                    except (TypeError, ValueError):
                        wait = 2 ** (attempt + 1)
                    self._sleep(min(wait, 120))
                    continue
            if r.status_code >= 400:
                raise PowerBiApiError(r.status_code, path, r.text)
            return r.json()
        raise AssertionError("unreachable")

    def get_list(self, path: str, params: Optional[dict] = None) -> list:
        data = self.get(path, params)
        return data.get("value", []) if isinstance(data, dict) else []


def fetch_workspace(client: DataflowClient, workspace_id: str, progress: Callable[[str], None] = lambda m: None,
                    include_transactions: bool = True, include_users: bool = True) -> dict:
    """Pull workspace metadata, sibling objects and every dataflow (definition, datasources, upstream, refreshes).

    Anything the caller lacks permission for is recorded under ``warnings`` instead of aborting the run.
    """
    warnings: list[str] = []

    def best_effort(label: str, path: str, params: Optional[dict] = None) -> list:
        try:
            return client.get_list(path, params)
        except PowerBiApiError as e:
            warnings.append(f"{label}: {e}")
            return []

    ws = {"id": workspace_id}
    found = best_effort("workspace info", "/groups", {"$filter": f"id eq '{workspace_id}'"})
    if found:
        ws = found[0]
    progress(f"workspace: {ws.get('name', workspace_id)}")

    base = f"/groups/{workspace_id}"
    objects = {}
    for kind in ("datasets", "reports", "dashboards", "dataflows"):
        objects[kind] = best_effort(kind, f"{base}/{kind}")
        progress(f"  {len(objects[kind])} {kind}")
    users = best_effort("users", f"{base}/users") if include_users else []
    dataset_upstream = best_effort("dataset upstream dataflows", f"{base}/datasets/upstreamDataflows")

    flows = []
    for item in objects["dataflows"]:
        fid = item.get("objectId") or item.get("id")
        progress(f"fetching dataflow: {item.get('name', fid)}")
        entry: dict = {"id": fid, "name": item.get("name", fid), "meta": item, "definition": None,
                       "datasources": [], "upstream": [], "transactions": []}
        try:
            entry["definition"] = client.get(f"{base}/dataflows/{fid}")
        except PowerBiApiError as e:
            warnings.append(f"definition of {entry['name']}: {e}")
        entry["datasources"] = best_effort(f"datasources of {entry['name']}", f"{base}/dataflows/{fid}/datasources")
        entry["upstream"] = best_effort(f"upstream of {entry['name']}", f"{base}/dataflows/{fid}/upstream")
        if include_transactions:
            entry["transactions"] = best_effort(f"refreshes of {entry['name']}", f"{base}/dataflows/{fid}/transactions")
        flows.append(entry)

    return {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "workspace": ws,
            "objects": objects, "users": users, "dataset_upstream": dataset_upstream, "dataflows": flows,
            "warnings": warnings}
