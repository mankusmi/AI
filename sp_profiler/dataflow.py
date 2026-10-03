"""Power BI Gen1 dataflow (``model.json`` / CDM folder) parsing and column-mapping suggestions."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

from .inspect_excel import normalise_header

DATAFLOW_DDL = """
CREATE TABLE IF NOT EXISTS dataflows (
    dataflow_id VARCHAR PRIMARY KEY, name VARCHAR, description VARCHAR, culture VARCHAR,
    modified_time VARCHAR, imported_utc TIMESTAMPTZ DEFAULT now(), source_file VARCHAR, raw_json JSON);
CREATE TABLE IF NOT EXISTS dataflow_entities (
    dataflow_id VARCHAR, entity VARCHAR, description VARCHAR, m_query VARCHAR, partitions INTEGER);
CREATE TABLE IF NOT EXISTS dataflow_attributes (
    dataflow_id VARCHAR, entity VARCHAR, position INTEGER, name VARCHAR, data_type VARCHAR, description VARCHAR);
CREATE TABLE IF NOT EXISTS dataflow_queries (
    dataflow_id VARCHAR, name VARCHAR, is_entity BOOLEAN, m_query VARCHAR);
-- kind 'attribute': target is a dataflow attribute; 'input': target is a column the Power Query steps read
CREATE TABLE IF NOT EXISTS column_mappings (
    entity VARCHAR, layout_hash VARCHAR, norm_header VARCHAR, attribute VARCHAR, kind VARCHAR DEFAULT 'attribute',
    updated_utc TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (entity, layout_hash, norm_header, kind));
CREATE TABLE IF NOT EXISTS entity_transforms (
    entity VARCHAR PRIMARY KEY, dataflow_id VARCHAR, use_m BOOLEAN, override_sql VARCHAR,
    accept_partial BOOLEAN DEFAULT FALSE, extra_inputs VARCHAR[], updated_utc TIMESTAMPTZ DEFAULT now(),
    date_order VARCHAR DEFAULT 'auto');
CREATE TABLE IF NOT EXISTS query_bindings (
    dataflow_id VARCHAR, query_name VARCHAR, table_name VARCHAR, PRIMARY KEY (dataflow_id, query_name));
