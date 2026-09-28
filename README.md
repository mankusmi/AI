# pbi-profiler

A profiling and best-practice analysis tool for Power BI / Analysis Services
**semantic models** (Tabular models). It inspects a model's metadata (tables,
columns, measures, relationships, hierarchies, RLS roles), optionally profiles
the underlying data (row counts, null rates, distinct counts, min/max), and
runs a set of best-practice rules similar in spirit to Tabular Editor's Best
Practice Analyzer -- then writes the results as JSON and (optionally) a
self-contained HTML report.

Three ways to point it at a model:

| Source | What it reads | Data profiling? |
|---|---|---|
| `tmdl` | A PBIP `<Name>.SemanticModel/definition` folder (Power BI Desktop project format) | No (metadata only) |
| `bim`  | A legacy `Model.bim` / TMSL JSON file (Analysis Services Tabular project, or a Tabular Editor export) | No (metadata only) |
| `live` | A published dataset over the Power BI REST API (`executeQueries`, using DAX) | Yes |

Optionally, it can also analyze the **report** layer (pages and visuals),
either the modern PBIR project format or the legacy embedded layout, and
cross-check which model fields those visuals actually use:

| Report source | What it reads |
|---|---|
| `pbir` | A PBIP `<Name>.Report/definition/pages/**/visual.json` folder (Power BI Desktop project format) |
| `legacy-layout` | A standalone, already-extracted legacy `Layout` JSON file (historically embedded as the `Report/Layout` part inside a `.pbix`) |

## Install

Core (offline `tmdl`/`bim` profiling) needs nothing beyond the standard
library:

```bash
pip install -e .
```

For `--source live` (Power BI REST API + Azure AD auth), install the extra:

```bash
pip install -e ".[live]"
```

## Usage

```bash
# Profile a Power BI Desktop project (PBIP) folder, with an HTML report
pbi-profile profile --source tmdl --path ./MySemanticModel.SemanticModel --html

# Profile a legacy Model.bim file
pbi-profile profile --source bim --path ./Model.bim --html

# Profile a published dataset live (metadata + data stats via DAX)
pbi-profile profile --source live \
  --workspace-id <workspace-guid> --dataset-id <dataset-guid> \
  --tenant-id <aad-tenant-guid> --client-id <aad-app-guid> \
  --client-secret <secret>   # omit to fall back to an interactive device-code login
  --html

# Also analyze the report's visuals (pages/visual field usage), and cross-check
# which model fields those visuals actually reference
pbi-profile profile --source tmdl --path ./MySemanticModel.SemanticModel \
  --report-source pbir --report-path ./MyReport.Report --html

# Also embed measure-dependency and table-relationship diagrams in the HTML report
pbi-profile profile --source tmdl --path ./MySemanticModel.SemanticModel \
  --dependency-graph --html

# List the built-in best-practice rules
pbi-profile list-rules
pbi-profile list-report-rules
```

