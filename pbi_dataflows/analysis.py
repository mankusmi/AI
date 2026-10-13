"""Turn a fetched snapshot into run order, dependencies and plain-English summaries (no network)."""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Optional

GUID = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
_DF_ID_RES = [re.compile(rf"/dataflows/({GUID})"), re.compile(rf'dataflowId\s*=\s*"({GUID})"')]
_SHARED_RE = re.compile(r'^shared\s+(#"(?:[^"]|"")+"|[A-Za-z_][\w.]*)\s*=', re.M)
_FUNC_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*\.[A-Z][A-Za-z0-9.]*)\s*\(")
_STR_ARG_RE = re.compile(r'"((?:[^"]|"")*)"')

# M source function -> friendly label
SOURCES = {
    "Sql.Database": "SQL Server", "Sql.Databases": "SQL Server", "AzureSQL.Database": "Azure SQL",
    "Oracle.Database": "Oracle", "PostgreSQL.Database": "PostgreSQL", "MySQL.Database": "MySQL",
    "Snowflake.Databases": "Snowflake", "GoogleBigQuery.Database": "BigQuery", "Odbc.DataSource": "ODBC",
    "Odbc.Query": "ODBC", "OleDb.DataSource": "OLE DB", "SharePoint.Files": "SharePoint files",
    "SharePoint.Tables": "SharePoint list", "SharePoint.Contents": "SharePoint", "Web.Contents": "Web",
    "Web.Page": "Web page", "OData.Feed": "OData", "Excel.Workbook": "Excel", "Csv.Document": "CSV",
    "Json.Document": "JSON", "Folder.Files": "Folder", "AzureStorage.DataLake": "Azure Data Lake",
    "AzureStorage.Blobs": "Azure Blob", "AzureStorage.Tables": "Azure Table", "Salesforce.Data": "Salesforce",
    "Salesforce.Reports": "Salesforce", "CommonDataService.Database": "Dataverse",
    "Dataverse.Contents": "Dataverse", "PowerPlatform.Dataflows": "Power BI dataflow",
    "PowerBI.Dataflows": "Power BI dataflow", "Databricks.Catalogs": "Databricks",
    "Lakehouse.Contents": "Fabric Lakehouse", "Fabric.Warehouse": "Fabric Warehouse",
    "AnalysisServices.Database": "Analysis Services", "Teradata.Database": "Teradata",
}
# Table function -> what it does (grouped for the summary sentence)
VERBS = {
    "Table.SelectRows": "filters rows", "Table.SelectColumns": "selects columns", "Table.RemoveColumns": "drops columns",
    "Table.RenameColumns": "renames columns", "Table.ReorderColumns": "reorders columns",
    "Table.TransformColumnTypes": "sets data types", "Table.TransformColumns": "transforms columns",
    "Table.AddColumn": "adds calculated columns", "Table.AddIndexColumn": "adds index columns",
    "Table.ReplaceValue": "replaces values", "Table.FillDown": "fills values", "Table.FillUp": "fills values",
    "Table.Distinct": "removes duplicates", "Table.RemoveRowsWithErrors": "removes error rows",
    "Table.NestedJoin": "merges (joins) tables", "Table.Join": "merges (joins) tables",
    "Table.FuzzyNestedJoin": "merges (joins) tables", "Table.ExpandTableColumn": "expands merged/nested tables",
    "Table.Combine": "appends tables", "Table.Group": "aggregates (group by)", "Table.Sort": "sorts",
    "Table.Pivot": "pivots", "Table.UnpivotOtherColumns": "unpivots", "Table.Unpivot": "unpivots",
    "Table.Transpose": "transposes", "Table.PromoteHeaders": "promotes headers", "Table.FirstN": "keeps top rows",
    "Table.Skip": "skips rows", "Table.SplitColumn": "splits columns", "Table.Buffer": "buffers",
}


def unquote_name(n: str) -> str:
    return n[2:-1].replace('""', '"') if n.startswith('#"') else n


def parse_queries(definition: dict) -> list[dict]:
    """Split the mashup document into queries with their detected sources and transformation verbs."""
    doc = ((definition or {}).get("pbi:mashup") or {}).get("document") or ""
    matches = list(_SHARED_RE.finditer(doc))
    out = []
    for i, m in enumerate(matches):
        body = doc[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(doc)]
        sources, steps = [], Counter()
        for fm in _FUNC_RE.finditer(body):
            fn = fm.group(1)
            if fn in SOURCES:
                args = [a.replace('""', '"') for a in _STR_ARG_RE.findall(body[fm.end(): fm.end() + 300].split(")")[0])][:2]
                sources.append({"function": fn, "kind": SOURCES[fn], "detail": " / ".join(args)})
            elif fn in VERBS:
                steps[VERBS[fn]] += 1
        out.append({"name": unquote_name(m.group(1)), "sources": sources, "steps": dict(steps),
                    "m_lines": body.count("\n") + 1})
    return out


def _entity_info(e: dict) -> dict:
    kind = e.get("$type", "LocalEntity").replace("Entity", "").lower() or "local"
    return {"name": e.get("name"), "kind": "linked" if kind == "reference" else kind,
            "description": e.get("description") or "", "columns": len(e.get("attributes") or []),
            "column_names": [a.get("name") for a in (e.get("attributes") or [])],
            "linked_source": e.get("source") if kind == "reference" else None,
            "last_refresh": max((p.get("refreshTime") or "" for p in e.get("partitions") or []), default="") or None}


def _latest_success(transactions: list) -> Optional[dict]:
    ok = [t for t in transactions if str(t.get("status", "")).lower() == "success" and t.get("startTime")]
    return max(ok, key=lambda t: t["startTime"]) if ok else None


def analyze(snapshot: dict) -> dict:
    """Return {'flows': [...in run order...], 'stages': [[ids]], 'warnings': [...]}; each flow gains analysis fields."""
    flows = {f["id"]: f for f in snapshot["dataflows"]}
    warnings = list(snapshot.get("warnings", []))
    info: dict[str, dict] = {}
    for fid, f in flows.items():
        d = f.get("definition") or {}
        ids = {x["targetDataflowId"] for x in f.get("upstream", []) if x.get("targetDataflowId")}
        text = json.dumps(d) + "\n" + (((d.get("pbi:mashup") or {}).get("document")) or "")  # raw M: json.dumps escapes quotes
        for rx in _DF_ID_RES:
            ids.update(rx.findall(text))
        ids.discard(fid)
        queries = parse_queries(d)
        api_sources = [{"function": None, "kind": s.get("datasourceType", "?"),
                        "detail": " / ".join(str(v) for v in (s.get("connectionDetails") or {}).values() if v)}
                       for s in f.get("datasources", [])]
        info[fid] = {"entities": [_entity_info(e) for e in d.get("entities", [])], "queries": queries,
                     "depends_on": sorted(i for i in ids if i in flows),
                     "external_deps": sorted(i for i in ids if i not in flows), "api_sources": api_sources}

    # Stage = longest dependency chain beneath a flow; flows in one stage don't depend on each other.
    stage: dict[str, int] = {}
    remaining = set(flows)
    while remaining:
        ready = [f for f in remaining if all(d in stage for d in info[f]["depends_on"])]
        if not ready:
            warnings.append("Dependency cycle among: " + ", ".join(sorted(flows[f]["name"] for f in remaining)))
            top = max(stage.values(), default=-1) + 1
            for f in remaining:
                stage[f] = top
            break
        for f in ready:
            stage[f] = 1 + max((stage[d] for d in info[f]["depends_on"]), default=-1)
            remaining.discard(f)

    def latest_start(fid):
        t = _latest_success(flows[fid].get("transactions", []))
        return t["startTime"] if t else "9999"

    order = sorted(flows, key=lambda f: (stage[f], latest_start(f), flows[f]["name"].lower()))
    downstream = {fid: [] for fid in flows}
    for fid in flows:
        for d in info[fid]["depends_on"]:
            downstream[d].append(fid)
    ds_names = {d.get("id"): d.get("name") for d in snapshot.get("objects", {}).get("datasets", [])}
    ds_by_flow: dict[str, list] = {}
    for link in snapshot.get("dataset_upstream", []):
        ds_by_flow.setdefault(link.get("dataflowObjectId"), []).append(ds_names.get(link.get("datasetObjectId"), link.get("datasetObjectId")))

    result = []
    for n, fid in enumerate(order, 1):
        f, i = flows[fid], info[fid]
        last = _latest_success(f.get("transactions", []))
        tx = f.get("transactions", [])
        rec = {"id": fid, "name": f["name"], "order": n, "stage": stage[fid] + 1, **i,
               "downstream": sorted(downstream[fid], key=lambda x: flows[x]["name"].lower()),
               "datasets": sorted(x for x in ds_by_flow.get(fid, []) if x),
               "last_success": last["startTime"] if last else None,
               "last_status": (max(tx, key=lambda t: t.get("startTime") or "").get("status") if tx else None),
               "meta": f.get("meta", {}), "has_definition": f.get("definition") is not None}
        rec["summary"] = summarize(rec, {k: flows[k]["name"] for k in flows})
        result.append(rec)
    stages: list[list[str]] = []
    for r in result:
        while len(stages) < r["stage"]:
            stages.append([])
        stages[r["stage"] - 1].append(r["id"])
    return {"flows": result, "stages": stages, "warnings": warnings}


def _join(items: list[str], limit: int = 6) -> str:
    items = list(dict.fromkeys(items))
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def summarize(rec: dict, names: dict[str, str]) -> str:
    parts = []
    desc = (rec["meta"].get("description") or "").strip()
    if desc:
        parts.append(desc.rstrip(".") + ".")
    srcs = [f"{s['kind']}" + (f" ({s['detail']})" if s["detail"] else "") for q in rec["queries"] for s in q["sources"]]
    srcs = srcs or [f"{s['kind']}" + (f" ({s['detail']})" if s["detail"] else "") for s in rec["api_sources"]]
    if rec["depends_on"]:
        srcs = [f"dataflow {names[d]}" for d in rec["depends_on"]] + [s for s in srcs if "Power BI dataflow" not in s]
    parts.append(f"Reads from {_join(srcs)}." if srcs else "No data source could be detected.")
    ents = rec["entities"]
    if ents:
        local = [e["name"] for e in ents if e["kind"] != "linked"]
        linked = [e["name"] for e in ents if e["kind"] == "linked"]
        bits = []
        if local:
            bits.append(f"{len(local)} table(s) ({_join(local)})")
        if linked:
            bits.append(f"{len(linked)} linked table(s) ({_join(linked)})")
        parts.append("Outputs " + " and ".join(bits) + ".")
    verbs = Counter()
    for q in rec["queries"]:
        verbs.update(q["steps"])
    if verbs:
        parts.append("Transformations: " + _join([v for v, _ in verbs.most_common()], 8) + ".")
    down = [names[d] for d in rec["downstream"]] + rec["datasets"]
    if down:
        parts.append(f"Feeds {_join(down)}.")
    return " ".join(parts)
