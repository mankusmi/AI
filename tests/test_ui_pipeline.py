"""The whole multi-stage flow through the real UI (skipped without Playwright/Chromium; see test_ui_smoke)."""
import os
import threading
from http.server import ThreadingHTTPServer

import pytest

from sp_profiler.webapp import App, make_handler

from . import test_pipeline as TP

pytestmark = pytest.mark.browser


def test_pipeline_flow_in_a_browser(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    root = tmp_path / "share"
    TP.bdx(root / "ACME" / "jan.xlsx", TP.ACME_H, [["A1", 100, "GBP", TP.D(2024, 1, 5)], ["A2", 200, "EUR", TP.D(2024, 1, 6)]])
    # same headers, different column order: a second layout that the name-similarity suggestions map automatically
    TP.bdx(root / "Beta" / "q1.xlsx", ["Currency", "Policy No", "Inception", "Gross Premium"],
           [["USD", "B1", TP.D(2024, 2, 1), 50], ["GBP", "B2", TP.D(2024, 2, 2), 80]])
    TP.rates(root / "Reference" / "fx_mapping.xlsx")
    models = tmp_path / "models"
    models.mkdir()
    for n, m in (("df1", TP.DF1), ("df2", TP.DF2), ("df3", TP.DF3)):
        (models / f"{n}.json").write_text(m)
    app = App(str(tmp_path / "ui.duckdb"), home=str(root))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(app, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    errors = []
    try:
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(executable_path=os.environ.get("SP_CHROMIUM") or None, args=["--no-sandbox"])
            except Exception as e:
                pytest.skip(f"no usable browser: {str(e)[:120]}")
            pg = browser.new_page(viewport={"width": 1400, "height": 1100})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/?t={app.launch_token}")
            pg.wait_for_selector("#fslist li")

            # 1. profile each coverholder folder (one coverholder per folder), then the lookup folder with no coverholder
            for folder, mode in (("ACME", "folder"), ("Beta", "folder"), ("Reference", "none")):
                pg.fill("#fspath", str(root / folder))
                pg.click("#fsgo")
                pg.check(f"input[name=chmode][value={mode}]")
                pg.click("#pfolder")
                pg.wait_for_selector("#pres .ok, #pres .warn, #pres .bad", timeout=20000)
                assert "1 Excel files inspected" in pg.inner_text("#pres"), pg.inner_text("#pres")
                pg.evaluate("document.querySelector('#pres').replaceChildren()")

            # 2. import the three dataflows
            pg.click("nav button[data-t=df]")
            for n in ("df1", "df2", "df3"):
                pg.evaluate("document.querySelector('#dfmsg').replaceChildren()")
                pg.set_input_files("#dffile", str(models / f"{n}.json"))
                pg.wait_for_selector("#dfmsg .ok")
            ids = {r[0]: r[1] for r in app.cur().execute("select name, dataflow_id from dataflows").fetchall()}

            # 3. dataflow 1: map both coverholders' layouts (name-similarity suggestions are exact here)
            pg.click("nav button[data-t=map]")
            pg.select_option("#mdf", ids["Normalise"])
            pg.wait_for_selector("#mlayouts details")
            pg.select_option("#ment", "Bordereau")
            pg.wait_for_selector("#mtransform h2:has-text('complete')")
            pg.wait_for_selector("#mlayouts details")
            assert "ACME" in pg.inner_text("#mlayouts") and "Beta" in pg.inner_text("#mlayouts")      # layouts show their coverholder
            for btn in pg.query_selector_all("#mlayouts button.b.p"):
                if btn.is_visible():
                    btn.click()
                    pg.wait_for_timeout(150)

            # 4. dataflow 2: it reads dataflow 1's output (no layouts to map) and needs the mapping workbook as a lookup
            pg.select_option("#mdf", ids["Enrich"])
            pg.select_option("#ment", "Enriched")
            pg.wait_for_selector("#mlayouts :text('Reads another dataflow')", timeout=5000)      # stage 2 has no sheet layouts to map
            pg.wait_for_selector("#mtransform :text('Lookups')")
            pg.locator("#mtransform select").filter(has_text="fx_mapping").first.select_option(index=0)   # the stored-file picker
            pg.fill("#mtransform input[placeholder^='sheet']", "Rates")
            pg.click("#mtransform button:has-text('Import')>>nth=0")
            pg.wait_for_selector("#mtransform :text('Created table ref_fx_mapping')")
            pg.locator("#mtransform div:has(> b:text-is('FxMap')) select").nth(0).select_option("ref_fx_mapping")
            pg.click("#mtransform button:has-text('Bind')")
            pg.wait_for_selector("#mtransform h2:has-text('complete')", timeout=10000)

            # 5. the pipeline: files -> dataflow 1, stage -> dataflow 2, merge -> dataflow 3
            pg.click("nav button[data-t=pipe]")
            pg.wait_for_selector("#chlist table")
            assert "ACME" in pg.inner_text("#chlist") and "Beta" in pg.inner_text("#chlist")
            pg.fill("#pname", "Bordereaux")
            pg.click("#padd")                                          # stage 2
            pg.select_option("#paddkind", "merge")
            pg.click("#padd")                                          # stage 3 (merge)
            rows = pg.locator("#pstages > div")
            assert rows.count() == 3
            for i, (df, ent) in enumerate((("Normalise", "Bordereau"), ("Enrich", "Enriched"), ("Merge", "Final"))):
                pg.locator("#pstages > div").nth(i).locator("select").nth(0).select_option(ids[df])     # dataflow (re-renders the row)
                pg.wait_for_timeout(100)
                row = pg.locator("#pstages > div").nth(i)
                row.locator("select").nth(1).select_option(ent)                                        # entity of that dataflow
                row.locator("input[placeholder^='output']").fill(["df_bordereau", "df_enriched", "df_final"][i])
            pg.click("#psave")
            pg.wait_for_selector("#pmsg2.ok", timeout=5000)

            pg.click("#prun")
            pg.wait_for_selector("#pstatus .chip", timeout=30000)
            pg.wait_for_selector("#prmsg:has-text('Run ok')", timeout=30000)
            chips = [c.inner_text() for c in pg.query_selector_all("#pstatus .chip")]
            assert chips and all(c.startswith(("loaded", "up to date")) for c in chips), chips
            assert len(chips) == 5                                      # 2 coverholders x 2 stages + merge

            if os.environ.get("SP_SHOTS"):                              # optional: save a picture of the Pipeline tab
                pg.screenshot(path=os.path.join(os.environ["SP_SHOTS"], "pipeline.png"), full_page=True)
            # 6. final output through the query tab
            pg.click("#pstatus button:has-text('df_final')")
            pg.wait_for_selector("#result table")
            text = pg.inner_text("#result")
            assert "ACME" in text and "Beta" in text and "A2" in text and "B1" in text
            browser.close()
    finally:
        srv.shutdown()
    assert errors == []
    con = app.cur()
    assert con.execute("select count(*) from df_final").fetchone()[0] == 4
    assert con.execute("select PremiumGBP from df_final where Policy = 'A2'").fetchone()[0] == 170
