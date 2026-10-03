"""The multi-stage flow: coverholder folders -> dataflow 1 -> dataflow 2 (+ lookup workbook) -> merge dataflow."""
import datetime
import json
from decimal import Decimal
from pathlib import Path

import pytest

from sp_profiler import dataflow as df
from sp_profiler import mapping_load as ml
from sp_profiler import pipeline as pl
from sp_profiler import transforms as tf
from sp_profiler.runner import coverholder_resolver, run_profile
from sp_profiler.sources import LocalSource
from sp_profiler.store import open_db

from .test_m_transform import make_book

D = datetime.datetime
NAV = ('Source = PowerPlatform.Dataflows(null), Nav = Source{[Id = "Workspaces"]}[Data], '
       'Data = Nav{[dataflowId = "abc"]}[Data], Upstream = Data{[entity = "%s", version = ""]}[Data]')

DF1_M = ('let Source = Excel.Workbook(File.Contents("x"), null, true), P = Table.PromoteHeaders(Source), '
         'R = Table.RenameColumns(P, {{"Policy No", "Policy"}, {"Gross Premium", "Premium"}, {"Currency", "Ccy"}}), '
         'T = Table.TransformColumnTypes(R, {{"Premium", Currency.Type}, {"Inception", type date}}), '
         'F = Table.SelectRows(T, each [Policy] <> null) in F')
DF2_M = ('let ' + NAV % "Bordereau" + ', '
         'Merged = Table.NestedJoin(Upstream, {"Ccy"}, FxMap, {"Ccy"}, "fx", JoinKind.LeftOuter), '
         'Expanded = Table.ExpandTableColumn(Merged, "fx", {"Rate", "Class"}), '
         'Typed = Table.TransformColumnTypes(Expanded, {{"Rate", type number}}), '
         'Added = Table.AddColumn(Typed, "PremiumGBP", each [Premium] * [Rate], Currency.Type) in Added')
FXMAP_M = ('let S = Excel.Workbook(File.Contents("mapping.xlsx"), null, true), D = S{[Item = "Rates"]}[Data], '
           'P = Table.PromoteHeaders(D) in P')
DF3_M = ('let ' + NAV % "Enriched" + ', '
         'R = Table.RenameColumns(Upstream, {{"_coverholder", "Coverholder"}}), '
         'S = Table.SelectColumns(R, {"Coverholder", "Policy", "PremiumGBP", "Class"}) in S')


def attrs(*pairs):
    return [{"name": n, "dataType": t} for n, t in pairs]


def model(name, entities, extra=None):
    doc = "section Section1;\r\n" + "".join(f"shared {n} = {m};\r\n" for n, m in {**{e[0]: e[2] for e in entities}, **(extra or {})}.items())
    return json.dumps({"name": name, "culture": "en-GB", "pbi:mashup": {"document": doc},
                       "entities": [{"name": n, "attributes": a} for n, a, _ in entities]})


DF1 = model("Normalise", [("Bordereau", attrs(("Policy", "string"), ("Premium", "decimal"), ("Ccy", "string"), ("Inception", "dateTime")), DF1_M)])
DF2 = model("Enrich", [("Enriched", attrs(("Policy", "string"), ("Premium", "decimal"), ("Ccy", "string"), ("Rate", "double"),
                                          ("Class", "string"), ("PremiumGBP", "decimal")), DF2_M)], {"FxMap": FXMAP_M})
DF3 = model("Merge", [("Final", attrs(("Coverholder", "string"), ("Policy", "string"), ("PremiumGBP", "decimal"), ("Class", "string")), DF3_M)])

ACME_H = ["Policy No", "Gross Premium", "Currency", "Inception"]
BETA_H = ["PolicyRef", "Premium", "Ccy", "Start"]


