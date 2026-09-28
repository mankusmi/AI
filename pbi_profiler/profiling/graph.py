"""Measure dependency edges, for the "measure dependency graph" diagram in
the HTML report. Reuses `dax_deps.find_references` (the same regex-based DAX
reference scanner `analyze_model` uses for the flat usage set) but keeps
per-measure edges instead of collapsing them into one usage set."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..model import Model, Relationship
from .dax_deps import find_references


@dataclass(frozen=True)
class MeasureDependencyEdge:
    from_measure: str  # qualified name, e.g. "Sales[Margin Pct]"
    to_measure: str


@dataclass
class DependencyGraph:
    measure_edges: list[MeasureDependencyEdge] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def build_measure_dependency_edges(model: Model) -> list[MeasureDependencyEdge]:
    """One edge per (measure, other measure it references by name). Doesn't
    resolve which specific table a bare `[Name]` reference "meant" -- if two
    tables have a measure with the same name, both are linked, same
    ambiguity-tolerant approach `analyze_model` uses for `UsageInfo`."""
    names_to_qualified: dict[str, list[str]] = {}
    for measure in model.all_measures():
        names_to_qualified.setdefault(measure.name, []).append(measure.qualified_name)

    edges: set[MeasureDependencyEdge] = set()
    for table in model.tables:
        for measure in table.measures:
            if not measure.expression:
                continue
            _, bare = find_references(measure.expression)
            for name in bare:
                for target in names_to_qualified.get(name, []):
                    if target != measure.qualified_name:
                        edges.add(MeasureDependencyEdge(measure.qualified_name, target))

    return sorted(edges, key=lambda e: (e.from_measure, e.to_measure))
