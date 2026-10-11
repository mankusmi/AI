import json
from pathlib import Path

from devops_planner.cli import main
from devops_planner.planner import build_plan
from devops_planner.tickets import Ticket, load_tickets

EX = Path(__file__).parent.parent / "devops_planner" / "examples" / "tickets.csv"


def sprint_of(plan, tid):
    return next(n for n, s in enumerate(plan.sprints) if any(t.id == tid for t in s))


def test_example_dependencies_respected_and_done_skipped():
    plan = build_plan(load_tickets(EX), capacity=20)
    assert plan.skipped_done == 1
    ids = [t.id for s in plan.sprints for t in s]
    assert "OPS-8" not in ids
    for t in load_tickets(EX):
        for d in t.depends_on:
            assert sprint_of(plan, d) <= sprint_of(plan, t.id)
            assert ids.index(d) < ids.index(t.id)
    assert plan.critical_path == ["OPS-1", "OPS-2", "OPS-4"]
    assert plan.critical_points == 21
    assert all(sum(plan.points[t.id] for t in s) <= 20 for s in plan.sprints)


def test_critical_bug_scheduled_first_and_missing_estimate_flagged():
    plan = build_plan(load_tickets(EX), capacity=20)
    assert plan.sprints[0][0].id == "OPS-7"
    assert any("OPS-5 has no estimate" in r for r in plan.risks)


def test_cycle_detected_and_still_scheduled():
    ts = [Ticket("A", depends_on=["B"], estimate=1), Ticket("B", depends_on=["A"], estimate=1), Ticket("C", estimate=1)]
    plan = build_plan(ts)
    assert plan.cycles == [["A", "B"]]
    assert sum(len(s) for s in plan.sprints) == 3


def test_unknown_dep_and_oversize():
    plan = build_plan([Ticket("A", depends_on=["ZZZ"], estimate=50)], capacity=10)
    assert any("unknown ticket ZZZ" in r for r in plan.risks)
    assert any("exceeds sprint capacity" in r for r in plan.risks)
    assert len(plan.sprints) == 1


def test_cli_json_and_markdown(tmp_path, capsys):
    out = tmp_path / "p.json"
    assert main([str(EX), "-f", "json", "-o", str(out)]) == 0
    assert json.loads(out.read_text())["critical_path"][-1] == "OPS-4"
    assert main([str(EX)]) == 0
    assert "## Sprint 1" in capsys.readouterr().out
    assert main([str(tmp_path / "nope.csv")]) == 2
