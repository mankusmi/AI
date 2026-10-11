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