def bdx(path: Path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    make_book(path, headers, rows)


def rates(path: Path, gbp=1.0, eur=0.85):
    wb = __import__("openpyxl").Workbook()
    ws = wb.active
    ws.title = "Rates"
    ws.append(["Currency rates"])
    ws.append(["Ccy", "Rate", "Class"])
    ws.append(["GBP", gbp, "Domestic"])
    ws.append(["EUR", eur, "Foreign"])
    ws.append(["USD", 0.79, "Foreign"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "share"
    bdx(root / "ACME" / "jan.xlsx", ACME_H, [["A1", 100, "GBP", D(2024, 1, 5)], ["A2", 200, "EUR", D(2024, 1, 6)]])
    bdx(root / "Beta" / "q1.xlsx", BETA_H, [["B1", 50, "USD", D(2024, 2, 1)], ["B2", 80, "GBP", D(2024, 2, 2)]])
    rates(root / "Reference" / "fx_mapping.xlsx")
    con = open_db(str(tmp_path / "t.duckdb"))

    def profile(folder, mode="folder"):
        src = LocalSource(root / folder)
        return run_profile(con, src, "local", str(root / folder),
                           coverholder_of=coverholder_resolver(mode, "", folder if mode == "folder" else ""))
    profile("ACME")
    profile("Beta")
    run_profile(con, LocalSource(root / "Reference"), "local", str(root / "Reference"))      # lookup workbook: no coverholder
    ids = {n: df.store_dataflow(con, df.parse_model_json(m))[0] for n, m in (("DF1", DF1), ("DF2", DF2), ("DF3", DF3))}
    return con, root, ids, profile


def map_stage1(con, ids):
    """Map each coverholder's layout onto the inputs the dataflow-1 steps read."""
    plan = tf.resolve(con, ids["DF1"], "Bordereau")
    assert plan["mode"] == "m" and set(plan["inputs"]) == {"Policy No", "Gross Premium", "Currency", "Inception"}, plan["status"]
    by_header = {"policy no": "Policy No", "policyref": "Policy No", "gross premium": "Gross Premium", "premium": "Gross Premium",
                 "currency": "Currency", "ccy": "Currency", "inception": "Inception", "start": "Inception"}
    for lh, nh in con.execute("select layout_hash, norm_headers from layouts").fetchall():
        pairs = [{"norm_header": h, "attribute": by_header[h]} for h in nh if h in by_header]
        if pairs:
            ml.save_mapping(con, "Bordereau", lh, pairs, plan["inputs"], "input")


def build_pipeline(con, ids):
    ref = tf.import_reference_stored(con, con.execute("select sha256 from v_files where name = 'fx_mapping.xlsx'").fetchone()[0],
                                     "ref_fx", "Rates")
    assert ref["rows"] == 3 and ref["columns"] == ["Ccy", "Rate", "Class"]
    con.execute("INSERT INTO query_bindings (dataflow_id, query_name, table_name) VALUES (?, 'FxMap', 'ref_fx')", [ids["DF2"]])
    return pl.save_pipeline(con, "Bordereaux", [
        {"name": "Normalise", "kind": "files", "dataflow_id": ids["DF1"], "entity": "Bordereau", "output_table": "df_bordereau"},
        {"name": "Enrich", "kind": "stage", "dataflow_id": ids["DF2"], "entity": "Enriched", "output_table": "df_enriched"},
        {"name": "Merge", "kind": "merge", "dataflow_id": ids["DF3"], "entity": "Final", "output_table": "df_final"}])


def statuses(report):
    out = {ch: [s["status"] for s in c["steps"]] for ch, c in report["coverholders"].items()}
    out["merge"] = [s["status"] for s in report["merge"]]
    return out


# ------------------------------------------------------------------------------------------------
def test_coverholders_are_tagged_from_folders(world):
    con, root, ids, profile = world
    assert {c["name"]: c["files"] for c in pl.coverholders(con)} == {"": 1, "ACME": 1, "Beta": 1}
    assert con.execute("select count(*) from v_layouts").fetchone()[0] == 3
    assert sorted(con.execute("select coverholders from v_layouts where coverholders is not null").fetchone()[0]) in (["ACME"], ["Beta"])
    # subfolder mode: one run over the whole share labels files by their first-level folder
    run_profile(con, LocalSource(root), "local", str(root), coverholder_of=coverholder_resolver("subfolders"))
    assert {c["name"] for c in pl.coverholders(con)} == {"ACME", "Beta", "Reference"}


def test_full_flow_files_to_stage_to_merge(world):
    con, root, ids, profile = world
    map_stage1(con, ids)
    pid = build_pipeline(con, ids)
    rep = pl.run_pipeline(con, pid)
    assert rep["status"] == "ok", rep
    assert statuses(rep) == {"ACME": ["loaded", "loaded"], "Beta": ["loaded", "loaded"], "merge": ["loaded"]}
    # stage 1: normalised rows per coverholder, in one table, with provenance
    got = con.execute("select _coverholder, Policy, Premium, Ccy from df_bordereau order by Policy").fetchall()
    assert got == [("ACME", "A1", Decimal("100"), "GBP"), ("ACME", "A2", Decimal("200"), "EUR"),
                   ("Beta", "B1", Decimal("50"), "USD"), ("Beta", "B2", Decimal("80"), "GBP")]
    # stage 2: the lookup workbook was applied (rates by currency)
    enr = {r[0]: r[1:] for r in con.execute("select Policy, Rate, Class, PremiumGBP, _coverholder from df_enriched").fetchall()}
    assert enr["A2"][:2] == (0.85, "Foreign") and enr["A2"][2] == Decimal("170") and enr["B1"][2] == Decimal("39.5")
    assert enr["A1"][3] == "ACME" and enr["B2"][3] == "Beta"            # coverholder kept through the stage
    # merge: one final table across coverholders
    final = con.execute("select Coverholder, Policy, PremiumGBP, Class from df_final order by Policy").fetchall()
    assert final == [("ACME", "A1", Decimal("100"), "Domestic"), ("ACME", "A2", Decimal("170"), "Foreign"),
                     ("Beta", "B1", Decimal("39.5"), "Foreign"), ("Beta", "B2", Decimal("80"), "Domestic")]
    # per-coverholder views exist for binding/inspection
    assert con.execute("select count(*) from df_enriched__acme").fetchone()[0] == 2


def test_rerun_does_nothing_and_only_affected_work_is_redone(world):
    con, root, ids, profile = world
    map_stage1(con, ids)
    pid = build_pipeline(con, ids)
    pl.run_pipeline(con, pid)
    again = pl.run_pipeline(con, pid)
    assert statuses(again) == {"ACME": ["up_to_date", "up_to_date"], "Beta": ["up_to_date", "up_to_date"], "merge": ["up_to_date"]}

    # 1. a new file for ACME only -> ACME stages and the merge rerun; Beta is untouched
    bdx(root / "ACME" / "feb.xlsx", ACME_H, [["A3", 300, "GBP", D(2024, 2, 9)]])
    profile("ACME")
    rep = pl.run_pipeline(con, pid)
    assert statuses(rep) == {"ACME": ["loaded", "loaded"], "Beta": ["up_to_date", "up_to_date"], "merge": ["loaded"]}
    assert con.execute("select count(*) from df_final").fetchone()[0] == 5

    # 2. the mapping workbook changes -> re-imported automatically; both coverholders' stage 2 + merge rerun, stage 1 does not
    rates(root / "Reference" / "fx_mapping.xlsx", eur=0.9)
    run_profile(con, LocalSource(root / "Reference"), "local", str(root / "Reference"))
    rep = pl.run_pipeline(con, pid)
    assert [r["table"] for r in rep["refreshed_references"]] == ["ref_fx"]
    assert statuses(rep) == {"ACME": ["up_to_date", "loaded"], "Beta": ["up_to_date", "loaded"], "merge": ["loaded"]}
    assert con.execute("select PremiumGBP from df_final where Policy = 'A2'").fetchone()[0] == Decimal("180")

    # 3. force reruns everything
    rep = pl.run_pipeline(con, pid, force=True)
    assert all(s != "up_to_date" for c in rep["coverholders"].values() for s in [x["status"] for x in c["steps"]][1:])


def test_unmapped_layout_blocks_only_that_coverholder_until_fixed(world):
    con, root, ids, profile = world
    map_stage1(con, ids)
    pid = build_pipeline(con, ids)
    pl.run_pipeline(con, pid)
    bdx(root / "Beta" / "q2.xlsx", ["Ref", "Amount", "Money", "Date", "Extra"], [["B3", 10, "EUR", D(2024, 3, 1), "x"]])   # new layout
    profile("Beta")
    rep = pl.run_pipeline(con, pid)
    beta = rep["coverholders"]["Beta"]
    assert [s["status"] for s in beta["steps"]] == ["needs_mapping", "blocked"] and not beta["ok"]
    assert "q2.xlsx" in beta["steps"][0]["detail"] and "no column mapping" in beta["steps"][0]["detail"]
    assert rep["coverholders"]["ACME"]["ok"] and rep["status"] == "partial"
    assert rep["merge"][0]["status"] == "blocked" and "Beta" in rep["merge"][0]["detail"]
    partial = pl.run_pipeline(con, pid, allow_partial_merge=True)
    assert partial["merge"][0]["status"] in ("loaded", "up_to_date")
    # the user marks the layout as not a bordereau -> pipeline completes
    lh = ml.unmapped_layouts(con, "Bordereau", "input", "Beta")[0]["layout_hash"]
    ml.set_layout_ignored(con, "Bordereau", lh, True)
    rep = pl.run_pipeline(con, pid)
    assert rep["status"] == "ok" and rep["coverholders"]["Beta"]["ok"]


def test_failure_in_one_coverholder_does_not_stop_others(world, monkeypatch):
    con, root, ids, profile = world
    map_stage1(con, ids)
    pid = build_pipeline(con, ids)
    real = pl._run_derived_stage

    def flaky(con_, pipe, stage, ch, force, run_id):
        if ch == "Beta" and stage["kind"] == "stage":
            raise RuntimeError("boom")
        return real(con_, pipe, stage, ch, force, run_id)
    monkeypatch.setattr(pl, "_run_derived_stage", flaky)
    rep = pl.run_pipeline(con, pid)
    assert statuses(rep)["ACME"] == ["loaded", "loaded"] and statuses(rep)["Beta"] == ["loaded", "error"]
    assert "boom" in rep["coverholders"]["Beta"]["steps"][1]["detail"] and rep["status"] == "partial"
    assert rep["merge"][0]["status"] == "blocked"
    monkeypatch.setattr(pl, "_run_derived_stage", real)
    assert pl.run_pipeline(con, pid)["status"] == "ok"


def test_missing_lookup_binding_and_missing_columns_are_reported_clearly(world):
    con, root, ids, profile = world
    map_stage1(con, ids)
    ref = tf.import_reference_stored(con, con.execute("select sha256 from v_files where name = 'fx_mapping.xlsx'").fetchone()[0], "ref_fx", "Rates")
    pid = pl.save_pipeline(con, "P", [
        {"name": "Normalise", "kind": "files", "dataflow_id": ids["DF1"], "entity": "Bordereau", "output_table": "df_bordereau"},
        {"name": "Enrich", "kind": "stage", "dataflow_id": ids["DF2"], "entity": "Enriched", "output_table": "df_enriched"}])
    rep = pl.run_pipeline(con, pid)                                      # FxMap not bound yet
    step = rep["coverholders"]["ACME"]["steps"][1]
    assert step["status"] == "error" and "FxMap" in step["detail"] and "bind" in step["detail"]
    assert ref["rows"] == 3
    # a stage whose steps read a column the previous stage does not produce
    bad = model("Bad", [("Out", attrs(("X", "string")), 'let ' + NAV % "Bordereau" + ', R = Table.RenameColumns(Upstream, {{"Nope", "X"}}) in R')])
    bid = df.store_dataflow(con, df.parse_model_json(bad))[0]
    pid2 = pl.save_pipeline(con, "P2", [
        {"name": "Normalise", "kind": "files", "dataflow_id": ids["DF1"], "entity": "Bordereau", "output_table": "df_bordereau"},
        {"name": "Bad", "kind": "stage", "dataflow_id": bid, "entity": "Out", "output_table": "df_out"}])
    step = pl.run_pipeline(con, pid2)["coverholders"]["ACME"]["steps"][1]
    assert step["status"] == "error" and "Nope" in step["detail"] and "Available" in step["detail"]


def test_definition_validation(world):
    con, root, ids, profile = world
    ok = {"kind": "files", "dataflow_id": ids["DF1"], "entity": "Bordereau", "output_table": "df_bordereau"}
    with pytest.raises(pl.PipelineError, match="first stage"):
        pl.save_pipeline(con, "x", [{**ok, "kind": "stage"}])
    with pytest.raises(pl.PipelineError, match="Merge stages must come after"):
        pl.save_pipeline(con, "x", [ok, {**ok, "kind": "merge", "name": "m", "output_table": "df_m"},
                                    {**ok, "kind": "stage", "name": "s", "output_table": "df_s"}])
    with pytest.raises(pl.PipelineError, match="not found"):
        pl.save_pipeline(con, "x", [{**ok, "entity": "Nope"}])
    with pytest.raises(pl.PipelineError, match="not created by sp-profile"):
        pl.save_pipeline(con, "x", [{**ok, "output_table": "files"}])                       # must not overwrite system tables
    with pytest.raises(pl.PipelineError, match="letters, digits"):
        pl.save_pipeline(con, "x", [{**ok, "output_table": "bad name;drop"}])
    with pytest.raises(pl.PipelineError, match="Two stages write"):
        pl.save_pipeline(con, "x", [ok, {**ok, "kind": "stage", "name": "s", "dataflow_id": ids["DF2"], "entity": "Enriched"}])
    pid = pl.save_pipeline(con, "keep", [ok])
    assert pl.get_pipeline(con, pid)["stages"][0]["output_table"] == "df_bordereau"
    pl.save_pipeline(con, "keep", [ok], pipeline_id=pid)                                        # re-saving the same pipeline is fine
    pl.delete_pipeline(con, pid)
    assert pl.list_pipelines(con) == []


def test_per_coverholder_dataflow_override(world):
    con, root, ids, profile = world
    map_stage1(con, ids)
    # Beta's stage 1 uses its own dataflow (same output shape, different steps: premiums are in cents)
    beta_m = DF1_M.replace('T = Table.TransformColumnTypes(R, {{"Premium", Currency.Type}, {"Inception", type date}}), ',
                           'T0 = Table.TransformColumnTypes(R, {{"Premium", Currency.Type}, {"Inception", type date}}), '
                           'T = Table.AddColumn(Table.RemoveColumns(T0, {"Premium"}), "Premium", each [Premium] / 100, Currency.Type), ')
    beta_m = DF1_M.replace('F = Table.SelectRows(T, each [Policy] <> null) in F',
                           'U = Table.AddColumn(T, "P2", each [Premium] / 100, Currency.Type), '
                           'V = Table.RemoveColumns(U, {"Premium"}), W = Table.RenameColumns(V, {{"P2", "Premium"}}), '
                           'F = Table.SelectRows(W, each [Policy] <> null) in F')
    d1b = df.store_dataflow(con, df.parse_model_json(model("Normalise Beta", [("Bordereau", attrs(
        ("Policy", "string"), ("Premium", "decimal"), ("Ccy", "string"), ("Inception", "dateTime")), beta_m)])))[0]
    plan = tf.resolve(con, d1b, "Bordereau")
    assert plan["mode"] == "m", plan["status"]
    pid = build_pipeline(con, ids)
    stages = pl.get_pipeline(con, pid)["stages"]
    pl.save_pipeline(con, "Bordereaux", stages, [{"position": 0, "coverholder": "Beta", "dataflow_id": d1b, "entity": "Bordereau"}],
                     pipeline_id=pid)
    rep = pl.run_pipeline(con, pid)
    assert rep["status"] == "ok", rep
    prem = dict(con.execute("select Policy, Premium from df_bordereau").fetchall())
    assert prem["A1"] == Decimal("100") and prem["B1"] == Decimal("0.5")          # Beta's own steps applied


def test_command_line_runs_a_saved_pipeline(world, capsys, tmp_path):
    from sp_profiler.cli import main
    con, root, ids, profile = world
    map_stage1(con, ids)
    build_pipeline(con, ids)
    path = con.execute("PRAGMA database_list").fetchall()[0][2]
    con.close()
    assert main(["pipeline-list", "--db", path]) == 0
    assert "Bordereaux: Normalise -> Enrich -> Merge" in capsys.readouterr().out
    assert main(["pipeline-run", "--db", path, "--name", "Bordereaux"]) == 0
    out = capsys.readouterr().out
    assert "Pipeline 'Bordereaux': ok" in out and "ACME" in out and "Beta" in out and "Merge=loaded" in out
    assert main(["pipeline-run", "--db", path, "--name", "Bordereaux", "--coverholder", "ACME"]) == 0     # nothing changed
    assert "Merge=up_to_date" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="No pipeline named"):
        main(["pipeline-run", "--db", path, "--name", "nope"])


def test_databricks_export_handles_pipeline_outputs(world, tmp_path):
    from sp_profiler.export import export_databricks
    con, root, ids, profile = world
    map_stage1(con, ids)
    pid = build_pipeline(con, ids)
    # stage 1 writes to a table name of its own choosing
    stages = pl.get_pipeline(con, pid)["stages"]
    stages[0]["output_table"] = "bdx_normalised"
    pl.save_pipeline(con, "Bordereaux", stages, pipeline_id=pid)
    pl.run_pipeline(con, pid)
    m1 = export_databricks(con, tmp_path / "x", incremental=True)
    kinds = {t["name"]: t["kind"] for t in m1["tables"]}
    assert kinds["bdx_normalised"] == "entity" and kinds["df_enriched"] == "snapshot" and kinds["df_final"] == "snapshot"
    assert "pipeline_runs" in kinds and next(t for t in m1["tables"] if t["name"] == "df_final")["rows"] == 4
    assert {c["name"] for c in next(t for t in m1["tables"] if t["name"] == "bdx_normalised")["columns"]} >= {"_coverholder", "_source_id"}
    import time
    time.sleep(1.1)
    bdx(root / "ACME" / "feb.xlsx", ACME_H, [["A3", 300, "GBP", D(2024, 2, 9)]])
    profile("ACME")
    pl.run_pipeline(con, pid)
    m2 = export_databricks(con, tmp_path / "x", incremental=True)
    assert next(t for t in m2["tables"] if t["name"] == "bdx_normalised")["rows"] == 1               # only the new sheet
    assert next(t for t in m2["tables"] if t["name"] == "df_final")["rows"] == 5                     # derived tables: full snapshot
