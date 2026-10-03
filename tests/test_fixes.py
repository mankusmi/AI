"""Regression tests for the review fixes: bulk insert, culture-aware dates, typed comparisons,
rounding, transactional / replacing loads, concurrent loads."""
import datetime
import json
import threading
import time
from decimal import Decimal

import duckdb
import pytest

from sp_profiler import dataflow as df
from sp_profiler import mapping_load as ml
from sp_profiler import transforms as tf
from sp_profiler.bulk import bulk_insert
from sp_profiler.m2sql import Translator, register_udfs, stage_table
from sp_profiler.mparse import parse_expr
from sp_profiler.store import open_db

from . import test_m_transform as T
from .test_m_transform import env, make_book, map_inputs  # noqa: F401  (fixtures)


# ---------------------------------------------------------------- 1. bulk insert
def test_bulk_insert_roundtrip_and_speed():
    con = duckdb.connect()
    con.execute("create table t(a varchar, b bigint, c decimal(38,10), d timestamp, e timestamptz, f boolean, g double, h date)")
    rows = [("plain", 1, Decimal("1.5"), datetime.datetime(2024, 1, 5, 3, 4, 5),
             datetime.datetime(2024, 1, 5, tzinfo=datetime.timezone.utc), True, 0.1, datetime.date(2024, 2, 3)),
            ("", None, None, None, None, None, None, None),
            (None, 2, Decimal("-3"), None, None, False, float("inf"), None),
            ('com,ma "q"\nnew line ünï £', 3, Decimal("0"), None, None, None, 1e-7, None)]
    assert bulk_insert(con, "t", list("abcdefgh"), rows) == 4
    got = con.execute("select a, b, c from t order by b nulls first").fetchall()
    assert got[0][0] == "" and got[1][0] == "plain" and got[2][0] is None and got[3][0].startswith('com,ma "q"\nnew')
    assert con.execute("select count(*) filter (where a = ''), count(*) filter (where a is null) from t").fetchone() == (1, 1)
    con.execute("create table big(a varchar, b varchar, c varchar, d varchar)")
    big = [("x" * 20, "y", "z", "w")] * 100_000
    t0 = time.time()
    bulk_insert(con, "big", list("abcd"), big)
    assert time.time() - t0 < 10          # executemany needed ~2.5 minutes for this
    assert con.execute("select count(*) from big").fetchone()[0] == 100_000
    with pytest.raises(KeyError):
        bulk_insert(con, "big", ["nope"], [("x",)])


# ---------------------------------------------------------------- 2. culture-aware dates
def test_date_order_and_ambiguity():
    assert ml.date_order_for_culture("en-US") == "MDY" and ml.date_order_for_culture("en-GB") == "DMY"
    assert ml.date_order_for_culture("") == "DMY"
    assert ml.coerce("06/02/2024", "date", "DMY")[0] == datetime.date(2024, 2, 6)
    assert ml.coerce("06/02/2024", "date", "MDY")[0] == datetime.date(2024, 6, 2)
    assert ml.coerce("25/12/2024", "date", "MDY")[0] == datetime.date(2024, 12, 25)   # only valid day-first
    assert ml.coerce("12/25/2024", "date", "DMY")[0] == datetime.date(2024, 12, 25)   # only valid month-first
    assert ml.is_ambiguous_date("06/02/2024") and not ml.is_ambiguous_date("25/12/2024")
    assert not ml.is_ambiguous_date("05/05/2024") and not ml.is_ambiguous_date("2024-02-06")


def test_sql_macros_follow_date_order():
    con = duckdb.connect()
    register_udfs(con)
    assert con.execute("select sp_ts_dmy('06/02/2024'), sp_ts_mdy('06/02/2024')").fetchone() == (
        datetime.datetime(2024, 2, 6), datetime.datetime(2024, 6, 2))
    assert con.execute("select sp_ts_mdy('25/12/2024'), sp_ts_dmy('12/25/2024')").fetchone() == (
        datetime.datetime(2024, 12, 25), datetime.datetime(2024, 12, 25))


