"""Writers: CSV, XLSX (needs openpyxl), self-contained HTML, JSON."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .analysis import Analysis

SHOP_COLS = ["priority", "decision", "datapoint", "kind", "data_type", "suggested_snowflake_type", "definition",
             "also_known_as", "needed_by", "dashboards_replaced", "visuals", "also_used_by_staying", "required_via_measures",
             "snowflake_database", "snowflake_schema", "snowflake_table", "snowflake_column", "snowflake_actual_type",
             "match", "candidates", "grain", "freshness", "status", "comments"]
LOGIC_COLS = ["priority", "decision", "datapoint", "kind", "definition", "needed_by", "dashboards_replaced", "visuals",
              "also_used_by_staying", "grain", "freshness", "status", "comments"]


def _csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_all(a: Analysis, out: Path, shop: list[dict] | None = None) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    shop = a.shopping_list() if shop is None else shop
    inv, stay = a.inventory(), a.stay_only()
    inv_cols = ["dashboard", "status", "owner", "pages", "visuals", "datapoints", "notes"]
    written = []
    for name, rows, cols in (("shopping_list.csv", shop, SHOP_COLS), ("dashboard_inventory.csv", inv, inv_cols),
                             ("stay_on_legacy.csv", stay, ["datapoint", "kind", "used_by"])):
        _csv(out / name, rows, cols)
        written.append(out / name)
    (out / "analysis.json").write_text(json.dumps({"shopping_list": shop, "inventory": inv, "stay_on_legacy": stay}, indent=2))
    written.append(out / "analysis.json")
    x = _xlsx(out / "shopping_list.xlsx", shop, inv, stay, inv_cols)
    if x:
        written.append(x)
    (out / "shopping_list.html").write_text(_html(shop, inv, stay, inv_cols), encoding="utf-8")
    written.append(out / "shopping_list.html")
    return written


def _xlsx(path: Path, shop, inv, stay, inv_cols):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError:
        return None
    cols_rows = [r for r in shop if r["kind"] == "column"]
    logic_rows = [r for r in shop if r["kind"] != "column"]
    wb = Workbook()
    sheets = [("Columns to source", cols_rows, SHOP_COLS), ("Measures & logic", logic_rows, LOGIC_COLS),
              ("Dashboards", inv, inv_cols), ("Stay on legacy", stay, ["datapoint", "kind", "used_by"])]
    for i, (title, rows, cols) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c, "") for c in cols])
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="305496")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for j, c in enumerate(cols, start=1):
            width = min(60, max([len(c)] + [len(str(r.get(c, ""))) for r in rows[:200]]) + 2)
            ws.column_dimensions[ws.cell(1, j).column_letter].width = width
        if "status" in cols and rows:
            col = ws.cell(1, cols.index("status") + 1).column_letter
            dv = DataValidation(type="list", formula1='"open,requested,mapped,confirmed,not available"', allow_blank=True)
            ws.add_data_validation(dv)
            dv.add(f"{col}2:{col}{len(rows) + 1}")
    wb.save(path)
    return path


def _table(rows, cols, tid):
    head = "".join(f"<th>{html.escape(c.replace('_', ' '))}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols) + "</tr>" for r in rows)
    return f'<table id="{tid}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _html(shop, inv, stay, inv_cols):
    shared = sum(1 for r in shop if r["decision"].startswith("SHARED"))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Datapoint shopping list</title>
<style>body{{font:14px system-ui,sans-serif;margin:24px;color:#222}}table{{border-collapse:collapse;width:100%;margin:8px 0 28px}}
th,td{{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top;max-width:420px;overflow-wrap:anywhere}}
th{{background:#305496;color:#fff;position:sticky;top:0}}tr:nth-child(even){{background:#f6f8fb}}
input{{padding:6px;width:320px}}.k{{display:inline-block;margin-right:24px}}</style></head><body>
<h1>Datapoint shopping list</h1>
<p><span class="k"><b>{len(shop)}</b> datapoints to source</span><span class="k"><b>{shared}</b> shared with staying dashboards</span>
<span class="k"><b>{len(stay)}</b> stay on legacy only</span></p>
<input id="f" placeholder="Filter shopping list..." oninput="var q=this.value.toLowerCase();
document.querySelectorAll('#shop tbody tr').forEach(function(r){{r.style.display=r.textContent.toLowerCase().includes(q)?'':'none'}})">
<h2>Shopping list</h2>{_table(shop, SHOP_COLS, 'shop')}
<h2>Dashboards</h2>{_table(inv, inv_cols, 'inv')}
<h2>Datapoints only used by staying dashboards</h2>{_table(stay, ['datapoint', 'kind', 'used_by'], 'stay')}
</body></html>"""
