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
SELECT layout, files, sheets, columns, example FROM v_layouts ORDER BY files DESC;
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
sp-profile serve --db profile.duckdb      # prints a one-time link (http://127.0.0.1:8765/?t=...) and opens it
```

1. **Sources** – browse folders/files on this computer (OneDrive-synced SharePoint works), tick files or profile a whole
   folder, or profile a SharePoint folder (browser sign-in). Results land in the same DuckDB.
2. **Dataflow** – import a Power BI **Gen1 dataflow** `model.json` (file dialog or picker). Entities, attributes, data types
   and the Power Query (M) source are stored in `dataflows`, `dataflow_entities`, `dataflow_attributes`.
3. **Map & load** – per detected layout, map source columns to the entity's attributes (name-similarity suggestions,
   incl. `Policy No` ≈ `PolicyNumber`, reordered words and common abbreviations). Mappings are stored per layout in
   `column_mappings`, so every file sharing that layout is covered. **Load** appends rows from the stored file bytes into a
   typed table `df_<entity>` (dataflow types → DuckDB types; day-first date parsing; Excel serial dates; `1,200.50`/`(5)` numbers).
   Unconvertible cells become NULL and are named in `_coerce_errors`. Text dates follow the dataflow's `culture` (en-US reads `06/02/2024` as 2 June, most others as 6 February); override it per entity on the panel. Dates that read validly either way are counted and reported after each load. Provenance columns: `_load_id`, `_source_path`,
   `_source_sha256`, `_sheet`, `_excel_row`, `_layout_hash`, `_loaded_utc`. The same content + sheet is never loaded twice
   unless "force reload" is ticked, which *replaces* that sheet's earlier rows (no duplicates); every sheet load is recorded in `load_log`. New attributes in a re-imported dataflow
   are added to the table with `ALTER TABLE`.
4. **Query & profile** – SQL editor over the whole database (tables/views sidebar, Ctrl+Enter, CSV download). Only
   one SELECT/DESCRIBE/SHOW/SUMMARIZE/EXPLAIN statement runs unless "allow changes" is ticked. **Profile result** gives per-column
   null %, distinct count, min/max, mean/std/quartiles, string lengths and top values for any query or table.

Security: the server listens on 127.0.0.1 only, every API call needs the per-run token and a localhost `Host` header, and
the page loads no external scripts, fonts or images.
Only Gen1 `model.json` is parsed; mapping is by column name/layout, M-query transformations are shown but not executed.

## Power Query transformations and lookups

Gen1 dataflow entities usually carry M steps (renames, type changes, filters, computed columns, lookups). On the
**Map & load** tab the entity's M query is translated into a DuckDB SQL pipeline (one CTE per step) that runs over each
sheet; the result is converted to the entity's attribute types and appended to `df_<entity>`.

* **Input columns:** the columns the M steps read (e.g. `Policy No`, `Currency`). You map each layout's sheet columns to these
  (suggestions use name similarity + abbreviations such as `No`≈`Number`, `Ccy`≈`Currency`), so one set of steps serves every layout.
* **Supported steps:** PromoteHeaders, RenameColumns, RemoveColumns, SelectColumns, ReorderColumns, TransformColumnTypes,
  TransformColumns, AddColumn, ReplaceValue, SelectRows, Distinct, Sort, FirstN, Skip/RemoveFirstN, FillDown, Combine,
  NestedJoin + ExpandTableColumn, Join, `#table`/FromRows; expressions with `if/then/else`, `and/or/not`, `&`, arithmetic and common
  `Text.*`, `Number.*`, `Date.*`, `List.Contains` functions. Everything before the header promotion (Excel.Workbook, navigation,
  title-row skips) is replaced by the staged sheet.
* **Lookups:** a referenced query is resolved from an inline `#table`, an explicit **binding** to a DuckDB table, or an already-loaded
  entity table. Lookups that read an external file are reported as `missing`; import the file as a table (CSV/Excel, button on the panel)
  and bind it.
