"""Different dataflows per coverholder, one shared mapping workbook, and a merge that reads each coverholder's output."""
import json
from decimal import Decimal

import pytest

from sp_profiler import dataflow as df
from sp_profiler import mapping_load as ml
from sp_profiler import pipeline as pl
from sp_profiler import transforms as tf
from sp_profiler.runner import coverholder_resolver, run_profile
from sp_profiler.sources import LocalSource
from sp_profiler.store import open_db

from .test_pipeline import attrs, bdx, model

SRC = 'let Source = Excel.Workbook(File.Contents("x"), null, true), P = Table.PromoteHeaders(Source), '
NAV = 'Source = PowerPlatform.Dataflows(null), Nav = Source{[Id = "Workspaces"]}[Data]'

A1_M = SRC + ('R = Table.RenameColumns(P, {{"Policy No", "Policy"}, {"Gross Premium", "Premium"}, {"Risk Code", "RiskCode"}, '
              '{"Currency", "Ccy"}}), T = Table.TransformColumnTypes(R, {{"Premium", Currency.Type}}) in T')
B1_M = SRC + ('R = Table.RenameColumns(P, {{"Ref", "Policy"}, {"Risk", "RiskCode"}}), '
              'T = Table.TransformColumnTypes(R, {{"Prem (cents)", Currency.Type}}), '
              'A = Table.AddColumn(T, "Premium", each [#"Prem (cents)"] / 100, Currency.Type), '
              'D = Table.RemoveColumns(A, {"Prem (cents)"}) in D')
# dataflow 2 of each coverholder: its own linked source, and the SAME mapping workbook under a different query name / location,
# with the lookup query's own cleaning steps
RISKMAP_ACME = ('let S = Excel.Workbook(File.Contents("\\\\\\\\corp\\\\ref\\\\Risk Class Mapping.xlsx"), null, true), '
                'D = S{[Item = "Map", Kind = "Sheet"]}[Data], P = Table.PromoteHeaders(D), '
                'T = Table.TransformColumns(P, {{"RiskCode", Text.Trim}, {"Class", Text.Trim}}) in T')
CLASSMAP_BETA = ('let S = Excel.Workbook(Web.Contents("https://contoso.sharepoint.com/sites/Ref/Shared%20Documents/Risk%20Class%20Mapping.xlsx"), '
                 'null, true), D = S{[Item = "Map", Kind = "Sheet"]}[Data], P = Table.PromoteHeaders(D), '
                 'T = Table.TransformColumns(P, {{"RiskCode", Text.Trim}, {"Class", each Text.Upper(Text.Trim(_))}}) in T')


def df2_m(entity_in, mapping_query):
    return ('let ' + NAV + f', D = Nav{{[dataflowId = "g-{entity_in}"]}}[Data], Bdx = D{{[entity = "{entity_in}", version = ""]}}[Data], '
            f'J = Table.NestedJoin(Bdx, {{"RiskCode"}}, {mapping_query}, {{"RiskCode"}}, "m", JoinKind.LeftOuter), '
            'E = Table.ExpandTableColumn(J, "m", {"Class"}, {"Class"}) in E')


MERGE_M = ('let ' + NAV + ', AD = Nav{[dataflowId = "g-acme"]}[Data], ACME = AD{[entity = "AcmeOut", version = ""]}[Data], '
           'BD = Nav{[dataflowId = "g-beta"]}[Data], Beta = BD{[entity = "BetaOut", version = ""]}[Data], '
           'A = Table.SelectColumns(ACME, {"_coverholder", "Policy", "Premium", "Class"}), '
           'B = Table.SelectColumns(Beta, {"_coverholder", "Policy", "Premium", "Class"}), '
           'C = Table.Combine({A, B}), R = Table.RenameColumns(C, {{"_coverholder", "Coverholder"}}) in R')

OUT = attrs(("Policy", "string"), ("Premium", "decimal"), ("RiskCode", "string"), ("Class", "string"))
DFS = {
    "A1": model("ACME Normalise", [("AcmeBdx", attrs(("Policy", "string"), ("Premium", "decimal"), ("RiskCode", "string"), ("Ccy", "string")), A1_M)]),
    "B1": model("Beta Normalise", [("BetaBdx", attrs(("Policy", "string"), ("Premium", "decimal"), ("RiskCode", "string"), ("Ccy", "string")), B1_M)]),
    "A2": model("ACME Enrich", [("AcmeOut", OUT, df2_m("AcmeBdx", "RiskMap"))], {"RiskMap": RISKMAP_ACME}),
    "B2": model("Beta Enrich", [("BetaOut", OUT, df2_m("BetaBdx", "ClassMap"))], {"ClassMap": CLASSMAP_BETA}),
    "M": model("Merge", [("Final", attrs(("Coverholder", "string"), ("Policy", "string"), ("Premium", "decimal"), ("Class", "string")), MERGE_M)]),
}


