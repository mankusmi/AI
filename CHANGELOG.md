# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added — `pbi-profiler`: Power BI semantic model profiling tool

A new Python package (`pbi_profiler/`) plus `pbi-profile` CLI for profiling
Power BI / Analysis Services semantic models.

**Model loading** — three interchangeable loaders producing one
backend-agnostic `Model` (tables, columns, measures, relationships,
hierarchies, RLS roles):
- `TmdlModelLoader` — parses a PBIP `<Name>.SemanticModel/definition` folder
  (Power BI Desktop's TMDL project format) via a new generic,
  indentation-based TMDL tree parser (`loaders/tmdl_parser.py`).
- `BimModelLoader` — parses a legacy `Model.bim` / TMSL JSON file.
- `LiveModelLoader` — reads a published dataset over the Power BI REST API
  (`executeQueries`), using DAX `INFO.VIEW.*` dynamic management functions
  for metadata (no ADOMD.NET/Windows-only tooling required). Includes
  `PowerBiAuth` (MSAL-based AAD auth, service principal or device-code flow)
  and `PowerBiRestClient` (the `QueryExecutor` implementation).

**Profiling**:
- `profiling/dax_deps.py` — best-effort static scan of every DAX expression
  in the model (measures, calculated columns, calculation items, RLS
  filters, relationships, sort-by-columns, hierarchy levels) to determine
  which columns/measures are referenced anywhere in the model's own
  formulas.
- `profiling/schema.py` — metadata-level profile: counts, per-table
  breakdown, data type distribution, missing descriptions, naming issues,
  and unused columns/measures (via the dependency scan above).
- `profiling/data.py` — data-level profile (live models only): row count,
  distinct count, null count/percentage, min/max per column, computed with
  one batched DAX query per table.
- `profiling/rules.py` — a 15-rule best-practice engine in the spirit of
  Tabular Editor's BPA: missing descriptions, unused columns/measures,
  bidirectional/inactive relationships, `/` used instead of `DIVIDE(...)`,
  calculated columns that aggregate, isolated tables, empty calculation
  groups, naming issues, plus (data-profile only) empty tables, high
  null-rate columns, and columns that look like keys.

**Output**:
- `report/html.py` — self-contained HTML report (no external CSS/JS,
  works offline, follows the browser's light/dark theme).
- `cli.py` — `pbi-profile profile --source {tmdl,bim,live} ...` (writes
  `profile.json` and, with `--html`, `report.html`) and
  `pbi-profile list-rules`.

**Tests & packaging**:
- 34 pytest tests, including a hand-built TMDL fixture project and an
  equivalent `Model.bim`, cross-checked against each other, and fake
  `QueryExecutor`-based tests for the live loader and data profiler (no
  real Power BI credentials or network access needed).
- `pyproject.toml` with a `pbi-profile` console-script entry point; core
  (`tmdl`/`bim`) profiling has no third-party dependencies, `[live]` adds
  `requests`/`msal` for the REST/AAD path.
