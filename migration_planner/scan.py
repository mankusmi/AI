"""Discover Power BI Projects (PBIP) under a folder and write a manifest.

A PBIP holds `<Name>.Report` and `<Name>.SemanticModel` folders. The report's
`definition.pbir` says which model it uses (`byPath`, usually a sibling folder).
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .manifest import COLUMNS


@dataclass
class Found:
    name: str
    report_source: str
    report_path: Path
    model_source: str
    model_path: Path
    notes: str = ""


def _model_for(report: Path) -> tuple[Optional[Path], str]:
    pbir = report / "definition.pbir"
    if pbir.is_file():
        try:
            ref = (json.loads(pbir.read_text(encoding="utf-8-sig")).get("datasetReference") or {})
        except (ValueError, OSError):
            ref = {}
        by_path = (ref.get("byPath") or {}).get("path")
        if by_path:
            return (report / by_path).resolve(), ""
        if ref.get("byConnection"):
            return None, "report is live-connected to a published model; export that model as PBIP/BIM and add the row manually"
    sib = report.parent / (report.name[: -len(".Report")] + ".SemanticModel")
    if sib.is_dir():
        return sib, ""
    return None, "no semantic model found next to the report"


def _model_kind(model: Path) -> Optional[tuple[str, Path]]:
    if (model / "definition").is_dir() or (model / "tables").is_dir():
        return "tmdl", model
    for cand in (model / "model.bim", model / "Model.bim"):
        if cand.is_file():
            return "bim", cand
    return None


def _report_kind(report: Path) -> Optional[tuple[str, Path]]:
    if (report / "definition" / "pages").is_dir():
        return "pbir", report
    if (report / "report.json").is_file():
        return "legacy-layout", report / "report.json"
    return None


def scan(root: str | Path) -> tuple[list[Found], list[str]]:
    found: list[Found] = []
    warnings: list[str] = []
    for report in sorted(Path(root).rglob("*.Report")):
        if not report.is_dir():
            continue
        name = report.name[: -len(".Report")]
        rk = _report_kind(report)
        if not rk:
            warnings.append(f"{report}: no PBIR definition/pages or report.json, skipped")
            continue
        model, why = _model_for(report)
        mk = _model_kind(model) if model and model.is_dir() else None
        if not mk:
            warnings.append(f"{report}: {why or 'model folder has no TMDL definition or model.bim'}, skipped")
            continue
        found.append(Found(name, rk[0], rk[1], mk[0], mk[1], "legacy report.json" if rk[0] == "legacy-layout" else ""))
    return found, warnings


def write_manifest(found: list[Found], out: str | Path, root: str | Path) -> None:
    base = Path(out).resolve().parent

    def rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(base))
        except ValueError:
            return str(p.resolve())

    with Path(out).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for f in found:
            w.writerow([f.name, "", f.report_source, rel(f.report_path), f.model_source, rel(f.model_path), "", f.notes])