Output goes to `--output` (default `./pbi_profile_output/`): always a
`profile.json`, plus a `report.html` when `--html` is passed. The command
exits non-zero if any `error`-severity finding was raised (there are none by
default -- see [Rules](#rules)).

### Live-mode auth

`--source live` needs an Azure AD app registration with access to the Power
BI service (`https://analysis.windows.net/powerbi/api/.default` scope) and
permission on the target workspace. Credentials can be passed as flags or via
environment variables: `PBI_TENANT_ID`, `PBI_CLIENT_ID`, `PBI_CLIENT_SECRET`.
With a client secret it uses a confidential-client (service principal) flow;
without one, it falls back to an interactive device-code flow (prints a URL
and code to complete sign-in in a browser) -- handy for local/manual runs,
not for unattended pipelines.

Live metadata is read via DAX `INFO.VIEW.*` dynamic management functions
(`INFO.VIEW.TABLES`, `.COLUMNS`, `.MEASURES`, `.RELATIONSHIPS`, `.HIERARCHIES`,
plus `INFO.ROLES`/`INFO.VIEW.TABLEPERMISSIONS` for RLS) run through the
[Execute Queries REST
API](https://learn.microsoft.com/rest/api/power-bi/datasets/execute-queries) --
no ODBC driver, ADOMD.NET, or Windows-only tooling required. Because
different engine/compatibility levels support different subsets of these
functions, each metadata query is best-effort: one that fails is skipped with
a warning rather than aborting the whole profile.

## What gets profiled

**Schema profile** (always computed): counts of tables/columns/measures/
relationships/hierarchies/roles, per-table breakdowns, data type
distribution, missing descriptions, and *unused* columns/measures -- based on
a best-effort static scan of every DAX expression in the model (other
measures, calculated columns, calculation items, RLS filters, relationships,
sort-by-columns, hierarchy levels). This has two known blind spots, called
out wherever it's surfaced: it can't see whether something is used directly
in a *report* visual, and a bare `[Name]` reference is ambiguous between a
measure and a same-table column (both are marked "used" to avoid false
positives).

**Data profile** (`--source live` only, skip with `--no-data-profile`): row
count, distinct count, null count/percentage, and min/max per column, via one
batched DAX query per table.

**Rules** (`pbi-profile list-rules` for the full list with descriptions):
missing descriptions, unused columns/measures, bidirectional/inactive
relationships, measures using raw `/` instead of `DIVIDE(...)`, calculated
columns that aggregate (usually should be measures), isolated tables, empty
calculation groups, naming issues, plus (live/data-profile only) empty
tables, high null-rate columns, and columns that look like keys.

**Report/visual profile** (`--report-source`/`--report-path` only): a page
and visual inventory (visual type, title, hidden flag, distinct field count),
plus every visual's field references resolved back to `table[field]` where
possible. Feeding this in also **fixes the schema profile's biggest blind
spot**: a column/measure referenced by a report visual but by no DAX
expression is no longer flagged "unused" -- `unused-visible-column`/
`unused-measure` see combined model+report usage whenever a report is
supplied.

**Report rules** (`pbi-profile list-report-rules`): visuals missing a title,
empty pages, visuals using an unusually large number of distinct fields,
likely duplicate visuals (same type + same fields on one page), and
(cross-checked against the model) visual field references pointing at a
table/column/measure that doesn't actually exist.

**Dependency graphs** (`--dependency-graph`; embedded as diagrams in
`report.html` with `--html`, always included in `profile.json` when the flag
is passed): a **measure dependency graph** (an edge means one measure's DAX
references another measure by name -- scoped to measures only, not the
columns they touch, to stay legible) and a **table relationship graph**
(every table-to-table relationship, dashed for inactive, double-arrowed for
bidirectional, labeled with cardinality when known). Both are rendered as
plain inline SVG -- no JavaScript, no vendored charting library, nothing
fetched over the network -- computed with a small hand-rolled layout (layered
left-to-right for the measure DAG since DAX disallows circular measure
references; a simple circular placement for relationships, since those
aren't guaranteed acyclic). A table with no relationships, or a measure that
references no other measure, simply doesn't appear in its diagram; that's
not a bug, it's the same signal the `isolated-table`/`unused-measure` rules
already surface elsewhere.

## Architecture

```
pbi_profiler/
  model.py             Backend-agnostic Model/Table/Column/Measure/... dataclasses
  report_model.py      Backend-agnostic ReportModel/Page/Visual/FieldRef dataclasses
  loaders/
    tmdl_parser.py     Generic indentation-based TMDL tree parser
    tmdl_loader.py      -> Model, from a PBIP definition/ folder
    bim_loader.py        -> Model, from Model.bim / TMSL JSON
    live_loader.py        -> Model, from DAX INFO.VIEW.* queries; also the
                             PowerBiAuth/PowerBiRestClient used to run them
  report_loaders/
    _field_expr.py     Shared Column/Measure/Aggregation/HierarchyLevel + title parsing
    pbir_loader.py      -> ReportModel, from a PBIR <Name>.Report/definition folder
    legacy_layout_loader.py -> ReportModel, from a standalone legacy Layout JSON file
  profiling/
    dax_deps.py        Static "what references what" scan over model DAX
                        (+ combine_usage() to merge in report-derived usage)
    schema.py          Metadata-level profile
    data.py            Data-level profile (needs a QueryExecutor)
    rules.py           Model best-practice rule engine
    visuals.py         Report/visual inventory + usage_from_report()
    report_rules.py    Report/visual best-practice rule engine
    graph.py           Measure dependency edges (reuses dax_deps.find_references)
  report/
    html.py            Self-contained HTML report renderer
    graph_svg.py        Hand-rolled inline-SVG rendering for the dependency diagrams
  profile_runner.py    Wires loaders -> profilers -> rule engines into one result
  cli.py               `pbi-profile` command-line entry point
```

Every model loader implements the same `ModelLoader.load() -> Model`
interface (and every report loader the same `ReportLoader.load() ->
ReportModel` interface), so the profilers, rule engines and report never
need to know which format the model/report came from. The live loader
additionally exposes a `QueryExecutor` (`run_dax(query) -> rows`) that the
data profiler uses -- and that tests fake out, so nothing in `profiling/` or
`report/` needs network access or real Power BI credentials to test.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Fixtures under `tests/fixtures/` include a small hand-built TMDL project and
an equivalent `Model.bim` (so the two model loaders' output can be
cross-checked against each other), plus a small PBIR report project and an
equivalent legacy `Layout` JSON (same cross-check, for the report loaders).

## Known limitations

* The TMDL parser implements the subset of the grammar needed for profiling
  (tables, columns, measures, hierarchies, partitions, relationships, roles),
  not the full language -- calculation groups are read but less exercised by
  tests, and things like translations/perspectives/shared expressions are
  not parsed.
* "Unused" findings are a static analysis: model DAX plus, when a report is
  supplied, that report's visual field references. Usage in a *different*
  report than the one passed in is still invisible.
* The live loader's exact DAX `INFO.VIEW.*` column names are based on
  Microsoft's documented shape for these functions but haven't been
  exercised against a real tenant in this environment -- if a query comes
  back empty for a model where you expect data, check the warning it emits
  and adjust the DAX in `live_loader.py` to match what your engine version
  actually returns.
* The PBIR/legacy-layout JSON shapes read by the report loaders (query
  roles/projections, the Column/Measure/Aggregation/HierarchyLevel field
  expressions, title objects) reflect the community-documented, empirically
  stable structure rather than an official published schema -- reads are
  defensive (an unrecognized visual/page is skipped, not fatal), but a field
  reference form not covered by the fixtures here may come back empty rather
  than resolved.
* A visual's title is only read when it's a static string literal; a
  dynamic (expression-bound) title comes back as no title.
