"""Export DuckDB tables as Parquet (+ a generated Databricks notebook / SQL) for loading into Delta tables.

Each export is a self-contained folder: ``<out>/<export_id>/<table>/part-N.parquet`` plus ``manifest.json``,
``databricks_load.py`` (notebook source) and ``databricks_load.sql``. Column names are made Delta-safe, naive
timestamps become UTC timestamps, TIME/JSON become strings, and file names never start with ``_`` (Spark skips those).
Entity tables (``df_*``) can be exported incrementally: only sheets loaded since the previous export are written, and the
notebook replaces those sheets in the target table (so reloaded sheets never duplicate).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .bulk import qi

EXPORT_DDL = """CREATE TABLE IF NOT EXISTS export_log (
    export_id VARCHAR, table_name VARCHAR, exported_utc TIMESTAMPTZ, mode VARCHAR, rows BIGINT, watermark TIMESTAMPTZ);"""
SNAPSHOT_TABLES = ["files", "sheets", "sheet_headers", "layouts", "load_log", "runs", "dataflow_attributes", "column_mappings",
                   "pipeline_runs", "pipeline_run_steps"]
BAD_CHARS = re.compile(r"[ ,;{}()\n\t=\"'`]+")


def delta_safe_names(names: list[str]) -> list[str]:
    """Replace characters Delta rejects (space , ; { } ( ) newline tab =) and de-duplicate case-insensitively."""
    out, seen = [], set()
    for n in names:
        if BAD_CHARS.search(n):
            core = BAD_CHARS.sub("_", n).strip("_")
            s = ("_" if n.startswith("_") else "") + core + ("_" if n.endswith("_") and core else "")
        else:
            s = n                                   # already safe (keeps _load_id etc. untouched)
        s = s or "col"
        base, i = s, 2
        while s.lower() in seen:
            s, i = f"{base}_{i}", i + 1
        seen.add(s.lower())
        out.append(s)
    return out


def spark_type(duck: str) -> Optional[str]:
    d = duck.upper()
    if d.endswith("[]"):
        inner = spark_type(duck[:-2])
        return f"ARRAY<{inner}>" if inner else None
    m = re.match(r"DECIMAL\((\d+),\s*(\d+)\)", d)
    if m:
        return f"DECIMAL({m.group(1)},{m.group(2)})"
    return {"VARCHAR": "STRING", "BIGINT": "BIGINT", "INTEGER": "INT", "SMALLINT": "SMALLINT", "TINYINT": "TINYINT",
            "DOUBLE": "DOUBLE", "FLOAT": "FLOAT", "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP",
            "TIMESTAMP WITH TIME ZONE": "TIMESTAMP", "TIME": "STRING", "JSON": "STRING", "UUID": "STRING",
            "BLOB": "BINARY"}.get(d)


def _select_expr(col: str, typ: str, alias: str) -> Optional[str]:
    t = typ.upper()
    c = qi(col)
    if t == "TIMESTAMP":
        e = f"CAST({c} AS TIMESTAMPTZ)"          # session TimeZone is UTC: stored as a UTC instant, read as Spark TIMESTAMP
    elif t in ("TIME", "JSON", "UUID"):
        e = f"CAST({c} AS VARCHAR)"
    elif spark_type(typ) is None:
        return None
    else:
        e = c
    return f"{e} AS {qi(alias)}"


def entity_tables(con) -> dict[str, list[str]]:
    """df_ table name -> entity names that load into it."""
    from .mapping_load import table_for
    out: dict[str, list[str]] = {}
    in_pipeline: set[str] = set()
    # a pipeline's first stage may write to a table of its own choosing: that table holds the loaded entities' rows
    for table, ent in con.execute("SELECT output_table, entity FROM pipeline_stages WHERE kind = 'files' UNION "
                                  "SELECT s.output_table, o.entity FROM pipeline_stages s JOIN pipeline_overrides o "
                                  "USING (pipeline_id, position) WHERE s.kind = 'files'").fetchall():
        out.setdefault(table, []).append(ent)
        in_pipeline.add(ent)
    for (ent,) in con.execute("SELECT DISTINCT entity FROM load_log").fetchall():
        if ent not in in_pipeline:
            out.setdefault(table_for(con, ent), []).append(ent)
    return out


def default_tables(con) -> list[str]:
    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
                                      "AND table_type = 'BASE TABLE'").fetchall()}
    piped = {r[0] for r in con.execute("SELECT output_table FROM pipeline_stages").fetchall()}
    data = sorted({t for t in have if t.startswith("df_")} | (piped & have))      # entity tables + every pipeline output
    return data + [t for t in SNAPSHOT_TABLES if t in have]


def export_databricks(con, out_dir: str | Path, tables: Optional[list[str]] = None, incremental: bool = False,
                      include_blobs: bool = False, prefix: str = "", catalog: str = "main", schema: str = "bordereaux",
                      volume_path: str = "/Volumes/<catalog>/<schema>/<volume>/sp_profiler") -> dict:
    """Write the export folder and return its manifest."""
    con.execute(EXPORT_DDL)
    cur = con.cursor()
    cur.execute("SET TimeZone = 'UTC'")
    export_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(out_dir) / export_id
    root.mkdir(parents=True, exist_ok=True)
    wanted = list(tables) if tables else default_tables(con)
    if include_blobs and "file_blobs" not in wanted:
        wanted.append("file_blobs")
    ent_map = entity_tables(con)
    manifest = {"export_id": export_id, "mode": "incremental" if incremental else "full", "prefix": prefix,
                "catalog": catalog, "schema": schema, "tables": [], "skipped": []}
    for table in wanted:
        if not con.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]).fetchone():
            manifest["skipped"].append({"table": table, "reason": "does not exist"})
            continue
        cols = con.execute(f"SELECT column_name, column_type FROM (DESCRIBE {qi(table)})").fetchall()
        aliases = delta_safe_names([c for c, _ in cols])
        sel, ddl_cols, dropped = [], [], []
        for (c, t), a in zip(cols, aliases):
            e = _select_expr(c, t, a)
            if e is None:
                dropped.append(c)
                continue
            sel.append(e)
            st = spark_type("VARCHAR" if t.upper() in ("TIME", "JSON", "UUID") else "TIMESTAMP" if t.upper() == "TIMESTAMP" else t)
            ddl_cols.append((a, st))
        is_entity = table in ent_map
        where, watermark = "", None
        if incremental and is_entity:
            ents = ent_map[table]
            marks = con.execute("SELECT max(watermark) FROM export_log WHERE table_name = ? AND mode IN ('incremental','full')",
                                [table]).fetchone()[0]
            ph = ", ".join("?" for _ in ents)
            params = list(ents) + ([marks] if marks else [])
            cond = f"status = 'ok' AND entity IN ({ph})" + (" AND finished_utc > ?" if marks else "")
            row = con.execute(f"SELECT max(finished_utc) FROM load_log WHERE {cond}", params).fetchone()
            watermark = row[0]
            if watermark is None:
                manifest["skipped"].append({"table": table, "reason": "no new loads since last export"})
                continue
            where = (f" WHERE (_source_sha256, _sheet) IN (SELECT source_sha256, sheet FROM load_log WHERE {cond} "
                     f"AND finished_utc <= ?)")
            where_params = params + [watermark]
        elif is_entity:
            row = con.execute("SELECT max(finished_utc) FROM load_log WHERE status = 'ok' AND entity IN (%s)" %
                              ", ".join("?" for _ in ent_map[table]), ent_map[table]).fetchone()
            watermark, where_params = row[0], []
        else:
            where_params = []
        dest = root / table
        sql = (f"COPY (SELECT {', '.join(sel)} FROM {qi(table)}{where}) TO '{dest.as_posix()}' "
               f"(FORMAT PARQUET, COMPRESSION SNAPPY, FILE_SIZE_BYTES '128MB', FILENAME_PATTERN 'part-{{i}}')")
        cur.execute(sql, where_params) if where_params else cur.execute(sql)
        files = sorted(p.name for p in dest.glob("*.parquet")) if dest.exists() else []
        n = cur.execute("SELECT count(*) FROM read_parquet(?)", [str(dest / "*.parquet")]).fetchone()[0] if files else 0
        con.execute("INSERT INTO export_log VALUES (?, ?, now(), ?, ?, ?)",
                    [export_id, table, manifest["mode"], n, watermark])
        manifest["tables"].append({"name": table, "kind": "entity" if is_entity else "snapshot", "rows": n, "files": files,
                                   "columns": [{"name": a, "spark_type": st} for a, st in ddl_cols], "dropped_columns": dropped,
                                   "renamed": {c: a for (c, _), a in zip(cols, aliases) if c != a}})
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (root / "databricks_load.py").write_text(notebook_source(manifest, volume_path), encoding="utf-8")
    (root / "databricks_load.sql").write_text(sql_source(manifest, volume_path), encoding="utf-8")
    manifest["path"] = str(root)
    return manifest


def _ddl(table: dict, target: str) -> str:
    cols = ",\n  ".join(f"`{c['name']}` {c['spark_type']}" for c in table["columns"])
    return f"CREATE TABLE IF NOT EXISTS {target} (\n  {cols}\n) USING DELTA"


def sql_source(m: dict, volume_path: str) -> str:
    base = f"{volume_path.rstrip('/')}/{m['export_id']}"
    out = [f"-- Generated by sp-profile, export {m['export_id']} ({m['mode']}).",
           f"-- Upload the folder to {base}/ (Unity Catalog volume or cloud storage), then run in a SQL warehouse.",
           "-- Entity (df_*) tables: COPY INTO only appends. If you reloaded sheets since the last export, use databricks_load.py",
           "-- (it replaces those sheets) or delete them first: DELETE FROM <table> WHERE _source_sha256 = '...' AND _sheet = '...'.",
           f"USE CATALOG {m['catalog']};", f"CREATE SCHEMA IF NOT EXISTS {m['schema']};", f"USE SCHEMA {m['schema']};", ""]
    for t in m["tables"]:
        target = f"{m['prefix']}{t['name']}"
        out += [f"-- {t['name']} ({t['rows']} rows)", _ddl(t, target) + ";"]
        if t["kind"] == "snapshot":
            out.append(f"TRUNCATE TABLE {target};")
        out += [f"COPY INTO {target} FROM '{base}/{t['name']}/' FILEFORMAT = PARQUET "
                f"COPY_OPTIONS ('mergeSchema' = 'true'{', ' + chr(39) + 'force' + chr(39) + ' = ' + chr(39) + 'true' + chr(39) if t['kind'] == 'snapshot' else ''});", ""]
    return "\n".join(out)


def notebook_source(m: dict, volume_path: str) -> str:
    tables = [{"name": t["name"], "kind": t["kind"]} for t in m["tables"]]
    return f'''# Databricks notebook source
# Generated by sp-profile, export {m['export_id']} ({m['mode']}). Import this file as a notebook.
# 1. Upload the export folder to a Unity Catalog volume (UI: Catalog > volume > Upload, or `databricks fs cp -r`).
# 2. Fill in the widgets, run all cells. Re-running is safe: reloaded sheets are replaced, snapshot tables overwritten.

# COMMAND ----------
dbutils.widgets.text("source_path", "{volume_path.rstrip('/')}/{m['export_id']}", "Path of this export folder")
dbutils.widgets.text("catalog", "{m['catalog']}", "Catalog")
dbutils.widgets.text("schema", "{m['schema']}", "Schema")
dbutils.widgets.text("prefix", "{m['prefix']}", "Table name prefix")

# COMMAND ----------
from delta.tables import DeltaTable

source, catalog, schema = dbutils.widgets.get("source_path").rstrip("/"), dbutils.widgets.get("catalog"), dbutils.widgets.get("schema")
prefix = dbutils.widgets.get("prefix")
mode = "{m['mode']}"      # fixed by how this export was made: a full export overwrites, an incremental one merges
spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{{catalog}}`.`{{schema}}`")
TABLES = {json.dumps(tables)}

for t in TABLES:
    target = f"`{{catalog}}`.`{{schema}}`.`{{prefix}}{{t['name']}}`"
    df = spark.read.parquet(f"{{source}}/{{t['name']}}")
    if t["kind"] == "snapshot" or mode == "full" or not spark.catalog.tableExists(target):
        df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(target)
    else:
        # incremental entity table: drop the sheets being (re)loaded (same content, or the same file location with
        # older content), then append them
        keys = df.select("_source_sha256", "_source_id", "_sheet").distinct()
        (DeltaTable.forName(spark, target).alias("t")
            .merge(keys.alias("k"), "(t._source_sha256 = k._source_sha256 OR t._source_id = k._source_id) AND t._sheet = k._sheet")
            .whenMatchedDelete().execute())
        df.write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable(target)
    print(t["name"], "->", target, df.count(), "rows")
'''
