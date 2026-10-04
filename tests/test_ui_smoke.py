"""Drives the real UI in Chromium: profile -> import dataflow -> map -> load -> query -> profile -> export.

Skipped when Playwright or a browser is not available. Set SP_CHROMIUM to a Chromium/Chrome executable to use one
that Playwright did not install itself.
"""
import datetime
import json
import os
import threading
from http.server import ThreadingHTTPServer

import pytest

from sp_profiler.webapp import App, make_handler

from . import test_m_transform as T

pytestmark = pytest.mark.browser


def test_full_flow_in_a_browser(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    data = tmp_path / "data"
    data.mkdir()
    cols = ["Policy No", "Insured Name", "Region", "Gross Premium", "Inception Date", "Units", "Currency"]
    T.make_book(data / "a.xlsx", cols, [["P1", " <b>acme</b> ", "North", 1200.5, datetime.datetime(2024, 1, 5), 3, "GBP"],
                                        ["P2", "beta", None, "1,000", "06/02/2024", 2, "EUR"]])
    T.make_book(data / "b.xlsx", ["PolicyNumber", "Insured", "Region", "Premium (Gross)", "InceptionDate", "Units", "Ccy"],
                [["P3", "gamma", "South", 50, datetime.datetime(2023, 12, 1), 1, "USD"]])
    (data / "model.json").write_text(json.dumps(T.MODEL))
    app = App(str(tmp_path / "ui.duckdb"), home=str(data))
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
            pg = browser.new_page(viewport={"width": 1300, "height": 1000})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/?t={app.launch_token}")
            pg.wait_for_selector("#fslist li")
            assert "/?t=" not in pg.url                                   # the launch token is removed from the address bar
            # 1. profile the folder
            pg.fill("#fspath", str(data))
            pg.click("#fsgo")
            pg.click("#pfolder")
            pg.wait_for_selector("#pres .ok, #pres .warn, #pres .bad", timeout=20000)
            assert "2 Excel files inspected" in pg.inner_text("#pres")
            # 2. import the dataflow with the file dialog
            pg.click("nav button[data-t=df]")
            pg.set_input_files("#dffile", str(data / "model.json"))
            pg.wait_for_selector("#dfmsg .ok")
            # 3. map and load
            pg.click("nav button[data-t=map]")
            pg.select_option("#ment", "Policies")
            pg.wait_for_selector("#mtransform h2:has-text('complete')")
            pg.wait_for_selector("#mlayouts details")
            for btn in pg.query_selector_all("#mlayouts button.b.p"):
                btn.click()
                pg.wait_for_timeout(150)
            pg.click("#mtransform button:has-text('Preview final output')")
            pg.wait_for_selector("#mtransform :text('Final output')", timeout=10000)
            pg.click("#mload")
            pg.wait_for_selector("#lres :text('Loaded 3 rows')", timeout=20000)
            # 4. query: values are shown as text (no HTML injection) and writes are refused by default
            pg.click("nav button[data-t=sql]")
            pg.fill("#sqltext", "SELECT PolicyNumber, Insured, Year, GrossGBP FROM df_policies ORDER BY 1")
            pg.click("#run")
            pg.wait_for_selector("#result table")
            assert "P3" in pg.inner_text("#result") and pg.query_selector("#result b") is None
            pg.fill("#sqltext", "DROP TABLE df_policies")
            pg.click("#run")
            pg.wait_for_selector("#sqlmsg.bad")
            pg.fill("#sqltext", "SELECT * FROM df_policies")
            pg.click("#prof")
            pg.wait_for_selector("#profile table")
            # 5. export for Databricks
            pg.click("summary:has-text('Export for Databricks')")
            pg.fill("#xout", str(tmp_path / "dbx"))
            pg.click("#xgo")
            pg.wait_for_selector("#xmsg.ok", timeout=20000)
            browser.close()
    finally:
        srv.shutdown()
    assert errors == []
    assert any((tmp_path / "dbx").glob("*/databricks_load.py"))