M_DATE = 'let S = Excel.Workbook(File.Contents("x"), null, true), P = Table.PromoteHeaders(S), ' \
         'T = Table.TransformColumnTypes(P, {{"d", type date}}) in T'


def _dates(order, m=M_DATE):
    p = Translator({"E": m}, date_order=order).translate("E")
    cur = duckdb.connect().cursor()
    register_udfs(cur)
    stage_table(cur, p.inputs, [(2, "06/02/2024")])
    return cur.execute(p.sql()).fetchone()[1]


def test_translator_uses_dataflow_culture_and_step_culture():
    assert _dates("DMY") == datetime.date(2024, 2, 6)
    assert _dates("MDY") == datetime.date(2024, 6, 2)
    explicit = M_DATE.replace('{{"d", type date}})', '{{"d", type date}}, "en-US")')
    assert _dates("DMY", explicit) == datetime.date(2024, 6, 2)      # the step's own culture wins


def test_load_uses_culture_from_dataflow(env, tmp_path):  # noqa: F811
    con, did = env
    model = json.loads(json.dumps(T.MODEL))
    model["culture"] = "en-US"
    model["name"] = "US"
    did2, _ = df.store_dataflow(con, df.parse_model_json(json.dumps(model)))
    plan = tf.resolve(con, did2, "Policies")
    assert plan["date_order"] == "MDY" and plan["culture"] == "en-US" and plan["date_order_source"] == "culture"
    map_inputs(con, did2)
    r = ml.load_entity(con, did2, "Policies")
    assert r["date_order"] == "MDY" and r["ambiguous_dates"] >= 1          # P2's 06/02/2024 text date is ambiguous
    year = con.execute("select Year from df_policies where PolicyNumber = 'P2'").fetchone()[0]
    assert year == 2024
    tf.save_settings(con, did2, "Policies", True, "", False, [], "DMY")     # explicit setting overrides culture
    assert tf.resolve(con, did2, "Policies")["date_order"] == "DMY"
    with pytest.raises(ValueError):
        tf.save_settings(con, did2, "Policies", True, "", False, [], "YMD")


# ---------------------------------------------------------------- 3. typed comparisons
def _run(m, staged):
    p = Translator({"E": m}).translate("E")
    cur = duckdb.connect().cursor()
    register_udfs(cur)
    stage_table(cur, p.inputs, staged)
    return p, cur.execute(p.sql()).fetchall()


def test_column_vs_column_after_type_step():
    m = ('let S = Excel.Workbook(File.Contents("x"), null, true), P = Table.PromoteHeaders(S), '
         'T = Table.TransformColumnTypes(P, {{"a", type number}, {"b", Int64.Type}}), '
         'F = Table.SelectRows(T, each [a] = [b]) in F')
    p, rows = _run(m, [(2, "1.0", "1"), (3, "2", "2"), (4, "3.5", "3")])
    assert p.complete and sorted(r[0] for r in rows) == [2, 3]            # 1.0 = 1 and 2 = 2


def test_kinds_survive_rename_and_addcolumn():
    m = ('let S = Excel.Workbook(File.Contents("x"), null, true), P = Table.PromoteHeaders(S), '
         'T = Table.TransformColumnTypes(P, {{"a", type number}}), R = Table.RenameColumns(T, {{"a", "amt"}}), '
         'A = Table.AddColumn(R, "dbl", each [amt] * 2, type number), '
         'F = Table.SelectRows(A, each [dbl] = [amt] + [amt]) in F')
    p, rows = _run(m, [(2, "1.50"), (3, "2")])
    assert len(rows) == 2


