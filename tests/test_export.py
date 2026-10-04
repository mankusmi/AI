import ast
import json
import re
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from sp_profiler import mapping_load as ml
from sp_profiler.cli import main
from sp_profiler.export import delta_safe_names, export_databricks, spark_type

from .test_m_transform import env, map_inputs  # noqa: F401


def test_delta_safe_names():
    assert delta_safe_names(["Gross Premium (GBP)", "a,b;c{d}e=f", "_load_id", "Name", "name", "x y", "x_y"]) == [
        "Gross_Premium_GBP", "a_b_c_d_e_f", "_load_id", "Name", "name_2", "x_y", "x_y_2"]


def test_spark_types():
    assert spark_type("VARCHAR") == "STRING" and spark_type("DECIMAL(38,10)") == "DECIMAL(38,10)"
    assert spark_type("TIMESTAMP WITH TIME ZONE") == "TIMESTAMP" and spark_type("VARCHAR[]") == "ARRAY<STRING>"
    assert spark_type("STRUCT(a INTEGER)") is None


def loaded(env):  # noqa: F811
    con, did = env
    map_inputs(con, did)
    ml.load_entity(con, did, "Policies")
    return con, did


def test_full_export_files_manifest_and_types(env, tmp_path):  # noqa: F811
    con, did = loaded(env)
    m = export_databricks(con, tmp_path / "out", prefix="bdx_", catalog="main", schema="bdx",
                          volume_path="/Volumes/main/bdx/landing/sp")
    root = Path(m["path"])
    names = {t["name"] for t in m["tables"]}
    assert {"df_policies", "files", "layouts", "load_log", "sheet_headers"} <= names and "file_blobs" not in names
    pol = next(t for t in m["tables"] if t["name"] == "df_policies")
    assert pol["kind"] == "entity" and pol["rows"] == 3 and pol["files"] and all(not f.startswith(("_", ".")) for f in pol["files"])
    assert (root / "manifest.json").exists() and json.loads((root / "manifest.json").read_text())["mode"] == "full"
    got = duckdb.connect().execute(
        f"select PolicyNumber, GrossGBP, _source_sha256 from read_parquet('{(root / 'df_policies').as_posix()}/*.parquet') order by 1").fetchall()
    assert [g[0] for g in got] == ["P1", "P2", "P3"] and got[1][1] == Decimal("850")


def test_parquet_schema_is_spark_friendly(env, tmp_path):  # noqa: F811
    pq = pytest.importorskip("pyarrow.parquet")
    pa = pytest.importorskip("pyarrow")
    con, did = loaded(env)
    m = export_databricks(con, tmp_path / "out")
    sch = pq.read_schema(next((Path(m["path"]) / "df_policies").glob("*.parquet")))
    assert pa.types.is_decimal(sch.field("GrossGBP").type) and pa.types.is_timestamp(sch.field("_loaded_utc").type)
    assert sch.field("_loaded_utc").type.tz is not None                      # UTC instant -> Spark TIMESTAMP
    fsch = pq.read_schema(next((Path(m["path"]) / "files").glob("*.parquet")))
    assert pa.types.is_list(fsch.field("path_segments").type)
    assert all(not re.search(r"[ ,;{}()\n\t=]", n) for n in sch.names)


def test_awkward_column_names_are_renamed(env, tmp_path):  # noqa: F811
    con, _ = loaded(env)
    con.execute('ALTER TABLE df_policies ADD COLUMN "Net Premium (GBP)" DOUBLE')
    m = export_databricks(con, tmp_path / "out", tables=["df_policies"])
    t = m["tables"][0]
    assert t["renamed"] == {"Net Premium (GBP)": "Net_Premium_GBP"} and any(c["name"] == "Net_Premium_GBP" for c in t["columns"])


def test_incremental_exports_only_new_sheets(env, tmp_path):  # noqa: F811
    con, did = env
    map_inputs(con, did)
    # load only layout A first
    first_layout = con.execute("select layout_hash from layouts order by layout_hash limit 1").fetchone()[0]
    ml.load_entity(con, did, "Policies", layout_hashes=[first_layout])
    m1 = export_databricks(con, tmp_path / "out", tables=["df_policies"], incremental=True)
    n1 = m1["tables"][0]["rows"]
    assert 0 < n1 < 3
    ml.load_entity(con, did, "Policies")                                      # the other layout
    import time
    time.sleep(1.1)                                                           # export ids are per second
    m2 = export_databricks(con, tmp_path / "out", tables=["df_policies"], incremental=True)
    assert m2["tables"][0]["rows"] == 3 - n1
    time.sleep(1.1)
    m3 = export_databricks(con, tmp_path / "out", tables=["df_policies"], incremental=True)
    assert m3["tables"] == [] and "no new loads" in m3["skipped"][0]["reason"]
    time.sleep(1.1)
    ml.load_entity(con, did, "Policies", force=True)                          # replaces both sheets
    m4 = export_databricks(con, tmp_path / "out", tables=["df_policies"], incremental=True)
    assert m4["tables"][0]["rows"] == 3


def test_blobs_opt_in_and_missing_table(env, tmp_path):  # noqa: F811
    con, _ = loaded(env)
    m = export_databricks(con, tmp_path / "o", tables=["files", "nope"], include_blobs=True)
    assert {t["name"] for t in m["tables"]} == {"files", "file_blobs"}
    assert m["skipped"] == [{"table": "nope", "reason": "does not exist"}]
    blob = next(t for t in m["tables"] if t["name"] == "file_blobs")
    assert any(c["spark_type"] == "BINARY" for c in blob["columns"])


def test_generated_notebook_and_sql(env, tmp_path):  # noqa: F811
    con, _ = loaded(env)
    m = export_databricks(con, tmp_path / "o", tables=["df_policies", "layouts"], catalog="main", schema="bdx", prefix="bdx_",
                          volume_path="/Volumes/main/bdx/landing/sp")
    root = Path(m["path"])
    src = (root / "databricks_load.py").read_text()
    ast.parse(src)                                                            # valid Python
    assert "# Databricks notebook source" in src and "whenMatchedDelete" in src and m["export_id"] in src
    assert '"kind": "entity"' in src and '"kind": "snapshot"' in src
    sql = (root / "databricks_load.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS bdx_df_policies" in sql and "`PolicyNumber` STRING" in sql
    assert "COPY INTO bdx_df_policies" in sql and "TRUNCATE TABLE bdx_layouts" in sql
    assert f"/Volumes/main/bdx/landing/sp/{m['export_id']}/df_policies/" in sql


def test_cli_export(env, tmp_path, capsys):  # noqa: F811
    con, did = loaded(env)
    path = con.execute("PRAGMA database_list").fetchall()[0][2]
    con.close()
    assert main(["export-databricks", "--db", path, "--out", str(tmp_path / "x"), "--tables", "df_policies", "layouts"]) == 0
    out = capsys.readouterr().out
    assert "df_policies" in out and "databricks_load.py" in out
