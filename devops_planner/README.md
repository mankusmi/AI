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
