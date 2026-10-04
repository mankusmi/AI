"""Per-coverholder dataflows, chosen in the UI table, with a merge over each coverholder's output (skipped without a browser)."""
import os
import threading
from http.server import ThreadingHTTPServer

import pytest

from sp_profiler import transforms as tf
from sp_profiler.webapp import App, make_handler

from .test_pipeline_per_coverholder import map_layouts, world  # noqa: F401  (fixture)

pytestmark = pytest.mark.browser


def test_per_coverholder_pipeline_in_a_browser(world, tmp_path):  # noqa: F811
    sync_api = pytest.importorskip("playwright.sync_api")
    con, root, ids = world
    map_layouts(con, ids)
    tf.import_reference_stored(con, con.execute("select sha256 from v_files where name = 'Risk Class Mapping.xlsx'").fetchone()[0],
                               "ref_risk_map", "Map")
    db = con.execute("PRAGMA database_list").fetchall()[0][2]
    con.close()
    app = App(db, home=str(root))
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
            pg = browser.new_page(viewport={"width": 1500, "height": 1200})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/?t={app.launch_token}")
            pg.click("nav button[data-t=pipe]")
            pg.wait_for_selector("#chlist table")
            pg.fill("#pname", "Per coverholder")
            # stage 1 and 2 have no default dataflow: each coverholder has its own
            pg.locator("#pstages > div").nth(0).locator("input").nth(0).fill("Normalise")
            pg.locator("#pstages > div").nth(0).locator("input[placeholder^='output']").fill("df_bdx")
            pg.click("#padd")
            row2 = pg.locator("#pstages > div").nth(1)
            row2.locator("input").nth(0).fill("Enrich")
            row2.locator("input[placeholder^='output']").fill("df_enriched")
            pg.select_option("#paddkind", "merge")
            pg.click("#padd")
            row3 = pg.locator("#pstages > div").nth(2)
            row3.locator("select").nth(0).select_option(ids["M"])
            pg.wait_for_timeout(100)
            row3 = pg.locator("#pstages > div").nth(2)
            row3.locator("input").nth(0).fill("Merge")
            row3.locator("input[placeholder^='output']").fill("df_final")
            # dataflows are named "<coverholder> <stage>": one click matches all four
            pg.click("#pmatch")
            pg.wait_for_selector("#pmatchmsg:has-text('Matched 4')")
            matrix = pg.inner_text("#povr")
            assert "ACME Normalise" in matrix and "Beta Enrich" in matrix
            pg.click("#psave")
            pg.wait_for_selector("#pmsg2.ok", timeout=5000)
            pg.click("#prun")
            pg.wait_for_selector("#prmsg:has-text('Run ok')", timeout=40000)
            chips = [c.inner_text() for c in pg.query_selector_all("#pstatus .chip")]
            assert len(chips) == 5 and all(c.startswith("loaded") for c in chips), chips

            # the Map & load tab shows how each dataflow's sources were satisfied
            pg.click("nav button[data-t=map]")
            pg.select_option("#mdf", ids["A2"])
            pg.select_option("#ment", "AcmeOut")
            pg.wait_for_selector("#mtransform :text('matched by file name')")           # the shared mapping workbook, found by name
            pg.select_option("#mdf", ids["M"])
            pg.select_option("#ment", "Final")
            pg.wait_for_selector("#mtransform :text('Sources this dataflow reads')")
            sources = pg.inner_text("#mtransform")
            assert "matched automatically" in sources and "df_enriched__acme" in sources and "df_enriched__beta" in sources
            browser.close()
    finally:
        srv.shutdown()
    assert errors == []
    c = app.cur()
    assert c.execute("select count(*), count(distinct Coverholder) from df_final").fetchone() == (4, 2)
    assert c.execute("select Class from df_final where Policy = 'B2'").fetchone()[0] == "CASUALTY"