"""


def migrate(con) -> None:
    """Upgrade databases created before ``column_mappings.kind`` existed."""
    et = [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                    "WHERE table_name = 'entity_transforms'").fetchall()]
    if et and "date_order" not in et:
        con.execute("ALTER TABLE entity_transforms ADD COLUMN date_order VARCHAR DEFAULT 'auto'")
    cols = [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'column_mappings'").fetchall()]
    if cols and "kind" not in cols:
        con.execute("ALTER TABLE column_mappings RENAME TO column_mappings_old")
        con.execute(DATAFLOW_DDL)
        con.execute("INSERT INTO column_mappings (entity, layout_hash, norm_header, attribute, kind, updated_utc) "
                    "SELECT entity, layout_hash, norm_header, attribute, 'attribute', updated_utc FROM column_mappings_old")
        con.execute("DROP TABLE column_mappings_old")


def parse_model_json(raw: bytes | str) -> dict:
    """Parse a Gen1 dataflow ``model.json``. Raises ValueError if it is not one."""
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw.lstrip("﻿")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Not valid JSON: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("entities"), list):
        raise ValueError("Not a dataflow model.json (no 'entities' array)")
    queries = extract_m_queries((doc.get("pbi:mashup") or {}).get("document", ""))
    entity_names = {e.get("name") for e in doc["entities"] if isinstance(e, dict)}
    entities = []
    for e in doc["entities"]:
        if not isinstance(e, dict) or "name" not in e:
            continue
        attrs = [{"position": i + 1, "name": a["name"], "data_type": a.get("dataType", "string"),
                  "description": a.get("description", "")}
                 for i, a in enumerate(e.get("attributes", [])) if isinstance(a, dict) and "name" in a]
        entities.append({"name": e["name"], "description": e.get("description", ""),
                         "m_query": queries.get(e["name"], ""), "partitions": len(e.get("partitions", [])),
                         "attributes": attrs})
    if not entities:
        raise ValueError("Dataflow contains no entities")
    return {"name": doc.get("name", ""), "description": doc.get("description", ""),
            "culture": doc.get("culture", ""), "modified_time": doc.get("modifiedTime", ""),
            "entities": entities, "queries": queries, "entity_names": entity_names,
            "sha": hashlib.sha256(text.encode()).hexdigest()}


def extract_m_queries(document: str) -> dict[str, str]:
    """Split the Power Query section document into {query name: M source}."""
    out: dict[str, str] = {}
    for chunk in re.split(r"(?m)^shared\s+", document or "")[1:]:
        m = re.match(r'(#"(?:[^"]|"")+"|[A-Za-z_][\w.]*)\s*=\s*(.*)', chunk, re.DOTALL)
        if not m:
            continue
        name = m.group(1)
        if name.startswith('#"'):
            name = name[2:-1].replace('""', '"')
        out[name] = m.group(2).strip().rstrip(";").strip()
    return out


def store_dataflow(con, parsed: dict, source_file: str = "") -> tuple[str, bool]:
    """Insert a dataflow definition. Returns (dataflow_id, newly_imported)."""
    dataflow_id = parsed["sha"][:12]
    if con.execute("SELECT 1 FROM dataflows WHERE dataflow_id = ?", [dataflow_id]).fetchone():
        return dataflow_id, False
    con.execute("BEGIN")
    try:
        con.execute("INSERT INTO dataflows (dataflow_id, name, description, culture, modified_time, source_file, raw_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [dataflow_id, parsed["name"], parsed["description"], parsed["culture"],
                     parsed["modified_time"], source_file, json.dumps({"entities": [e["name"] for e in parsed["entities"]]})])
        con.executemany("INSERT INTO dataflow_queries VALUES (?, ?, ?, ?)",
                        [[dataflow_id, n, n in parsed["entity_names"], m] for n, m in parsed["queries"].items()])
        for e in parsed["entities"]:
            con.execute("INSERT INTO dataflow_entities VALUES (?, ?, ?, ?, ?)",
                        [dataflow_id, e["name"], e["description"], e["m_query"], e["partitions"]])
            con.executemany("INSERT INTO dataflow_attributes VALUES (?, ?, ?, ?, ?, ?)",
                            [[dataflow_id, e["name"], a["position"], a["name"], a["data_type"], a["description"]]
                             for a in e["attributes"]])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return dataflow_id, True


def _compact(s: str) -> str:
    return normalise_header(s).replace(" ", "")


SYNONYMS = {"no": "number", "num": "number", "nbr": "number", "nr": "number", "amt": "amount", "ccy": "currency",
            "cur": "currency", "dt": "date", "prem": "premium", "ref": "reference", "pol": "policy", "pct": "percent",
            "cmsn": "commission", "comm": "commission", "qty": "quantity", "desc": "description"}


def _tokens(s: str) -> frozenset:
    words = normalise_header(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)).split()
    return frozenset(SYNONYMS.get(w, w) for w in words)


def _canon(s: str) -> str:
    """Words with abbreviations expanded, joined: 'Policy No' and 'PolicyNumber' both give 'policynumber'."""
    words = normalise_header(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)).split()
    return "".join(SYNONYMS.get(w, w) for w in words)


def _score(header: str, attribute: str) -> float:
    hc, ac = header.replace(" ", ""), _compact(attribute)
    if not hc or not ac:
        return 0.0
    if hc == ac:
        return 1.0
    s = SequenceMatcher(None, hc, ac).ratio()
    if _canon(header) == _canon(attribute):            # same words once abbreviations are expanded
        s = max(s, 0.97)
    if _tokens(header) == _tokens(attribute):          # same words, different order: "Premium (Gross)"
        s = max(s, 0.95)
    if min(len(hc), len(ac)) >= 4 and (hc.startswith(ac) or ac.startswith(hc)):   # "Policy No" ~ "PolicyNumber"
        s = max(s, 0.8)
    return s


def suggest_mapping(norm_headers: list[str], attributes: list[str], threshold: float = 0.75) -> dict[str, dict]:
    """Greedy one-to-one match of source headers to target attributes by name similarity.

    Returns {norm_header: {"attribute": name, "score": 0..1}} for pairs at/above the threshold.
    """
    scored = []
    for h in norm_headers:
        for a in attributes:
            s = _score(h, a)
            if s >= threshold:
                scored.append((s, h, a))
    out, used_h, used_a = {}, set(), set()
    for s, h, a in sorted(scored, key=lambda x: (-x[0], x[1], x[2])):
        if h in used_h or a in used_a:
            continue
        out[h] = {"attribute": a, "score": round(s, 3)}
        used_h.add(h)
        used_a.add(a)
    return out
