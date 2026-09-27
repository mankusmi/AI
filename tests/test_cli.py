import json

from pbi_profiler.cli import main
from .conftest import TMDL_SAMPLE, BIM_SAMPLE


def test_cli_profile_tmdl_writes_json_and_html(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(
        [
            "profile",
            "--source",
            "tmdl",
            "--path",
            str(TMDL_SAMPLE),
            "--html",
            "--output",
            str(out_dir),
        ]
    )
    assert rc == 0
    assert (out_dir / "profile.json").is_file()
    assert (out_dir / "report.html").is_file()

    data = json.loads((out_dir / "profile.json").read_text())
    assert data["model_name"] == "MyModel"
    assert data["source_kind"] == "tmdl"
    assert data["data"] is None  # no live executor for tmdl source
    assert data["schema"]["table_count"] == 2
    assert any(f["rule_id"] == "unused-visible-column" for f in data["findings"])

    html = (out_dir / "report.html").read_text()
    assert "<!doctype html>" in html
    assert "MyModel" in html


def test_cli_profile_bim(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["profile", "--source", "bim", "--path", str(BIM_SAMPLE), "--output", str(out_dir)])
    assert rc == 0
    data = json.loads((out_dir / "profile.json").read_text())
    assert data["source_kind"] == "bim"


def test_cli_list_rules_runs(capsys):
    rc = main(["list-rules"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "unused-visible-column" in out


def test_cli_live_requires_ids():
    rc = 1
    try:
        main(["profile", "--source", "live"])
        rc = 0
    except SystemExit as e:
        rc = e.code
    assert rc != 0
