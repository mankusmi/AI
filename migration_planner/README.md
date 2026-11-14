# pbi-migrate

Turns legacy Power BI dashboards into a **shopping list of datapoints** to request from a replacement system.

```bash
pbi-migrate init dashboards.csv          # template manifest
# edit it: one row per dashboard, status = stay | replace | retire
pbi-migrate build -m dashboards.csv -o out [--aliases aliases.csv]
```

Manifest columns: `dashboard, status, report_source (pbir|legacy-layout), report_path, model_source (tmdl|bim), model_path, owner, notes`.
Relative paths resolve against the manifest's folder.

How it works: every visual's fields are collected; measures are followed through their DAX to the columns underneath
(best-effort regex). A datapoint used by any `replace` dashboard goes on the list. If a `stay` dashboard also uses it, it is
flagged `SHARED - decide source`. `retire` dashboards create no demand. Datapoints used only by `stay` dashboards go to `stay_on_legacy.csv`.
Priority: High = 3+ replaced dashboards, Medium = 2, Low = 1.

`--aliases`: CSV `datapoint,canonical` (e.g. `Sales[Net Rev],Sales[Revenue]`) merges differently named datapoints into one row.

Output: `shopping_list.xlsx/.csv/.html` (send to the vendor; they fill `new_system_field`, `status`, `comments`; grain/freshness are left for you),
`dashboard_inventory.csv`, `stay_on_legacy.csv`, `analysis.json`.

Limits: report-level/page filters and slicer selections are not captured; hidden visuals are counted.