* **Nothing is skipped silently.** Steps that cannot be translated are listed with the reason and the entity is *blocked* until you
  (a) write a SQL override (a `SELECT` over staged table `_stg`: input columns as VARCHAR plus `__row`), or (b) tick "accept partial
  translation" (untranslated steps are then NOT applied). Per-step and final-output previews run on a sample of a stored file.
* **Not supported (flagged):** Table.Group/Pivot/Unpivot/SplitColumn/CombineColumns, custom M functions, `try/otherwise`,
  fuzzy joins, parameters, merges against external sources. Type conversions follow the loader: day-first dates, `1,200.50`, `(5)`.
* The conversion helpers are SQL macros `sp_dbl/sp_dec/sp_int/sp_ts/sp_bool/...` stored in the database, so they also work in the SQL console.

## Reliability notes

* Each sheet loads in its own transaction (rows + `load_log` entry, or nothing); a failed sheet is logged with its error and
  retried on the next load. Loads of the same entity are serialised, so two clicks cannot double-load.
* Rows are written through a temporary CSV (`INSERT ... SELECT FROM read_csv`), ~250x faster than row-by-row inserts; the temp file
  holds your data for the duration of the insert and is deleted immediately afterwards.
* Power Query semantics worth knowing: `Number.Round` defaults to round-half-to-even (as in Power Query); once a step types a
  column, later comparisons and arithmetic use that type (so `[a] = [b]` on `1.0` and `1` is true).
* SharePoint calls retry on throttling (429/5xx, honouring `Retry-After`) and network errors, and refresh expired download links.

## Loading into Databricks

```bash
sp-profile export-databricks --db profile.duckdb --out C:/exports --catalog main --schema bordereaux \
  --volume-path /Volumes/main/bordereaux/landing/sp_profiler          # also: Query tab > "Export for Databricks"
```
creates `C:/exports/<export_id>/` with one folder of Parquet files per table, `manifest.json`, `databricks_load.py` (notebook) and
`databricks_load.sql`. Then:

1. Upload the `<export_id>` folder to a Unity Catalog **volume** (Catalog > volume > Upload, or `databricks fs cp -r <folder> dbfs:/Volumes/...`),
   or to ADLS/S3 and use that path instead.
2. Import `databricks_load.py` as a notebook, set the widgets (source path, catalog, schema) and run all.
   It creates/updates Delta tables `df_<entity>` plus `files`, `sheets`, `sheet_headers`, `layouts`, `load_log`, `runs`,
   `dataflow_attributes`, `column_mappings`. (`databricks_load.sql` does the same with `CREATE TABLE` + `COPY INTO` if you prefer SQL.)

Details: column names are made Delta-safe (spaces/`,;{}()=` become `_`; `_load_id` etc. are kept), naive timestamps are exported as UTC
instants (Spark `TIMESTAMP`), TIME/JSON become strings, decimals/arrays/booleans map directly, and file names never start with `_`
(Spark ignores those). `--tables` picks tables, `--prefix` prefixes the Databricks table names, `--include-blobs` also exports the
original Excel bytes (`file_blobs`, large).

**Incremental:** `--incremental` exports only the entity-table sheets loaded since the previous export (tracked in `export_log`).
The notebook then deletes those sheets from the target (matching `_source_sha256` + `_sheet`) and appends the new rows, so
reloaded sheets replace rather than duplicate and re-running the notebook is safe. The mode is fixed per export (a full export
overwrites, an incremental one merges). Metadata tables are always exported in full and overwritten. Rows you delete by hand in DuckDB are
not propagated to Databricks; do a full export for that.

## Large runs, re-runs and views

* **Progress is saved as it goes.** A run is written to DuckDB in batches, so a crash or Ctrl+C at file 900 of 1000 keeps those 900
  (`runs.status` is `running`/`failed`/`complete`). Re-running reuses them.
* **Unchanged files are not opened again.** A file is reused when its identity (SharePoint item id, else path), size and eTag
  (SharePoint) or modified time (local) match an earlier run made with the same settings, and its bytes are still stored. Nothing is
  downloaded for it. `--refresh` re-inspects everything; changed files are always re-inspected; failed ones are retried.
