# pbi-migrate

Turns legacy Power BI dashboards into a **shopping list of datapoints** to request from a replacement system.

```bash
pbi-migrate scan path/to/projects -o dashboards.csv   # finds every *.Report + its *.SemanticModel (PBIP)
# or: pbi-migrate init dashboards.csv                 # blank template
# edit it: one row per dashboard, status = stay | replace | retire
pbi-migrate build -m dashboards.csv -o out [--aliases aliases.csv]
```

`scan` reads each report's `definition.pbir` to find its model, writes the manifest with `status` blank for you to fill in, and warns about
reports it skips (live-connected to a published model, or no TMDL/BIM next to it). `report.json` (older PBIP report format) is picked up as `legacy-layout`; that path is untested on real files.

Manifest columns: `dashboard, status, report_source (pbir|legacy-layout), report_path, model_source (tmdl|bim), model_path, owner, notes`.
Relative paths resolve against the manifest's folder.

How it works: every visual's fields are collected; measures are followed through their DAX to the columns underneath
(best-effort regex). A datapoint used by any `replace` dashboard goes on the list. If a `stay` dashboard also uses it, it is
flagged `SHARED - decide source`. `retire` dashboards create no demand. Datapoints used only by `stay` dashboards go to `stay_on_legacy.csv`.
Priority: High = 3+ replaced dashboards, Medium = 2, Low = 1.

`--aliases`: CSV `datapoint,canonical` (e.g. `Sales[Net Rev],Sales[Revenue]`) merges differently named datapoints into one row.

**Snowflake pre-match (optional).** Export the catalog once and pass it in:

```sql
SELECT table_catalog, table_schema, table_name, column_name, data_type
FROM <db>.information_schema.columns WHERE table_schema <> 'INFORMATION_SCHEMA';
```
```bash
pbi-migrate build -m dashboards.csv -o out --snowflake-columns columns.csv
```
Columns are matched by normalised name; the table name breaks ties. `match` is `exact`, `column only`, `ambiguous (n)` (see `candidates`) or `none`.
These are suggestions to verify, not mappings.

Output: `shopping_list.xlsx` (sheets: *Columns to source*, *Measures & logic*, *Dashboards*, *Stay on legacy*; `status` has a dropdown), plus
`.csv`/`.html`. Columns carry a suggested Snowflake type; measures and calculated columns are marked `recreate logic` (rebuild in the
repointed model or a Snowflake view) and their underlying columns appear on the columns sheet. grain/freshness are left for you,
`dashboard_inventory.csv`, `stay_on_legacy.csv`, `analysis.json`.

Limits: report-level/page filters and slicer selections are not captured; hidden visuals are counted.
