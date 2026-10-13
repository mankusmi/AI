from datetime import datetime
from pathlib import Path

from devops_planner.report import build_report
from devops_planner.report_cli import main
from devops_planner.tickets import load_tickets

EX = Path(__file__).parent.parent / "devops_planner" / "examples" / "delivery.csv"
NOW = datetime(2026, 10, 12)


def rep():
    return build_report(load_tickets(EX), as_of=NOW)


def test_counts_and_progress():
    r = rep()
    assert r.counts == {"done": 5, "active": 3, "blocked": 1, "todo": 3}
    assert round(r.pct_done) == 45


def test_epic_rag_and_reasons():
    r = rep()
    rows = {x.id: x for x in r.rows}
    assert rows["E1"].rag == "green"
    assert rows["E2"].rag == "red" and any("blocked" in s for s in rows["E2"].reasons)
    assert rows["E3"].rag == "red" and "open critical bug" in rows["E3"].reasons
    assert r.overall == "red"


def test_forecast_uses_completed_weeks_only():
    r = rep()
    assert [w[0] for w in r.weekly][-1] == "05 Oct"        # last completed week; the current one (12 Oct) is excluded
    assert r.eta_date and r.eta_date > NOW
    assert r.remaining_days == 23


def test_blocked_and_aging():
    r = rep()
    assert [t.id for t in r.blocked] == ["S7"]
    assert [t.id for t, _ in r.aging] == ["S6"]


def test_area_grouping_when_no_epics():
    ts = [t for t in load_tickets(EX) if t.type != "epic"]
    r = build_report(ts, as_of=NOW)
    assert r.group_by == "area"
    assert {x.id for x in r.rows} >= {"Proj \\ Platform", "Proj \\ Security"}


def test_cli_html_and_markdown(tmp_path, capsys):
    out = tmp_path / "r.html"
    assert main([str(EX), "-o", str(out), "--as-of", "2026-10-12"]) == 0
    html = out.read_text()
    assert html.startswith("<!doctype html>") and "Cloud platform migration" in html and "RED" in html
    assert main([str(EX), "-f", "markdown", "--as-of", "2026-10-12"]) == 0
    assert "## Progress by epic" in capsys.readouterr().out
    assert main([str(tmp_path / "nope.csv")]) == 2
    assert main([str(EX), "--as-of", "bad"]) == 2
