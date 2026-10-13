# pbi-dataflows

Connects to a Power BI workspace, exports every dataflow definition (`model.json`) and documents the workspace.

```bash
pip install -e ".[live]"

# interactive device-code sign-in (uses the public Azure CLI app id unless you pass --client-id)
pbi-dataflows fetch --workspace-id <guid> -o out

# service principal (must be allowed to use Power BI APIs and be a workspace member)
pbi-dataflows fetch --workspace-id <guid> --tenant-id <t> --client-id <c> --client-secret <s> -o out

# existing token:  --token <jwt>  or  PBI_ACCESS_TOKEN
# rebuild docs from a previous fetch, offline:
pbi-dataflows document out/<workspace>
```

Output (`out/<workspace>/`):

| Path | Content |
|---|---|
| `dataflows/NN_name.json` | Raw dataflow definition. `NN` is the run position, so the listing is the execution order |
| `manifest.json` | Run order, stages, dependencies, datasources, refresh history, per-dataflow metadata |
| `workspace_objects.json` | Datasets, reports, dashboards, dataflows, users |
| `summary.html` / `SUMMARY.md` | Workspace properties, object counts and lists, run order, what each dataflow does |
| `docs/NN_name.md` | Per-dataflow documentation: sources, entities and columns, transformations, dependencies, refreshes |

## How run order is derived

The Power BI API has no "orchestration order" (schedules, Power Automate and pipelines are not exposed). Order is
inferred: dataflows linked to others (upstream API, linked entities, `PowerPlatform.Dataflows` M code) run after
their dependencies. Dataflows with no dependency between them share a *stage*; inside a stage they are ordered by
last successful refresh start time, then by name. Cycles are reported as a warning rather than failing.

Summaries are generated deterministically from the definition (no LLM): sources, output tables, M transformation
types, upstream/downstream dataflows and datasets. Endpoints you lack permission for become warnings.
