"""Orchestrates loading a model, running the schema/data profilers and the
rule engine, and bundling the results into one serializable `ProfileResult`.
This is the shared entry point used by the CLI (and importable directly from
scripts/notebooks)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .loaders.base import QueryExecutor
from .model import Model
from .profiling import (
    DataProfile,
    Finding,
    Rule,
    SchemaProfile,
    analyze_model,
    compute_data_profile,
    compute_schema_profile,
    run_rules,
)


@dataclass
class ProfileResult:
    model_name: str
    source_kind: str
    generated_at: str
    schema: SchemaProfile
    data: Optional[DataProfile]
    findings: list[Finding]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "source_kind": self.source_kind,
            "generated_at": self.generated_at,
            "schema": self.schema.to_dict(),
            "data": self.data.to_dict() if self.data else None,
            "findings": [f.to_dict() for f in self.findings],
        }


def run_profile(
    model: Model,
    executor: Optional[QueryExecutor] = None,
    rules: Optional[list[Rule]] = None,
) -> ProfileResult:
    usage = analyze_model(model)
    schema = compute_schema_profile(model, usage)
    data = compute_data_profile(model, executor) if executor is not None else None
    findings = run_rules(model, data_profile=data, usage=usage, rules=rules)

    return ProfileResult(
        model_name=model.name,
        source_kind=model.source_kind,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        schema=schema,
        data=data,
        findings=findings,
    )
