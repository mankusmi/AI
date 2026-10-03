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

## Raw file content and export

Excel bytes are stored in `file_blobs` (one row per distinct SHA-256, so duplicates cost nothing;
`files.sha256` links to it). Disable with `--no-content`; files over `--max-content-mb` (default 200) are skipped.

```bash
sp-profile export --db profile.duckdb --rel-path "2024/Jan/claims.xlsx" --dest ./restored/
```

## Privacy / running locally

Everything runs on your machine. The only network traffic is to Microsoft
(`login.microsoftonline.com` for sign-in, `graph.microsoft.com` and SharePoint download URLs for files); the code has no
telemetry and makes no calls to Anthropic or any other service. `local` mode uses no network at all.
The DuckDB file contains your file contents and the token cache holds a refresh token: keep both out of git
(`*.duckdb` is git-ignored) and treat them as confidential.

## Browser UI

```bash
sp-profile serve --db profile.duckdb      # prints http://127.0.0.1:8765/?t=<token> and opens it
```

1. **Sources** – browse folders/files on this computer (OneDrive-synced SharePoint works), tick files or profile a whole
   folder, or profile a SharePoint folder (browser sign-in). Results land in the same DuckDB.
2. **Dataflow** – import a Power BI **Gen1 dataflow** `model.json` (file dialog or picker). Entities, attributes, data types
   and the Power Query (M) source are stored in `dataflows`, `dataflow_entities`, `dataflow_attributes`.
3. **Map & load** – per detected layout, map source columns to the entity's attributes (name-similarity suggestions,
   incl. `Policy No` ≈ `PolicyNumber`, reordered words and common abbreviations). Mappings are stored per layout in
   `column_mappings`, so every file sharing that layout is covered. **Load** appends rows from the stored file bytes into a
   typed table `df_<entity>` (dataflow types → DuckDB types; day-first date parsing; Excel serial dates; `1,200.50`/`(5)` numbers).
   Unconvertible cells become NULL and are named in `_coerce_errors`. Provenance columns: `_load_id`, `_source_path`,
   `_source_sha256`, `_sheet`, `_excel_row`, `_layout_hash`, `_loaded_utc`. The same content + sheet is never loaded twice
   unless "force reload" is ticked; every sheet load is recorded in `load_log`. New attributes in a re-imported dataflow
   are added to the table with `ALTER TABLE`.
4. **Query & profile** – SQL editor over the whole database (tables/views sidebar, Ctrl+Enter, CSV download). Only
   one SELECT/DESCRIBE/SHOW/SUMMARIZE/EXPLAIN statement runs unless "allow changes" is ticked. **Profile result** gives per-column
   null %, distinct count, min/max, mean/std/quartiles, string lengths and top values for any query or table.

Security: the server listens on 127.0.0.1 only, every API call needs the per-run token and a localhost `Host` header, and
the page loads no external scripts, fonts or images.
Only Gen1 `model.json` is parsed; mapping is by column name/layout, M-query transformations are shown but not executed.