# ---------------------------------------------------------------- 4. rounding
def test_number_round_is_half_even_by_default():
    m = ('let S = Excel.Workbook(File.Contents("x"), null, true), P = Table.PromoteHeaders(S), '
         'T = Table.TransformColumnTypes(P, {{"x", type number}}), '
         'A = Table.AddColumn(T, "even", each Number.Round([x]), type number), '
         'B = Table.AddColumn(A, "away", each Number.Round([x], 0, RoundingMode.AwayFromZero), type number), '
         'C = Table.AddColumn(B, "up", each Number.RoundUp([x]), type number), '
         'D = Table.AddColumn(C, "two", each Number.Round([x], 1), type number) in D')
    p, rows = _run(m, [(2, "2.5"), (3, "3.5"), (4, "-2.5"), (5, "2.25")])
    got = {r[0]: r[2:] for r in rows}
    assert got[2][:3] == (2.0, 3.0, 3.0) and got[3][:3] == (4.0, 4.0, 4.0) and got[4][:3] == (-2.0, -3.0, -2.0)
    assert got[5][3] == 2.2                                                # half-even at one digit (2.25 -> 2.2)


# ---------------------------------------------------------------- `is null`
def test_is_null_parses_and_translates():
    assert parse_expr("each [x] is null")["body"]["t"] == "is"
    m = ('let S = Excel.Workbook(File.Contents("x"), null, true), P = Table.PromoteHeaders(S), '
         'F = Table.SelectRows(P, each not ([x] is null)) in F')
    p, rows = _run(m, [(2, "a"), (3, None)])
    assert [r[0] for r in rows] == [2] and p.complete


# ---------------------------------------------------------------- 5. forced reload replaces
def test_force_reload_replaces_not_duplicates(env):  # noqa: F811
    con, did = env
    map_inputs(con, did)
    assert ml.load_entity(con, did, "Policies")["rows"] == 3
    r = ml.load_entity(con, did, "Policies", force=True)
    assert r["rows"] == 3 and r["replaced_rows"] == 3
    assert con.execute("select count(*) from df_policies").fetchone()[0] == 3
    assert con.execute("select count(*) from load_log where entity = 'Policies' and status = 'ok'").fetchone()[0] == 2
    assert ml.load_entity(con, did, "Policies")["sheets_planned"] == 0


# ---------------------------------------------------------------- 6. transactions + locking
def test_failed_sheet_leaves_no_partial_rows_and_can_retry(env, monkeypatch):  # noqa: F811
    con, did = env
    map_inputs(con, did)
    import sp_profiler.bulk as bulk
    calls = {"n": 0}
    orig = bulk.bulk_insert

    def flaky(c, table, cols, rows, chunk=100_000):
        if table == "df_policies" and rows:
            calls["n"] += 1
            if calls["n"] == 1:
                orig(c, table, cols, rows[:1], chunk)          # insert one row, then blow up
                raise RuntimeError("disk full")
        return orig(c, table, cols, rows, chunk)

    monkeypatch.setattr(bulk, "bulk_insert", flaky)
    r = ml.load_entity(con, did, "Policies")
    assert r["sheets_failed"] == 1 and r["sheets_loaded"] == 1
    failed = con.execute("select source_path, error from load_log where status = 'error'").fetchall()
    assert len(failed) == 1 and "disk full" in failed[0][1]
    loaded_paths = {x[0] for x in con.execute("select distinct _source_path from df_policies").fetchall()}
    assert failed[0][0] not in loaded_paths                      # rolled back: nothing from the failed sheet
    monkeypatch.setattr(bulk, "bulk_insert", orig)
    r2 = ml.load_entity(con, did, "Policies")                    # retry picks up only the failed sheet
    assert r2["sheets_loaded"] == 1 and con.execute("select count(*) from df_policies").fetchone()[0] == 3


