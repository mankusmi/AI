"""Advanced profiling: table summary, per-column deep dive, every pipeline stage, snapshots, CLI and endpoints."""
import duckdb
import pytest

from sp_profiler import pipeline as pl
from sp_profiler import profiling as pf
from sp_profiler.cli import main

from .test_pipeline_per_coverholder import make_pipeline, map_layouts, world  # noqa: F401  (fixture)


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""CREATE TABLE t AS SELECT i AS id, CASE WHEN i % 7 = 0 THEN NULL ELSE (i * 37 % 1000)::DOUBLE END amt,
        CASE WHEN i % 5 = 0 THEN ' N/A' WHEN i % 3 = 0 THEN 'ab-' || i ELSE 'AB-' || (i % 40) END code,
        DATE '2024-01-01' + to_days(i % 400) d, 'x' const, NULL::VARCHAR empty,
        CASE WHEN i % 2 = 0 THEN 'acme' ELSE 'beta' END _coverholder FROM range(1, 2001) r(i)""")
    c.execute("INSERT INTO t SELECT 1, 1e9, 'AB-1', DATE '2024-01-01', 'x', NULL, 'acme'")      # a duplicate id and an outlier
    return c


def test_table_summary_flags_the_table_as_a_whole(con):
    s = pf.table_summary(con, pf.source_sql("t"))
    assert s["rows"] == 2001 and s["columns"] == 7
    assert s["flags"]["all_null"] == ["empty"] and s["flags"]["constant"] == ["const"]
    assert "id" not in s["flags"]["key_candidates"]                      # id 1 now appears twice
    assert {r["coverholder"]: r["rows"] for r in s["by_coverholder"]} == {"acme": 1001, "beta": 1000}
    assert s["duplicate_rows"] == 0 and 0 < s["completeness_pct"] < 100
    one = pf.table_summary(con, pf.source_sql("t", coverholder="beta"))
    assert one["rows"] == 1000


def test_numeric_column_distribution_and_outliers(con):
    p = pf.column_profile(con, pf.source_sql("t"), "amt")
    assert p["kind"] == "number" and p["stats"]["max"] == 1e9 and p["stats"]["p95"] < 1e9
    assert p["outliers"]["above"] >= 1 and p["outliers"]["extremes"][0][0] == 1e9
    assert sum(b["count"] for b in p["histogram"]) == p["non_null"] and len(p["histogram"]) == 20
    assert any("outlier" in i for i in p["issues"])
    assert {r["coverholder"] for r in p["by_coverholder"]} == {"acme", "beta"}


def test_text_column_patterns_and_quality_flags(con):
    p = pf.column_profile(con, pf.source_sql("t"), "code")
    assert p["kind"] == "text" and p["text"]["leading_trailing_spaces"] == 400 and p["text"]["null_like"] == 400
    assert p["patterns"][0]["pattern"] in ("AA-99", "AA-9", "aa-999", "aa-99", "aa-9") or p["pattern_count"] > 1
    joined = " ".join(p["issues"])
    assert "leading/trailing" in joined and "placeholder" in joined and "differ only by case" in joined
    assert p["top_values"] and p["lengths"]


def test_date_column_periods_and_future_dates(con):
    con.execute("INSERT INTO t VALUES (9999, 1, 'AB-1', current_date + 30, 'x', NULL, 'acme')")
    p = pf.column_profile(con, pf.source_sql("t"), "d")
    assert p["kind"] == "date" and p["stats"]["future"] == 1 and p["by_period"]["unit"] == "month"
    assert sum(r[1] for r in p["by_period"]["rows"]) == p["non_null"]
    assert any("future" in i for i in p["issues"])


def test_unknown_column_and_unsafe_sql_are_rejected(con):
    with pytest.raises(LookupError):
        pf.column_profile(con, pf.source_sql("t"), "nope")
    with pytest.raises(Exception):
        pf.table_summary(con, "DROP TABLE t")
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 2001
    with pytest.raises(ValueError):
        pf.source_sql()


def test_profile_every_stage_saves_snapshots_and_shows_column_changes(world):  # noqa: F811
    con, root, ids = world
    map_layouts(con, ids)
    pid = make_pipeline(con, ids)
    assert pl.run_pipeline(con, pid)["status"] == "ok"
    out = pf.profile_pipeline(con, pid, run_id="r1")
    stages = {s["stage"]: s for s in out["stages"]}
    assert [stages[n]["rows"] for n in ("Normalise", "Enrich", "Merge")] == [4, 4, 4]
    assert "Class" in stages["Enrich"]["added_columns"] and "RiskCode" in stages["Merge"]["dropped_columns"]
    assert {r["coverholder"]: r["rows"] for r in stages["Enrich"]["by_coverholder"]} == {"ACME": 2, "Beta": 2}
    assert pf.profile_pipeline(con, pid, coverholder="ACME", store=False)["stages"][1]["rows"] == 2
    hist = pf.snapshot_history(con, pid)
    assert len(hist) == 3 and {h["stage"] for h in hist} == {"Normalise", "Enrich", "Merge"}
    deep = pf.column_profile(con, pf.source_sql("df_enriched"), "Class")
    assert deep["kind"] == "text" and {t["value"] for t in deep["top_values"]} == {"Property", "Marine", "PROPERTY", "CASUALTY"}
    assert any("differ only by case" in i for i in deep["issues"])


def test_stage_not_run_yet_is_reported_not_fatal(world):  # noqa: F811
    con, root, ids = world
    map_layouts(con, ids)
    pid = make_pipeline(con, ids)
    out = pf.profile_pipeline(con, pid)
    assert all("Not created yet" in s["error"] for s in out["stages"])


def test_cli_profile_stages(world, capsys):  # noqa: F811
    con, root, ids = world
    map_layouts(con, ids)
    pid = make_pipeline(con, ids)
    pl.run_pipeline(con, pid)
    db = con.execute("PRAGMA database_list").fetchall()[0][2]
    con.close()
    assert main(["profile-stages", "--db", db, "--name", "Per coverholder", "--column", "Class"]) == 0
    text = capsys.readouterr().out
    assert "df_enriched: 4 rows" in text and "df_final.Class" in text and "Marine" in text


def test_decimal_columns_are_profiled(con):
    con.execute("CREATE TABLE m AS SELECT (i * 1.25)::DECIMAL(18,3) prem FROM range(1, 300) r(i)")
    p = pf.column_profile(con, pf.source_sql("m"), "prem")
    assert p["stats"]["max"] == pytest.approx(373.75) and len(p["histogram"]) == 20 and p["outliers"]["below"] == 0