* **Parallel:** `--workers N` (default 4 for SharePoint, 1 for local) inspects files concurrently.
* **Views show the latest known state of every file**, not just the last run: `v_files` (one row per file: SharePoint item id, else
  path), `v_sheets`, `v_layouts`, `v_file_layouts`, `v_duplicates`; `v_files_last_run` is the old "last run only" view.
* **File bytes** can live in a folder instead of inside DuckDB: `--blob-dir D:/bdx_files` (stored by hash, so duplicates are free).
* **Changed files replace their rows.** If a file at the same location changes, loading it replaces the rows loaded from its
  previous version (matched on `_source_id` + sheet) instead of adding a second copy; the Databricks notebook does the same.
* **Header detection and warnings:** years/dates count as header cells (`2023 | 2024 | 2025`), hidden sheets are skipped unless
  `--include-hidden`, `--exact-rows` counts rows exactly, and files whose formulas were never calculated (saved by a script, no cached
  values) get a warning in `files.warnings` because those cells would load as empty.
* **Table names are registered** (`entity_tables`) so entities like `A B` and `A-B` never share a table; if the dataflow changes an
  attribute's type the column is converted in place (strict cast, with a clear error if values do not fit). Re-importing a dataflow with
  the same name carries lookup bindings and entity settings over to the new version.

## Power Query: more steps

Also supported now: `Table.Group` (global; `List.Sum/Max/Min/Average/Count`, `Table.RowCount`, distinct counts), `Table.Unpivot` and
`Table.UnpivotOtherColumns` (the other sheet columns are staged under their own header text), `Table.SplitColumn`
(`Splitter.SplitTextByDelimiter`), `Table.CombineColumns` (`Combiner.CombineTextByDelimiter`), parameter queries
(`shared Year = 2024 meta [IsParameterQuery = true]` become constants), `x is null`, and a tokenizer-based query splitter that copes with
attributes, comments and `;`/`shared` inside strings.

## Security notes

* The launch link works **once** (then a `HttpOnly`, `SameSite=Strict` session cookie is used) and is removed from the address bar, so the
  token does not stay in browser history. Requests over 64 MB are refused; finished jobs are cleaned up; if the port is busy the next free
  one is used.
* In read-only mode the SQL console, CSV download and profiling refuse file/environment functions (`read_csv`, `read_blob`, `glob`,
  `getenv`, `query`, `FROM 'file.csv'` ...). This is best-effort protection for a single-user local tool, not a sandbox; ticking
  "allow changes" lifts it. (The CSV download previously ran any SQL regardless of that setting; fixed.)
* The SharePoint sign-in is cached **encrypted by the OS** (Windows DPAPI / macOS Keychain / Linux libsecret) when `msal-extensions` can
  use it; otherwise a plain file restricted to your user, with a warning. `sp-profile graph --logout` removes both.

## Development

`pip install -e ".[dev]"`, then `ruff check sp_profiler ...` and `pytest` (the browser test runs when Playwright and Chromium are
installed: `playwright install chromium`, or set `SP_CHROMIUM`). CI runs both on Linux and Windows (`.github/workflows/tests.yml`).

## Pipelines: coverholder files → dataflow 1 → dataflow 2 → merge

For the multi-stage flow (a folder per coverholder, normalise with one dataflow, enrich with a second that uses a mapping workbook,
then merge everything with a final dataflow) use the **5 · Pipeline** tab, or `sp-profile pipeline-run --db profile.duckdb --name "Bordereaux"`.

**Set it up once**

1. **Profile each coverholder's folder** (Sources tab). "This folder is one coverholder" labels every file with the folder name
   (or choose "each subfolder is one" for a parent folder, or type a name). Keep file bytes on. Profile the folder holding the mapping workbook with
   "none" so it is not treated as a coverholder.
