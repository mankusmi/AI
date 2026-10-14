# devops-plan

Turns a DevOps ticket export (CSV or JSON; Azure DevOps, Jira, GitHub or hand-written) into a sprint plan.
Standard library only.

    python -m devops_planner tickets.csv -c 20 -o plan.md        # or: devops-plan after pip install .
    python -m devops_planner tickets.json -f json

Columns are matched by common names: `ID/Key`, `Title/Summary`, `Type`, `Priority`, `Story Points/Effort/Estimate`,
`State/Status`, `Assigned To`, `Tags/Labels`, `Depends On/Predecessors` (`;` or `,` separated). See `examples/tickets.csv`.

What it does: skips closed tickets (their dependents count as unblocked), orders by dependency then priority, finds the
critical path (longest estimate-weighted chain), packs sprints to the given capacity (a ticket never lands before its
dependencies), and reports risks: cycles, unknown dependencies, missing estimates (defaulted by type), oversize tickets, unassigned work.
A dependent may share a sprint with its dependency; it is listed after it.

## devops-triage: backlog clean-up

    python -m devops_planner.triage_cli backlog.csv -o review.md --csv changes.csv

Classifies by size and structure using your definitions: **user story** = a few days (<= 5), **feature** = one sprint
(<= 10 days), **epic** = a quarter (<= 65 days); override with `--story-max/--feature-max/--epic-max`, and
`--days-per-point` converts points to days. Containers without an estimate are sized from their children. Items within 25% of a
range are left alone. A parent must outrank its children (epic > feature > story); bugs and tasks are never reclassified.

Also flags: parent not found / open under closed parent, orphans (with candidate parents from the nearest area), child area
outside parent's area, missing or too-shallow area paths (`--min-area-depth`), too big for a quarter, empty or single-child
containers, duplicates (same title + area), stale items, in-progress but unassigned, weak titles, unsized items.
`changes.csv` has one row per ticket needing action (suggested type and parent) for bulk update. See `examples/backlog.csv`.

## devops-report: state of play for a head of delivery

    python -m devops_planner.report_cli backlog.csv -o state-of-play.html          # or -f markdown
    python -m devops_planner.report_cli backlog.csv --as-of 2026-10-12

One page: overall RAG, completion %, in progress / blocked / not started, forecast finish date, progress and RAG per epic
(per top-level area if there are no epics), weekly throughput, blocked and gone-quiet work, backlog hygiene, and a short
list of decisions needed. Needs `Parent` and `State`; `Closed Date` and `Changed Date` enable throughput, forecast and ageing.

- Progress and throughput count leaf work items (those with no children), weighted by days of work; items with no estimate count by number.
- An epic is **red** if it has blocked work, no movement for 30+ days, or an open critical bug; **amber** if over 25% of its open work is
  unsized or nothing is in progress. Overall is red when a quarter of open epics are red.
- Forecast = remaining days of work / average days closed per week over the last 4 completed weeks.
- Blocked means state `Blocked` or a `blocked` tag. In progress means state Active / In Progress / Doing / Committed etc.
See `examples/delivery.csv`.

### Day-to-day monitoring: the data lead view

`devops-report` now has two sections on one page: **Overview** (for the head of delivery) and **Data lead view** (for daily use).

    python -m devops_planner.report_cli export.csv --previous yesterday.csv -o today.html
    python -m devops_planner.report_cli export.csv --view lead          # data lead section only
    python -m devops_planner.report_cli export.csv --view exec          # head of delivery section only

The data lead view has: six status tiles; a ranked **Needs attention** list; **Since the last run** (closed, started, newly blocked,
unblocked, added, reopened; needs `--previous`); and five charts: burn-up, weekly throughput, workload by person, stuck and ageing
work, and a heatmap of problems by area. Every chart has hover details and a "Table view" underneath it.

Flags, from most to least serious (every flag shows an icon and a word, never colour alone):
- **Critical:** blocked work; open critical bugs; in progress for over twice the ageing limit with no update.
- **Serious:** in progress and quiet past the ageing limit (`--aging-days`, default 14); anyone over the WIP limit (`--wip-limit`, default 3
  items in flight); in progress with no owner; last full week's throughput under half the 4-week average; nothing closed recently.
- **Watch:** closed items with no Closed Date (they vanish from throughput); in-progress items with no Changed Date (ageing can't be checked);
  more than 25% of open work unestimated; items untouched for 60+ days; items with no parent; items that look the wrong type.

Daily routine: export from Azure DevOps each morning, keep yesterday's file, and pass it as `--previous`.
The burn-up uses today's scope for every week (scope history isn't in a single export). See `examples/daily.csv` and `examples/daily_yesterday.csv`.
