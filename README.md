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

# List the built-in best-practice rules
pbi-profile list-rules
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

## Architecture

```
pbi_profiler/
  model.py            Backend-agnostic Model/Table/Column/Measure/... dataclasses
  loaders/
    tmdl_parser.py     Generic indentation-based TMDL tree parser
    tmdl_loader.py      -> Model, from a PBIP definition/ folder
    bim_loader.py        -> Model, from Model.bim / TMSL JSON
    live_loader.py        -> Model, from DAX INFO.VIEW.* queries; also the
                             PowerBiAuth/PowerBiRestClient used to run them
  profiling/
    dax_deps.py        Static "what references what" scan over model DAX
    schema.py          Metadata-level profile
    data.py            Data-level profile (needs a QueryExecutor)
    rules.py           Best-practice rule engine
  report/html.py       Self-contained HTML report renderer
  profile_runner.py    Wires loader -> profilers -> rules into one result
  cli.py               `pbi-profile` command-line entry point
```

Every loader implements the same `ModelLoader.load() -> Model` interface, so
the profilers, rule engine and report never need to know which format the
model came from. The live loader additionally exposes a `QueryExecutor`
(`run_dax(query) -> rows`) that the data profiler uses -- and that tests
fake out, so nothing in `profiling/` or `report/` needs network access or
real Power BI credentials to test.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Fixtures under `tests/fixtures/` include a small hand-built TMDL project and
an equivalent `Model.bim`, so the two loaders' output can be cross-checked
against each other.

## Known limitations

* The TMDL parser implements the subset of the grammar needed for profiling
  (tables, columns, measures, hierarchies, partitions, relationships, roles),
  not the full language -- calculation groups are read but less exercised by
  tests, and things like translations/perspectives/shared expressions are
  not parsed.
* "Unused" findings are a static, model-internal analysis only; they cannot
  see report-level usage.
* The live loader's exact DAX `INFO.VIEW.*` column names are based on
  Microsoft's documented shape for these functions but haven't been
  exercised against a real tenant in this environment -- if a query comes
  back empty for a model where you expect data, check the warning it emits
  and adjust the DAX in `live_loader.py` to match what your engine version
  actually returns.