2. **Import the three dataflow JSON files** (Dataflow tab).
3. **Dataflow 1** (Map & load tab): map each layout's columns onto the inputs its steps read, as before. Layouts that are not bordereaux
   (e.g. the mapping workbook if it sits in a coverholder folder) can be ticked "not a bordereau (ignore)".
4. **Dataflow 2**: it reads dataflow 1's output, so there are no sheet columns to map. Its mapping workbook is a lookup query: import the
   workbook you already profiled (Lookups → "Import a lookup workbook you already profiled", name the sheet) and **Bind** the lookup to the
   table. Binding can be for all coverholders or one coverholder (per-coverholder mapping files). The table **refreshes automatically** when the
   workbook changes and is re-profiled.
5. **Pipeline tab**: stage 1 = files → dataflow 1, add a stage for dataflow 2 and a merge stage for the final dataflow, name the output tables, Save.

**Run it** (button, or `pipeline-run`): for each coverholder, stage 1 then stage 2; when all are done, the merge stage runs once.

* Stage 1 loads that coverholder's new/changed sheets into one table with a `_coverholder` column (a changed file replaces its old rows).
* Stage 2 applies dataflow 2 to that coverholder's stage-1 rows; its source is the previous stage's table (columns are checked, and a
  clear message lists missing ones). The coverholder's rows in the stage-2 table are replaced.
* The merge stage applies the final dataflow to all coverholders' stage-2 rows and **replaces** the final table. Inside it, `_coverholder`
  is available (e.g. rename it to `Coverholder`). A coverholder that is not ready blocks the merge unless you tick "merge even if some coverholders are not ready".
* **Only affected work is redone.** A new file for one coverholder reruns that coverholder's stages and the merge; a changed mapping
  workbook reruns stage 2 for everyone and the merge; nothing changed = everything "up to date". "Redo everything" forces it.
* **Problems stay local.** A coverholder with an unmapped layout (status *needs mapping*, with the file named) or an error stops at that stage,
  its later stages show *blocked*, and the other coverholders carry on. The status grid shows every coverholder × stage; click a cell for details.
* A different dataflow can be used for one coverholder at any stage ("Different dataflow for one coverholder"); the stage's output table stays the same.
* Per-coverholder views `<output table>__<coverholder>` are created so a lookup or merge query can be bound to one coverholder's rows.
* Export for Databricks includes every pipeline output (entity tables incrementally, derived tables as full snapshots) plus the run history.

**Things to know**

* A dataflow that reads another dataflow (linked entity) gets that stage's table as its source. If one query reads several linked entities, all of
  them receive the same input (the stage reports a warning); bind extra inputs as lookups to the `__<coverholder>` views instead.
* Stage 1 mappings are stored per layout and per entity name; give the dataflow-1 entity a name that is not used by a stage-1 entity in another dataflow.

### Different dataflows per coverholder, one shared mapping workbook

* Leave a stage's default dataflow as "— default: none (per coverholder) —" and choose dataflow 1 / dataflow 2 for each coverholder in the
  coverholder table. All coverholders still write to the stage's one output table (partitioned by `_coverholder`). A coverholder with no
  dataflow at a stage gets an explicit error; coverholders a merge dataflow does not read are listed as a warning. "Match by name" fills
  the table by matching dataflow/entity names to coverholder names.
* A shared lookup workbook (e.g. risk code → class) is imported once. Every coverholder's dataflow 2 that reads it is **matched by file name**,
  even through different UNC/URL paths and query names; the lookup query's own steps (Trim, Upper, ...) run over it first.
* Untouched attributes of an open schema pass through from the sheet unchanged.
* The merge dataflow's linked sources (one per coverholder's dataflow-2 entity) are matched automatically to that coverholder's output view
  `<output>__<coverholder>` by entity name, then by coverholder name. Anything that cannot be matched is shown on the Map & load tab
  ("Sources this dataflow reads"); **Bind** it explicitly (`Query::Step`).
* Limit: stage 1 entities that share a name across dataflows share layout mappings; give them distinct names.