def test_concurrent_loads_do_not_duplicate(env):  # noqa: F811
    con, did = env
    map_inputs(con, did)
    out = []
    ths = [threading.Thread(target=lambda: out.append(ml.load_entity(con.cursor(), did, "Policies"))) for _ in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert sum(o["rows"] for o in out) == 3
    assert con.execute("select count(*) from df_policies").fetchone()[0] == 3


# ---------------------------------------------------------------- more Power Query coverage
BASE = 'let S = Excel.Workbook(File.Contents("x"), null, true), P = Table.PromoteHeaders(S), '


def _rows(m, raw_rows, entity="E", queries=None):
    """Stage ``raw_rows`` (a list of {column: value}) the way the loader does, then run the translated pipeline."""
    p = Translator({entity: m, **(queries or {})}).translate(entity)
    cols = list(p.inputs)
    if p.dynamic_columns:
        cols += [c for c in raw_rows[0] if c not in cols]
    cur = duckdb.connect().cursor()
    register_udfs(cur)
    stage_table(cur, cols, [(i + 2, *[r.get(c) for c in cols]) for i, r in enumerate(raw_rows)])
    res = cur.execute(p.sql())
    return p, [d[0] for d in res.description], res.fetchall()


def test_group_by_aggregations():
    m = BASE + ('T = Table.TransformColumnTypes(P, {{"amt", type number}}), '
                'G = Table.Group(T, {"pol"}, {{"total", each List.Sum([amt]), type number}, {"n", each Table.RowCount(_), Int64.Type}, '
                '{"ccys", each List.Count(List.Distinct([ccy])), Int64.Type}, {"top", each List.Max([amt]), type number}}) in G')
    p, cols, rows = _rows(m, [{"pol": "P1", "amt": "10", "ccy": "GBP"}, {"pol": "P1", "amt": "5.5", "ccy": "EUR"},
                         {"pol": "P2", "amt": "7", "ccy": "GBP"}, {"pol": "P1", "amt": "1", "ccy": "GBP"}])
    got = {r[cols.index("pol")]: dict(zip(cols, r)) for r in rows}
    assert p.complete and set(p.inputs) == {"pol", "amt", "ccy"}
    assert got["P1"]["total"] == 16.5 and got["P1"]["n"] == 3 and got["P1"]["ccys"] == 2 and got["P1"]["top"] == 10.0
    assert got["P2"]["n"] == 1 and "__row" not in cols
    local = m.replace('Int64.Type}, {"ccys"', 'Int64.Type}, {"ccys"').replace("}}) in G", "}}, GroupKind.Local) in G")
    assert not Translator({"E": local}).translate("E").complete


def test_unpivot_other_columns_and_explicit_unpivot():
    m = BASE + 'U = Table.UnpivotOtherColumns(P, {"pol"}, "Month", "Amount") in U'
    p, cols, rows = _rows(m, [{"pol": "P1", "Jan": "10", "Feb": "20", "Mar": None}, {"pol": "P2", "Jan": None, "Feb": "5", "Mar": "7"}])
    assert p.complete and p.inputs == ["pol"] and p.dynamic_columns        # month columns are staged from the sheet itself
    got = sorted((r[cols.index("pol")], r[cols.index("Month")], r[cols.index("Amount")]) for r in rows)
    assert got == [("P1", "Feb", "20"), ("P1", "Jan", "10"), ("P2", "Feb", "5"), ("P2", "Mar", "7")]      # nulls dropped, as in M
    assert {r[cols.index("__row")] for r in rows} == {2, 3}                                                # provenance survives
    m2 = BASE + 'U = Table.Unpivot(P, {"Jan", "Feb"}, "Month", "Amount") in U'
    p2, cols2, rows2 = _rows(m2, [{"Jan": "10", "Feb": "20"}])
    assert sorted(r[cols2.index("Month")] for r in rows2) == ["Feb", "Jan"] and set(p2.inputs) == {"Jan", "Feb"}


def test_split_and_combine_columns():
    m = BASE + ('S = Table.SplitColumn(P, "full", Splitter.SplitTextByDelimiter(","), {"first", "second", "third"}), '
                'C = Table.CombineColumns(S, {"first", "second"}, Combiner.CombineTextByDelimiter(" ", QuoteStyle.None), "name") in C')
    p, cols, rows = _rows(m, [{"full": "Ann,Lee"}])
    r = dict(zip(cols, rows[0]))
    assert p.complete and r["name"] == "Ann Lee" and r["third"] is None and "full" not in cols and "first" not in cols
    bad = BASE + 'S = Table.SplitColumn(P, "full", Splitter.SplitTextByPositions({0, 3}), {"a", "b"}) in S'
    assert not Translator({"E": bad}).translate("E").complete


def test_parameters_become_constants():
    queries = {"MinYear": "2023 meta [IsParameterQuery = true, Type = \"Number\", IsParameterQueryRequired = true]",
               "Ccy": '"GBP" meta [IsParameterQuery = true]'}
    m = BASE + ('T = Table.TransformColumnTypes(P, {{"yr", Int64.Type}}), '
                'F = Table.SelectRows(T, each [yr] >= MinYear and [ccy] = Ccy) in F')
    p, cols, rows = _rows(m, [{"yr": "2022", "ccy": "GBP"}, {"yr": "2024", "ccy": "GBP"}, {"yr": "2024", "ccy": "EUR"}], queries=queries)
    assert p.complete and [r[0] for r in rows] == [3]
    missing = BASE + 'F = Table.SelectRows(P, each [yr] >= Unknown) in F'
    assert not Translator({"E": missing}).translate("E").complete


def test_query_splitting_survives_attributes_strings_and_comments():
    from sp_profiler.dataflow import extract_m_queries
    doc = ('section Section1;\n[Description = "a; b"] shared Year = 2024 meta [IsParameterQuery = true];\n'
           'shared #"My Query" = let S = "shared X = 1;", // shared Y = 2;\n  T = Table.FromRows({{1}}, {"a"}) in T;\n'
           'shared Other = "x";\n')
    q = extract_m_queries(doc)
    assert list(q) == ["Year", "My Query", "Other"] and "shared X = 1;" in q["My Query"]
    assert parse_expr(q["Year"])["v"] == 2024


def test_unpivot_dataflow_loads_month_columns_end_to_end(tmp_path):
    from sp_profiler.cli import main
    src = tmp_path / "src"
    src.mkdir()
    make_book(src / "m.xlsx", ["Policy", "Jan", "Feb", "Mar"], [["P1", 10, 20, None], ["P2", None, 5, "7"]])
    model = {"name": "Monthly", "culture": "en-GB", "pbi:mashup": {"document": "section Section1;\r\nshared Monthly = " + (
        'let S = Excel.Workbook(File.Contents("x"), null, true), P = Table.PromoteHeaders(S), '
        'U = Table.UnpivotOtherColumns(P, {"Policy"}, "Month", "Amount"), '
        'T = Table.TransformColumnTypes(U, {{"Amount", Currency.Type}}) in T') + ";\r\n"},
        "entities": [{"name": "Monthly", "attributes": [{"name": "Policy", "dataType": "string"},
                                                        {"name": "Month", "dataType": "string"}, {"name": "Amount", "dataType": "decimal"}]}]}
    db = tmp_path / "t.duckdb"
    assert main(["local", "--path", str(src), "--db", str(db)]) == 0
    con = open_db(str(db))
    did, _ = df.store_dataflow(con, df.parse_model_json(json.dumps(model)))
    plan = tf.resolve(con, did, "Monthly")
    assert plan["mode"] == "m" and plan["dynamic_columns"] and plan["inputs"] == ["Policy"]
    map_inputs(con, did, "Monthly")
    r = ml.load_entity(con, did, "Monthly")
    assert r["rows"] == 4 and r["sheets_failed"] == 0, r
    got = sorted(con.execute("select Policy, Month, Amount, _excel_row from df_monthly").fetchall())
    assert [(g[0], g[1], g[2]) for g in got] == [("P1", "Feb", Decimal("20")), ("P1", "Jan", Decimal("10")),
                                                 ("P2", "Feb", Decimal("5")), ("P2", "Mar", Decimal("7"))]
    assert {g[3] for g in got} == {3, 4}
    assert tf.stage_sample(con.cursor(), plan, "Monthly", con.execute("select layout_hash from layouts").fetchone()[0]) == 2
