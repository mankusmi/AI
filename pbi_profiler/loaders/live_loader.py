"""Live loader: reads a published Power BI semantic model over the REST API.

Two pieces:

* ``PowerBiAuth`` / ``PowerBiRestClient`` -- AAD auth (via msal) and the HTTP
  call to the "Execute Queries" REST endpoint
  (``POST .../groups/{workspace}/datasets/{dataset}/executeQueries``), which
  runs plain DAX. This needs the optional ``requests``/``msal`` dependencies
  (``pip install pbi-profiler[live]``), imported lazily so offline TMDL/BIM
  profiling never needs them.

* ``LiveModelLoader`` -- builds a `Model` purely from DAX ``INFO.VIEW.*``
  dynamic management queries run through *any* `QueryExecutor` (so it can be
  unit-tested with a fake executor, with no network or auth involved). These
  functions were added to the DAX engine specifically to expose model
  metadata without needing a separate XMLA/AMO client; see
  https://learn.microsoft.com/dax/info-view-tables-function-dax and
  neighboring INFO.VIEW.* function docs.

  Because different compatibility levels / engine versions support different
  subsets of INFO functions, every metadata query is best-effort: a query
  that fails (function not supported on this model) is skipped with a
  warning rather than aborting the whole load.
"""
from __future__ import annotations

import os
import warnings
from typing import Any, Optional

from .base import ModelLoader, QueryExecutor
from ..model import Model, Table, Column, Measure, Relationship, Hierarchy, Role, TablePermission

POWER_BI_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
POWER_BI_API_BASE = "https://api.powerbi.com/v1.0/myorg"


class PowerBiAuth:
    """Acquires an AAD access token for the Power BI REST API via msal.

    Uses a confidential-client (service principal) flow when a client secret
    is supplied, otherwise falls back to an interactive device-code flow.
    Credentials can be passed explicitly or picked up from the environment
    (``PBI_TENANT_ID``, ``PBI_CLIENT_ID``, ``PBI_CLIENT_SECRET``).
    """

    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scope: Optional[list[str]] = None,
    ):
        self.tenant_id = tenant_id or os.environ.get("PBI_TENANT_ID")
        self.client_id = client_id or os.environ.get("PBI_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("PBI_CLIENT_SECRET")
        self.scope = scope or POWER_BI_SCOPE
        if not self.tenant_id or not self.client_id:
            raise ValueError(
                "PowerBiAuth requires tenant_id and client_id "
                "(pass explicitly or set PBI_TENANT_ID / PBI_CLIENT_ID)."
            )
        self._app = None

    def _get_app(self):
        if self._app is not None:
            return self._app
        import msal  # lazy import: only needed for live mode

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        if self.client_secret:
            self._app = msal.ConfidentialClientApplication(
                self.client_id, authority=authority, client_credential=self.client_secret
            )
        else:
            self._app = msal.PublicClientApplication(self.client_id, authority=authority)
        return self._app

    def get_token(self) -> str:
        import msal

        app = self._get_app()
        if isinstance(app, msal.ConfidentialClientApplication):
            result = app.acquire_token_for_client(scopes=self.scope)
        else:
            result = None
            accounts = app.get_accounts()
            if accounts:
                result = app.acquire_token_silent(self.scope, account=accounts[0])
            if not result:
                flow = app.initiate_device_flow(scopes=self.scope)
                if "user_code" not in flow:
                    raise RuntimeError(f"Failed to start device code flow: {flow}")
                print(flow["message"])
                result = app.acquire_token_by_device_flow(flow)
        if not result or "access_token" not in result:
            desc = (result or {}).get("error_description", "unknown error")
            raise RuntimeError(f"Authentication failed: {desc}")
        return result["access_token"]


class PowerBiRestClient(QueryExecutor):
    """Runs DAX queries against a published dataset via the Power BI REST API."""

    def __init__(
        self,
        workspace_id: str,
        dataset_id: str,
        auth: PowerBiAuth,
        session: Any = None,
        api_base: str = POWER_BI_API_BASE,
    ):
        self.workspace_id = workspace_id
        self.dataset_id = dataset_id
        self.auth = auth
        self.api_base = api_base
        self._session = session

    @property
    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def run_dax(self, query: str) -> list[dict[str, Any]]:
        url = f"{self.api_base}/groups/{self.workspace_id}/datasets/{self.dataset_id}/executeQueries"
        token = self.auth.get_token()
        response = self.session.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}},
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", [])
        if not results:
            return []
        tables = results[0].get("tables", [])
        if not tables:
            return []
        return tables[0].get("rows", [])


def _pick(row: dict[str, Any], *keys: str) -> Any:
    """Fetch the first present key, tolerating the `[Bracketed]` DAX column
    naming and minor spelling variance across engine versions."""
    for key in keys:
        for candidate in (key, f"[{key}]"):
            if candidate in row:
                return row[candidate]
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1")


