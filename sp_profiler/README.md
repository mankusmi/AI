# sp-profile

Profiles Excel (bordereau) files in a SharePoint folder: inventory, size/age statistics,
header detection and layout grouping.

```bash
pip install -e ".[sharepoint]"

# 1) Folder on disk (OneDrive-synced SharePoint library, or a downloaded copy)
sp-profile local --path "C:/Users/me/Contoso/Claims - Bordereaux" --out out/

# 2) Straight from SharePoint: opens your browser to sign in (token cached in ~/.sp_profiler)
sp-profile graph --site-url https://contoso.sharepoint.com/sites/Claims \
  --library Documents --folder "Bordereaux/2024" --db profile.duckdb
sp-profile graph --site-url ... --logout     # forget the cached sign-in
```

Files are streamed to a temp dir and deleted. The default client is Microsoft's public
"Graph Command Line Tools" app; if your tenant blocks it, register a public-client app
(redirect URI `http://localhost`, delegated `Files.Read.All` + `Sites.Read.All`) and pass `--client-id`.

Results go to DuckDB (`--db`, default `sp_profile.duckdb`), appended per run (`run_id`):
`runs`, `files` (paths, `path_segments`, sizes, created/modified, SharePoint item/drive ids, etags, hashes,
status, layout), `sheets`, `sheet_headers` (one row per header cell), `layouts`; views `v_files`, `v_layouts`,
`v_file_layouts`, `v_duplicates` show the latest run.

```sql
SELECT layout, family, files, columns FROM v_layouts ORDER BY files DESC;
SELECT path_segments[1] AS top_folder, count(*), sum(size_bytes) FROM v_files GROUP BY 1;
```

Optional `--out DIR` also writes CSV/JSON/HTML exports:
`file_inventory.csv`, `sheet_headers.csv`, `layouts.csv`,
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
