from datetime import datetime
from pathlib import Path

from devops_planner.cli import main as plan_main
from devops_planner.tickets import load_tickets
from devops_planner.triage import Config, review
from devops_planner.triage_cli import main

EX = Path(__file__).parent.parent / "devops_planner" / "examples" / "backlog.csv"
NOW = datetime(2026, 10, 11)


def rules(r, tid):
    return {f.rule for f in r.findings if f.id == tid}


def test_reclassification_by_size_and_structure():
    r = review(load_tickets(EX), now=NOW)
    assert r.suggested_type == {"E2": "story", "F1": "epic", "S2": "epic"}
    assert "F2" not in r.suggested_type            # 6 days, feature is right
    assert "S1" not in r.suggested_type


def test_container_with_children_cannot_be_story():
    from devops_planner.tickets import Ticket
    ts = [Ticket("S", type="story", days=2), Ticket("C", type="story", days=1, parent="S")]
    r = review(ts, now=NOW)
    assert r.suggested_type == {"S": "feature"}
    assert "bad-hierarchy" in rules(r, "C")


def test_hierarchy_area_and_hygiene_findings():
    r = review(load_tickets(EX), now=NOW)
    assert "area-mismatch" in rules(r, "S6")
    assert {"orphan", "shallow-area"} <= rules(r, "S4")
    assert r.suggested_parent["S4"]                  # candidate features offered
    assert {"duplicate", "stale"} <= rules(r, "S5")
    assert "no-area" in rules(r, "T1") and "weak-title" in rules(r, "T1")
    assert "empty-container" in rules(r, "F3")


def test_custom_thresholds_and_points_conversion():
    r = review(load_tickets(EX), Config(story_max=15, days_per_point=1), now=NOW)
    assert "S2" not in r.suggested_type              # 13 days now fits a story


def test_cli_outputs(tmp_path, capsys):
    csv_out = tmp_path / "c.csv"
    assert main([str(EX), "--csv", str(csv_out)]) == 0
    assert "Suggested reclassification" in capsys.readouterr().out
    assert csv_out.read_text().splitlines()[0].startswith("ID,Title,Current Type")
    assert main([str(tmp_path / "missing.csv")]) == 2


def test_planner_still_reads_story_type_aliases():
    assert plan_main([str(EX)]) == 0