def mapping_workbook(path, prop="Property"):
    wb = __import__("openpyxl").Workbook()
    ws = wb.active
    ws.title = "Map"
    ws.append(["RiskCode", "Class"])
    ws.append(["PROP ", prop])            # stray spaces: only matches if the lookup query's own Trim step runs
    ws.append(["MAR", "Marine"])
    ws.append([" CASU", "Casualty"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    wb.save(path)


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "share"
    bdx(root / "ACME" / "jan.xlsx", ["Policy No", "Gross Premium", "Risk Code", "Currency"],
        [["A1", 100, "PROP", "GBP"], ["A2", 200, "MAR", "EUR"]])
    bdx(root / "Beta" / "q1.xlsx", ["Ref", "Prem (cents)", "Risk", "Ccy"], [["B1", 5000, "PROP", "USD"], ["B2", 8000, "CASU", "GBP"]])
    mapping_workbook(root / "Reference" / "Risk Class Mapping.xlsx")
    con = open_db(str(tmp_path / "t.duckdb"))
    for folder in ("ACME", "Beta"):
        run_profile(con, LocalSource(root / folder), "local", str(root / folder),
                    coverholder_of=coverholder_resolver("folder", "", folder))
    run_profile(con, LocalSource(root / "Reference"), "local", str(root / "Reference"))
    ids = {k: df.store_dataflow(con, df.parse_model_json(m))[0] for k, m in DFS.items()}
    return con, root, ids


def map_layouts(con, ids):
    for key, ent, mapping in (("A1", "AcmeBdx", {"policy no": "Policy No", "gross premium": "Gross Premium", "risk code": "Risk Code",
                                                 "currency": "Currency"}),
                              ("B1", "BetaBdx", {"ref": "Ref", "prem cents": "Prem (cents)", "risk": "Risk", "ccy": "Ccy"})):
        plan = tf.resolve(con, ids[key], ent)
        assert plan["mode"] == "m" and set(plan["inputs"]) == set(mapping.values()), (plan["status"], plan["inputs"])
        for lh, nh in con.execute("select layout_hash, norm_headers from layouts").fetchall():
            if set(mapping) <= set(nh):
                ml.save_mapping(con, ent, lh, [{"norm_header": h, "attribute": mapping[h]} for h in nh if h in mapping], plan["inputs"], "input")


def make_pipeline(con, ids, merge_key="M"):
    ref = tf.import_reference_stored(con, con.execute("select sha256 from v_files where name = 'Risk Class Mapping.xlsx'").fetchone()[0],
                                     "ref_risk_map", "Map")
    assert ref["rows"] == 3
    stages = [{"name": "Normalise", "kind": "files", "dataflow_id": "", "entity": "", "output_table": "df_bdx"},
              {"name": "Enrich", "kind": "stage", "dataflow_id": "", "entity": "", "output_table": "df_enriched"},
              {"name": "Merge", "kind": "merge", "dataflow_id": ids[merge_key], "entity": "Final", "output_table": "df_final"}]
    ovr = [{"position": 0, "coverholder": "ACME", "dataflow_id": ids["A1"], "entity": "AcmeBdx"},
           {"position": 0, "coverholder": "Beta", "dataflow_id": ids["B1"], "entity": "BetaBdx"},
           {"position": 1, "coverholder": "ACME", "dataflow_id": ids["A2"], "entity": "AcmeOut"},
           {"position": 1, "coverholder": "Beta", "dataflow_id": ids["B2"], "entity": "BetaOut"}]
    return pl.save_pipeline(con, "Per coverholder", stages, ovr)


def test_each_coverholder_uses_its_own_dataflows_and_the_shared_mapping_file(world):
    con, root, ids = world
    map_layouts(con, ids)
    pid = make_pipeline(con, ids)
    rep = pl.run_pipeline(con, pid)
    assert rep["status"] == "ok", json.dumps(rep, indent=1, default=str)
    # stage 1: each coverholder's own steps (Beta's premiums arrive in cents)
    assert con.execute("select _coverholder, Policy, Premium from df_bdx order by Policy").fetchall() == [
        ("ACME", "A1", Decimal("100")), ("ACME", "A2", Decimal("200")), ("Beta", "B1", Decimal("50")), ("Beta", "B2", Decimal("80"))]
    # stage 2: both dataflows found the ONE mapping workbook by the file name in their M (a UNC path / a URL-encoded SharePoint
    # URL, under different query names) and ran their own cleaning steps over it (Trim; Beta also upper-cases)
    assert con.execute("select Policy, Class from df_enriched order by Policy").fetchall() == [
        ("A1", "Property"), ("A2", "Marine"), ("B1", "PROPERTY"), ("B2", "CASUALTY")]
    plan = tf.resolve(con, ids["A2"], "AcmeOut", "ACME")
    assert plan["pipeline"].references["RiskMap"] == {"kind": "binding", "table": "ref_risk_map", "auto": True, "file": "Risk Class Mapping.xlsx"}
    # merge: two linked sources, each matched to its coverholder's output
    assert con.execute("select Coverholder, Policy, Premium, Class from df_final order by Policy").fetchall() == [
        ("ACME", "A1", Decimal("100"), "Property"), ("ACME", "A2", Decimal("200"), "Marine"),
        ("Beta", "B1", Decimal("50"), "PROPERTY"), ("Beta", "B2", Decimal("80"), "CASUALTY")]
    srcs = tf.resolve(con, ids["M"], "Final", "", None)["sources"]
    assert [(s["step"], s["mode"]) for s in srcs] == [("ACME", "missing"), ("Beta", "missing")]               # without the pipeline there is nothing to match them to
    assert pl.run_pipeline(con, pid)["status"] == "ok"                        # and re-running is a no-op
    again = pl.run_pipeline(con, pid)
    assert [s["status"] for s in again["merge"]] == ["up_to_date"]


def test_changed_mapping_workbook_reruns_every_coverholders_stage_two(world):
    con, root, ids = world
    map_layouts(con, ids)
    pid = make_pipeline(con, ids)
    pl.run_pipeline(con, pid)
    mapping_workbook(root / "Reference" / "Risk Class Mapping.xlsx", prop="Commercial Property")
    run_profile(con, LocalSource(root / "Reference"), "local", str(root / "Reference"))
    rep = pl.run_pipeline(con, pid)
    assert [r["table"] for r in rep["refreshed_references"]] == ["ref_risk_map"]
    assert {ch: [s["status"] for s in c["steps"]] for ch, c in rep["coverholders"].items()} == {
        "ACME": ["up_to_date", "loaded"], "Beta": ["up_to_date", "loaded"]}
    assert rep["merge"][0]["status"] == "loaded"
    assert con.execute("select Class from df_final where Policy = 'A1'").fetchone()[0] == "Commercial Property"
    assert con.execute("select Class from df_final where Policy = 'B1'").fetchone()[0] == "COMMERCIAL PROPERTY"


def test_a_lookup_without_an_imported_file_says_exactly_what_to_do(world):
    con, root, ids = world
    map_layouts(con, ids)
    stages = [{"name": "Normalise", "kind": "files", "dataflow_id": "", "entity": "", "output_table": "df_bdx"},
              {"name": "Enrich", "kind": "stage", "dataflow_id": "", "entity": "", "output_table": "df_enriched"}]
    ovr = [{"position": 0, "coverholder": c, "dataflow_id": ids[k], "entity": e}
           for c, k, e in (("ACME", "A1", "AcmeBdx"), ("Beta", "B1", "BetaBdx"))]
    ovr += [{"position": 1, "coverholder": c, "dataflow_id": ids[k], "entity": e} for c, k, e in (("ACME", "A2", "AcmeOut"), ("Beta", "B2", "BetaOut"))]
    pid = pl.save_pipeline(con, "NoRef", stages, ovr)
    step = pl.run_pipeline(con, pid)["coverholders"]["ACME"]["steps"][1]               # the workbook was never imported as a lookup table
    assert step["status"] == "error" and "RiskMap" in step["detail"] and "Risk Class Mapping.xlsx" in step["detail"]


def test_merge_sources_that_cannot_be_matched_are_listed_and_can_be_bound_explicitly(world):
    con, root, ids = world
    map_layouts(con, ids)
    # a merge dataflow whose linked entities are named in a way that matches neither coverholder
    odd = MERGE_M.replace('entity = "AcmeOut"', 'entity = "Out_1"').replace('entity = "BetaOut"', 'entity = "Out_2"') \
        .replace("ACME =", "SrcOne =").replace("Beta =", "SrcTwo =").replace("(ACME,", "(SrcOne,").replace("(Beta,", "(SrcTwo,")
    odd_id = df.store_dataflow(con, df.parse_model_json(model("Odd merge", [(
        "Final", attrs(("Coverholder", "string"), ("Policy", "string"), ("Premium", "decimal"), ("Class", "string")), odd)])))[0]
    ids["ODD"] = odd_id
    pid = make_pipeline(con, ids, "ODD")
    rep = pl.run_pipeline(con, pid)
    merge = rep["merge"][0]
    assert merge["status"] == "error" and "SrcOne" in merge["detail"] and "bind" in merge["detail"]
    assert rep["status"] == "partial"
    # bind each linked source to the right coverholder's rows; the merge now runs
    for step, view in (("SrcOne", "df_enriched__acme"), ("SrcTwo", "df_enriched__beta")):
        con.execute("INSERT INTO query_bindings (dataflow_id, query_name, table_name) VALUES (?, ?, ?)", [odd_id, f"Final::{step}", view])
    rep = pl.run_pipeline(con, pid)
    assert rep["merge"][0]["status"] == "loaded", rep["merge"]
    assert con.execute("select count(*), count(distinct Coverholder) from df_final").fetchone() == (4, 2)


def test_coverholder_without_a_dataflow_and_unreferenced_coverholders(world):
    con, root, ids = world
    map_layouts(con, ids)
    bdx(root / "Gamma" / "g.xlsx", ["Policy No", "Gross Premium", "Risk Code", "Currency"], [["G1", 10, "MAR", "GBP"]])
    run_profile(con, LocalSource(root / "Gamma"), "local", str(root / "Gamma"), coverholder_of=coverholder_resolver("folder", "", "Gamma"))
    pid = make_pipeline(con, ids)
    rep = pl.run_pipeline(con, pid)
    gamma = rep["coverholders"]["Gamma"]["steps"][0]
    assert gamma["status"] == "error" and "No dataflow is chosen for Gamma" in gamma["detail"]
    assert rep["coverholders"]["ACME"]["ok"] and rep["coverholders"]["Beta"]["ok"] and not rep["coverholders"]["Gamma"]["ok"]
    assert rep["merge"][0]["status"] == "blocked" and "Gamma" in rep["merge"][0]["detail"]
    # Gamma uses ACME's dataflows (same layout, same steps): add overrides, then the merge reports it never reads Gamma's rows
    stages = pl.get_pipeline(con, pid)["stages"]
    ovr = pl.get_pipeline(con, pid)["overrides"] + [
        {"position": 0, "coverholder": "Gamma", "dataflow_id": ids["A1"], "entity": "AcmeBdx"},
        {"position": 1, "coverholder": "Gamma", "dataflow_id": ids["A2"], "entity": "AcmeOut"}]
    pl.save_pipeline(con, "Per coverholder", stages, ovr, pipeline_id=pid)
    rep = pl.run_pipeline(con, pid)
    assert rep["status"] == "ok", rep
    warnings = rep["merge"][0].get("warnings", [])
    assert any("not read by this dataflow" in w and "Gamma" in w for w in warnings), warnings


def test_definition_rules_for_blank_defaults(world):
    con, root, ids = world
    base = {"kind": "files", "dataflow_id": "", "entity": "", "output_table": "df_x"}
    with pytest.raises(pl.PipelineError, match="not found"):
        pl.save_pipeline(con, "x", [base])                                    # no default and no overrides: nothing to run
    ovr = [{"position": 0, "coverholder": "ACME", "dataflow_id": ids["A1"], "entity": "AcmeBdx"}]
    pid = pl.save_pipeline(con, "ok", [base], ovr)
    assert pl.get_pipeline(con, pid)["overrides"][0]["coverholder"] == "ACME"
    with pytest.raises(pl.PipelineError, match="not found"):
        pl.save_pipeline(con, "x", [{"kind": "files", "dataflow_id": ids["A1"], "entity": "AcmeBdx"},
                                    {"kind": "merge", "dataflow_id": "", "entity": "", "output_table": "df_m", "name": "m"}], ovr)  # merge needs one
