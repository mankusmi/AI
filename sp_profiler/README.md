# sp-profile

Profiles Excel (bordereau) files in a SharePoint folder: inventory, size/age statistics,
header detection and layout grouping.

```bash
pip install -e ".[sharepoint]"

# 1) Folder on disk (OneDrive-synced SharePoint library, or a downloaded copy)
sp-profile local --path "C:/Users/me/Contoso/Claims - Bordereaux" --out out/

# 2) Straight from SharePoint via Microsoft Graph (files are streamed to a temp dir and deleted)
sp-profile graph --site-url https://contoso.sharepoint.com/sites/Claims \
  --library Documents --folder "Bordereaux/2024" \
  --tenant-id <guid> --client-id <guid> [--client-secret <s>]   # no secret -> device-code login
```

Outputs in `--out`: `file_inventory.csv`, `sheet_headers.csv`, `layouts.csv`,
`header_frequency.csv`, `summary.json`, `report.html`.

* **Header row** = first row (within `--scan-rows`, default 50) with the most unique text cells
  (≥ `--min-headers`), so title/summary rows above the table are skipped.
* **Layout** = ordered, normalised header list (case/punctuation/spacing-insensitive) of a sheet.
  Same set in a different order is a distinct layout but the same "header set".
* **Family** = layouts with ≥ 80% header overlap (greedy; the most common layout seeds each family);
  `layouts.csv` shows columns added/removed/reordered versus the family seed.
* A file's *primary layout* is that of its sheet with the most data rows; files holding several layouts are counted.
* `.xls`/`.xlsb` are inventoried but not opened; corrupt/encrypted files are flagged, not fatal.
* Data row counts come from sheet dimensions (fast, can overcount formatted-but-empty rows).
