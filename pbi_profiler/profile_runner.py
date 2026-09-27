"""Orchestrates loading a model (and, optionally, a report), running the
schema/data/report profilers and both rule engines, and bundling the results
into one serializable `ProfileResult`. This is the shared entry point used by
the CLI (and importable directly from scripts/notebooks)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .loaders.base import QueryExecutor
from .model import Model
from .report_model import ReportModel
from .profiling import (
    DataProfile,
    Finding,
    ReportProfile,
    Rule,
    ReportRule,
    SchemaProfile,
    analyze_model,
    combine_usage,
    compute_data_profile,
    compute_report_profile,
    compute_schema_profile,
    run_report_rules,
    run_rules,
    usage_from_report,
)


@dataclass
class ProfileResult:
    model_name: str
    source_kind: str
    generated_at: str
    schema: SchemaProfile
    data: Optional[DataProfile]
    report: Optional[ReportProfile]
    findings: list[Finding]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "source_kind": self.source_kind,
            "generated_at": self.generated_at,
            "schema": self.schema.to_dict(),
            "data": self.data.to_dict() if self.data else None,
            "report": self.report.to_dict() if self.report else None,
            "findings": [f.to_dict() for f in self.findings],
        }


def run_profile(
    model: Model,
    executor: Optional[QueryExecutor] = None,
    report: Optional[ReportModel] = None,
    rules: Optional[list[Rule]] = None,
    report_rules: Optional[list[ReportRule]] = None,
) -> ProfileResult:
    model_usage = analyze_model(model)
    usage = combine_usage(model_usage, usage_from_report(report)) if report is not None else model_usage

    schema = compute_schema_profile(model, usage)
    data = compute_data_profile(model, executor) if executor is not None else None

    findings = run_rules(model, data_profile=data, usage=usage, rules=rules)

    report_profile: Optional[ReportProfile] = None
    if report is not None:
        report_profile = compute_report_profile(report)
        findings.extend(run_report_rules(report, model=model, rules=report_rules))

    return ProfileResult(
        model_name=model.name,
        source_kind=model.source_kind,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        schema=schema,
        data=data,
        report=report_profile,
        findings=findings,
    )
