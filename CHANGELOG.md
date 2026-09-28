# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added — dependency graph diagrams (`--dependency-graph`)

Embeds two diagrams in `report.html` (and their raw edge data in
`profile.json`), gated behind a new opt-in `--dependency-graph` flag:

- `profiling/graph.py` — `build_measure_dependency_edges()` reuses the
  existing DAX reference scanner (`dax_deps.find_references`) to build a
  proper per-measure edge list (which measure references which other
  measure by name), rather than the flat usage set `analyze_model` already
  produces for the unused-object checks.
- `report/graph_svg.py` — hand-rolled, dependency-free inline SVG rendering:
  no JS, no vendored charting library (jsdelivr/unpkg are blocked in this
  environment anyway, and self-hosting Mermaid would need the npm registry
  and add 1-3MB per report). A layered left-to-right layout for the measure
  DAG (DAX disallows circular measure references, so this is always a true
  DAG; a defensive cycle guard keeps a malformed edge list from hanging
  regardless), and a circular layout for the table relationship graph
  (relationships aren't guaranteed acyclic/hierarchical). Parallel edges
  between the same two nodes fan out as bezier curves instead of
  overlapping; inactive relationships are dashed, bidirectional ones get
  arrowheads at both ends, and cardinality is shown as an edge label when
  known. Both `render_*_svg()` functions are pure and deterministic (sorted
  iteration throughout), so they're covered by plain pytest string
  assertions rather than needing a browser to verify.
- `profile_runner.py` / `cli.py` / `report/html.py` — wired through as
  `ProfileResult.dependency_graph`, `--dependency-graph`, and a new
  "Dependency graphs" HTML section, following the same `None`-means-
  "not computed" pattern already used for the data and report profiles.
- 12 new tests (64 total) covering the edge-building logic against the
  existing TMDL fixture (which already happens to have exactly the right
  shape: two measures both referencing `Sales[Total Sales]`), SVG rendering
  edge cases (empty input, inactive/bidirectional styling, cardinality
  labels, parallel-edge fan-out, cycle safety, determinism), and a CLI
  end-to-end run.

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

### Added — report/visual analysis (pages, visuals, field usage cross-check)

Extends `pbi-profiler` to also analyze the **report** layer (pages and
visuals), not just the semantic model, and to close the model profiler's
"can't see report usage" blind spot.

- `report_model.py` — backend-agnostic `ReportModel`/`Page`/`Visual`/
  `FieldRef` dataclasses.
- `report_loaders/` — two loaders producing a `ReportModel`:
  - `PbirReportLoader` — parses a PBIP `<Name>.Report/definition/pages/**/
    visual.json` folder (the modern report project format), extracting each
    visual's type, title, and field references with their query role
    (Category/Y/Values/etc.) straight from `queryState`.
  - `LegacyLayoutReportLoader` — parses a standalone extracted legacy
    `Layout` JSON file (the older single-blob report format), including
    resolving the `Source` alias indirection in `prototypeQuery.From` back
    to table names.
  - `report_loaders/_field_expr.py` — the shared Column/Measure/Aggregation/
    HierarchyLevel field-expression and title parsing both loaders use.
- `profiling/visuals.py` — `compute_report_profile()` (page/visual
  inventory: counts by visual type, per-page visual counts, per-visual field
  counts) and `usage_from_report()` (a `UsageInfo` built from every visual's
  field references).
- `profiling/dax_deps.py` — new `combine_usage()` merges the model's own DAX
  usage with `usage_from_report()`'s report usage, so `unused-visible-column`
  / `unused-measure` no longer flag a column/measure that a report visual
  actually uses but no DAX expression does.
- `profiling/report_rules.py` — a 5-rule report-level engine, sharing the
  same `Finding` shape as the model rule engine: visuals missing a title,
  empty pages, visuals with an unusually high distinct field count, likely
  duplicate visuals (same type + fields on one page), and — cross-checked
  against the model — visual field references pointing at a table/column/
  measure that doesn't exist.
- `cli.py` — new `--report-source {pbir,legacy-layout}` / `--report-path`
  flags on `profile` (both `profile.json` and, with `--html`, `report.html`
  gain the report/visual data), and a new `list-report-rules` subcommand.
- 18 more pytest tests (52 total) plus a small PBIR report fixture and an
  equivalent legacy `Layout` JSON fixture, including one that demonstrates
  the usage cross-check fixing a false "unused" positive.