class LiveModelLoader(ModelLoader):
    """Builds a `Model` from DAX INFO.VIEW.* metadata queries run through any
    `QueryExecutor` (typically a `PowerBiRestClient`, or a fake in tests)."""

    def __init__(self, executor: QueryExecutor, model_name: str = "Model"):
        self.executor = executor
        self.model_name = model_name

    def _query(self, dax: str) -> list[dict[str, Any]]:
        try:
            return self.executor.run_dax(dax)
        except Exception as exc:  # engine/model may not support this INFO function
            warnings.warn(f"Live metadata query failed, skipping: {dax!r} ({exc})")
            return []

    def load(self) -> Model:
        table_rows = self._query("EVALUATE INFO.VIEW.TABLES()")
        column_rows = self._query("EVALUATE INFO.VIEW.COLUMNS()")
        measure_rows = self._query("EVALUATE INFO.VIEW.MEASURES()")
        relationship_rows = self._query("EVALUATE INFO.VIEW.RELATIONSHIPS()")
        hierarchy_rows = self._query("EVALUATE INFO.VIEW.HIERARCHIES()")
        role_rows = self._query("EVALUATE INFO.ROLES()")
        permission_rows = self._query("EVALUATE INFO.VIEW.TABLEPERMISSIONS()")

        tables_by_name: dict[str, Table] = {}
        for row in table_rows:
            name = _pick(row, "Name", "Table")
            if not name:
                continue
            tables_by_name[name] = Table(
                name=name,
                is_hidden=_as_bool(_pick(row, "IsHidden")),
                description=_pick(row, "Description"),
            )

        def _table_for(name: str) -> Table:
            if name not in tables_by_name:
                tables_by_name[name] = Table(name=name)
            return tables_by_name[name]

        for row in column_rows:
            table_name = _pick(row, "Table")
            col_name = _pick(row, "Column", "Name", "ExplicitName")
            if not table_name or not col_name:
                continue
            col_type = (_pick(row, "Type") or "").lower()
            _table_for(table_name).columns.append(
                Column(
                    name=col_name,
                    table=table_name,
                    data_type=_pick(row, "DataType"),
                    is_hidden=_as_bool(_pick(row, "IsHidden")),
                    is_calculated="calculated" in col_type,
                    is_key=_as_bool(_pick(row, "IsKey")),
                    source_column=_pick(row, "SourceColumn"),
                    expression=_pick(row, "Expression"),
                    format_string=_pick(row, "FormatString"),
                    summarize_by=_pick(row, "SummarizeBy"),
                    display_folder=_pick(row, "DisplayFolder"),
                    description=_pick(row, "Description"),
                    data_category=_pick(row, "DataCategory"),
                    sort_by_column=_pick(row, "SortByColumn"),
                )
            )

        for row in measure_rows:
            table_name = _pick(row, "Table")
            name = _pick(row, "Name", "Measure")
            if not table_name or not name:
                continue
            _table_for(table_name).measures.append(
                Measure(
                    name=name,
                    table=table_name,
                    expression=_pick(row, "Expression"),
                    format_string=_pick(row, "FormatString"),
                    is_hidden=_as_bool(_pick(row, "IsHidden")),
                    display_folder=_pick(row, "DisplayFolder"),
                    description=_pick(row, "Description"),
                )
            )

        hierarchies_by_table_name: dict[tuple[str, str], Hierarchy] = {}
        for row in hierarchy_rows:
            table_name = _pick(row, "Table")
            hier_name = _pick(row, "Hierarchy")
            level_col = _pick(row, "Column", "Level")
            if not table_name or not hier_name:
                continue
            key = (table_name, hier_name)
            if key not in hierarchies_by_table_name:
                hier = Hierarchy(name=hier_name, table=table_name, is_hidden=_as_bool(_pick(row, "IsHidden")))
                hierarchies_by_table_name[key] = hier
                _table_for(table_name).hierarchies.append(hier)
            if level_col:
                hierarchies_by_table_name[key].levels.append(level_col)

        relationships: list[Relationship] = []
        for row in relationship_rows:
            from_table = _pick(row, "FromTable")
            to_table = _pick(row, "ToTable")
            if not from_table or not to_table:
                continue
            cross = _pick(row, "CrossFilteringBehavior") or "single"
            relationships.append(
                Relationship(
                    from_table=from_table,
                    from_column=_pick(row, "FromColumn") or "",
                    to_table=to_table,
                    to_column=_pick(row, "ToColumn") or "",
                    is_active=_as_bool(_pick(row, "IsActive"), default=True),
                    cross_filtering_behavior="bothDirections" if "both" in str(cross).lower() else "single",
                    from_cardinality=_pick(row, "FromCardinality"),
                    to_cardinality=_pick(row, "ToCardinality"),
                )
            )

        roles_by_name: dict[str, Role] = {}
        for row in role_rows:
            name = _pick(row, "Name")
            if not name:
                continue
            roles_by_name[name] = Role(name=name, model_permission=_pick(row, "ModelPermission"))
        for row in permission_rows:
            role_name = _pick(row, "Role")
            table_name = _pick(row, "Table")
            if not role_name or not table_name:
                continue
            role = roles_by_name.setdefault(role_name, Role(name=role_name))
            role.table_permissions.append(
                TablePermission(table=table_name, filter_expression=_pick(row, "FilterExpression"))
            )

        return Model(
            name=self.model_name,
            source_kind="live",
            tables=list(tables_by_name.values()),
            relationships=relationships,
            roles=list(roles_by_name.values()),
        )
