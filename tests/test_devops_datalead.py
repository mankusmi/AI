from datetime import datetime
from pathlib import Path

from devops_planner.report import build_report
from devops_planner.report_cli import main
from devops_planner.report_render import to_html, to_markdown
from devops_planner.tickets import Ticket, load_tickets

EX = Path(__file__).parent.parent / "devops_planner" / "examples"
NOW = datetime(2026, 10, 12)


def lead(prev=False, **kw):
    p = load_tickets(EX / "daily_yesterday.csv") if prev else None
    return build_report(load_tickets(EX / "daily.csv"), as_of=NOW, previous=p, **kw).lead


def titles(dl):
    return {f.title: f for f in dl.flags}


def test_flags_are_ranked_and_cover_the_planted_problems():
    dl = lead()
    sev = [f.severity for f in dl.flags]
    assert sev == sorted(sev, key=["critical", "serious", "warning"].index)
    t = " | ".join(titles(dl))
    assert "1 blocked" in t and "1 critical bug open" in t and "without an update" in t
    assert "over the WIP limit" in t and "no owner" in t and "Throughput dropped" in t
    assert titles(dl)["1 blocked"].ids == ["S7"]


def test_workload_and_wip_limit():
    dl = lead()
    bob = next(p for p in dl.people if p.name == "bob")
    assert bob.active == 4 and bob.over_limit
    assert not next(p for p in dl.people if p.name == "Unassigned").over_limit     # no owner is its own flag
    assert not any(p.over_limit for p in lead(wip_limit=5).people)


def test_ageing_sorted_oldest_first_and_blocked_marked():
    dl = lead()
    assert dl.aged[0].ticket.id == "S7" and dl.aged[0].blocked and dl.aged[0].days == 53
    assert [a.days for a in dl.aged] == sorted((a.days for a in dl.aged), reverse=True)


def test_burnup_is_cumulative_and_heatmap_skips_clean_areas():
    dl = lead()
    vals = [v for _, v in dl.burnup]
    assert vals == sorted(vals) and vals[-1] == 19
    assert all(sum(r.values()) for _, r in dl.heat_rows)
    assert not any("Secrets" in a for a, _ in dl.heat_rows)


def test_changes_since_previous_export():
    c = lead(prev=True).changes
    assert [t.id for t in c.closed] == ["S5"] and [t.id for t in c.started] == ["S3"]
    assert [t.id for t in c.newly_blocked] == ["S7"] and [t.id for t in c.added] == ["S13"]
    assert lead().changes is None


def test_clean_backlog_flags_nothing_serious():
    ts = [Ticket("A", title="Do the thing properly", type="story", estimate=3, state="Done", assignee="x", area="P\\Team", closed=datetime(2026, 10, 6))]
    dl = build_report(ts, as_of=NOW).lead
    assert not [f for f in dl.flags if f.severity in ("critical", "serious")]


def test_html_has_both_sections_and_is_safe():
    r = build_report(load_tickets(EX / "daily.csv"), as_of=NOW)
    r.titles["S7"] = "<script>alert(1)</script>"
    html = to_html(r)
    assert "id=overview" in html and "id=lead" in html and "Table view" in html
    assert "<script>alert(1)</script>" not in html
    assert "id=lead" not in to_html(r, view="exec") and "id=overview" not in to_html(r, view="lead")
    assert "## Data lead view" in to_markdown(r)


def test_cli_options(tmp_path):
    out = tmp_path / "r.html"
    base = [str(EX / "daily.csv"), "--as-of", "2026-10-12", "-o", str(out)]
    assert main(base + ["--previous", str(EX / "daily_yesterday.csv"), "--wip-limit", "5", "--view", "lead"]) == 0
    html = out.read_text()
    assert "Since the last run" in html and "id=overview" not in html
    assert main(base + ["--previous", str(tmp_path / "missing.csv")]) == 2
