"""Write a snapshot to disk in run order, and load it back."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .analysis import analyze


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("_")[:60] or "unnamed"


def workspace_dir(root: Path, snapshot: dict) -> Path:
    ws = snapshot["workspace"]
    return Path(root) / f"{slug(ws.get('name') or ws['id'])}"


def save_snapshot(snapshot: dict, root: str | Path) -> tuple[Path, dict]:
    """Layout: <root>/<workspace>/{manifest.json, workspace_objects.json, dataflows/NN_name.json}.

    NN_ is the run position (dependencies first), so a plain directory listing shows execution order.
    """
    out = workspace_dir(Path(root), snapshot)
    (out / "dataflows").mkdir(parents=True, exist_ok=True)
    for old in (out / "dataflows").glob("*.json"):
        old.unlink()  # re-runs must not leave stale numbering behind
    analysis = analyze(snapshot)
    by_id = {f["id"]: f for f in snapshot["dataflows"]}
    width = max(2, len(str(len(analysis["flows"]))))
    entries = []
    for r in analysis["flows"]:
        f = by_id[r["id"]]
        fname = f"{r['order']:0{width}d}_{slug(r['name'])}.json"
        if f.get("definition") is not None:
            (out / "dataflows" / fname).write_text(json.dumps(f["definition"], indent=2), encoding="utf-8")
        entries.append({"order": r["order"], "stage": r["stage"], "id": r["id"], "name": r["name"],
                        "file": f"dataflows/{fname}" if f.get("definition") is not None else None,
                        "depends_on": r["depends_on"], "external_dependencies": r["external_deps"],
                        "meta": f.get("meta", {}), "datasources": f.get("datasources", []),
                        "upstream": f.get("upstream", []), "transactions": f.get("transactions", [])})
    manifest = {"fetched_at": snapshot["fetched_at"], "workspace": snapshot["workspace"],
                "run_order_note": "Dependencies first; dataflows in the same stage are independent of each other. "
                                  "Power BI does not expose Power Automate/schedule chaining, so this is derived "
                                  "from linked-entity dependencies and refresh history.",
                "stages": [[by_id[i]["name"] for i in s] for s in analysis["stages"]],
                "dataflows": entries, "dataset_upstream": snapshot.get("dataset_upstream", []),
                "warnings": analysis["warnings"]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "workspace_objects.json").write_text(
        json.dumps({"workspace": snapshot["workspace"], "objects": snapshot["objects"], "users": snapshot["users"]}, indent=2),
        encoding="utf-8")
    return out, analysis


def load_snapshot(directory: str | Path) -> dict:
    d = Path(directory)
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    objs = json.loads((d / "workspace_objects.json").read_text(encoding="utf-8"))
    flows = []
    for e in manifest["dataflows"]:
        definition = json.loads((d / e["file"]).read_text(encoding="utf-8")) if e.get("file") else None
        flows.append({"id": e["id"], "name": e["name"], "meta": e.get("meta", {}), "definition": definition,
                      "datasources": e.get("datasources", []), "upstream": e.get("upstream", []),
                      "transactions": e.get("transactions", [])})
    return {"fetched_at": manifest["fetched_at"], "workspace": manifest["workspace"], "objects": objs["objects"],
            "users": objs.get("users", []), "dataset_upstream": manifest.get("dataset_upstream", []),
            "dataflows": flows, "warnings": [w for w in manifest.get("warnings", []) if not w.startswith("Dependency cycle")]}
