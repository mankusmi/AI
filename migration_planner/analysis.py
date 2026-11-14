"""Build the datapoint shopping list from dashboards + their semantic models.

A *datapoint* is a model column or measure that a visual uses. Measures are
followed through their DAX to the underlying columns (best-effort regex, see
`pbi_profiler.profiling.dax_deps`), because those columns are what the new
system must actually supply.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pbi_profiler.loaders import BimModelLoader, TmdlModelLoader
from pbi_profiler.model import Model
from pbi_profiler.profiling.dax_deps import find_references
from pbi_profiler.report_loaders import LegacyLayoutReportLoader, PbirReportLoader

from .manifest import DashboardEntry


@dataclass
class Datapoint:
    key: str  # canonical qualified name, e.g. Sales[Amount]
    model: str
    table: str
    name: str
    kind: str  # column | measure
    data_type: str = ""
    format_string: str = ""
    legacy_source: str = ""  # source column for columns, DAX for measures
    description: str = ""
    aliases: set[str] = field(default_factory=set)
    # dashboard -> number of visuals using it directly (0 = only via a measure)
    visual_counts: dict[str, int] = field(default_factory=dict)
    via_measures: set[str] = field(default_factory=set)  # measures that depend on this column
    roles: set[str] = field(default_factory=set)
    unresolved: bool = False


@dataclass
class DashboardInfo:
    entry: DashboardEntry
    pages: int = 0
    visuals: int = 0
    datapoints: set[str] = field(default_factory=set)


@dataclass
class Analysis:
    dashboards: list[DashboardInfo]
    datapoints: dict[str, Datapoint]
    status_of: dict[str, str]  # dashboard -> status

    def _statuses(self, dp: Datapoint) -> set[str]:
        return {self.status_of[d] for d in dp.visual_counts}

    def shopping_list(self) -> list[dict]:
        rows = []
        for dp in self.datapoints.values():
            st = self._statuses(dp)
            if "replace" not in st:
                continue
            rep = sorted(d for d in dp.visual_counts if self.status_of[d] == "replace")
            stay = sorted(d for d in dp.visual_counts if self.status_of[d] == "stay")
            visuals = sum(dp.visual_counts[d] for d in rep)
            n = len(rep)
            rows.append({
                "datapoint": dp.key, "model": dp.model, "table": dp.table, "name": dp.name, "kind": dp.kind,
                "data_type": dp.data_type, "definition": dp.legacy_source,
                "also_known_as": "; ".join(sorted(dp.aliases)),
                "needed_by": "; ".join(rep), "dashboards_replaced": n, "visuals": visuals,
                "also_used_by_staying": "; ".join(stay),
                "decision": "SHARED - decide source" if stay else "request from new system",
                "priority": "High" if n >= 3 else "Medium" if n == 2 else "Low",
                "required_via_measures": "; ".join(sorted(dp.via_measures)),
                "grain": "", "freshness": "", "new_system_field": "", "status": "open", "comments": "",
            })
        order = {"High": 0, "Medium": 1, "Low": 2}
        rows.sort(key=lambda r: (order[r["priority"]], -r["dashboards_replaced"], -r["visuals"], r["datapoint"]))
        return rows

    def inventory(self) -> list[dict]:
        out = []
        for di in self.dashboards:
            e = di.entry
            out.append({
                "dashboard": e.dashboard, "status": e.status, "owner": e.owner, "pages": di.pages,
                "visuals": di.visuals, "datapoints": len(di.datapoints), "notes": e.notes,
            })
        return out

    def stay_only(self) -> list[dict]:
        """Datapoints only used by staying dashboards: legacy keeps feeding them."""
        return [{"datapoint": dp.key, "kind": dp.kind, "used_by": "; ".join(sorted(dp.visual_counts))}
                for dp in self.datapoints.values()
                if self._statuses(dp) == {"stay"}]


def _load_model(e: DashboardEntry) -> Model:
    return (TmdlModelLoader if e.model_source == "tmdl" else BimModelLoader)(e.model_path).load()


def _load_report(e: DashboardEntry):
    return (PbirReportLoader if e.report_source == "pbir" else LegacyLayoutReportLoader)(e.report_path).load()


def _find_measure(model: Model, table: Optional[str], name: str):
    if table:
        t = model.get_table(table)
        m = t.get_measure(name) if t else None
        if m:
            return m
    return next((m for m in model.all_measures() if m.name == name), None)


def _find_column(model: Model, table: Optional[str], name: str):
    t = model.get_table(table) if table else None
    return t.get_column(name) if t else None


def analyze(entries: list[DashboardEntry], aliases: Optional[dict[str, str]] = None) -> Analysis:
    aliases = aliases or {}
    dps: dict[str, Datapoint] = {}
    infos: list[DashboardInfo] = []
    model_cache: dict[tuple, Model] = {}

    def canon(model_name: str, table: str, name: str) -> tuple[str, str]:
        q = f"{table}[{name}]"
        return aliases.get(q, q), q

    def get_dp(model: Model, table: str, name: str, kind: str, meas=None, col=None) -> Datapoint:
        key, raw = canon(model.name, table, name)
        dp = dps.get(key)
        if dp is None:
            dp = Datapoint(key=key, model=model.name, table=table, name=name, kind=kind)
            if meas is not None:
                dp.legacy_source = (meas.expression or "").strip()
                dp.format_string = meas.format_string or ""
                dp.description = meas.description or ""
            elif col is not None:
                dp.data_type = col.data_type or ""
                dp.format_string = col.format_string or ""
                dp.description = col.description or ""
                dp.legacy_source = (col.expression or "").strip() if col.is_calculated else (col.source_column or col.name)
            else:
                dp.unresolved = True
            dps[key] = dp
        if raw != key:
            dp.aliases.add(raw)
        return dp

    for e in entries:
        mk = (e.model_source, str(e.model_path))
        if mk not in model_cache:
            model_cache[mk] = _load_model(e)
        model = model_cache[mk]
        report = _load_report(e)
        info = DashboardInfo(entry=e, pages=len(report.pages), visuals=len(report.all_visuals()))
        infos.append(info)

        def add(dp: Datapoint, direct: int = 0, via: Optional[str] = None, role: Optional[str] = None):
            dp.visual_counts[e.dashboard] = dp.visual_counts.get(e.dashboard, 0) + direct
            info.datapoints.add(dp.key)
            if via:
                dp.via_measures.add(via)
            if role:
                dp.roles.add(role)

        def expand_measure(m, top: str, seen: set):
            """Add the columns/measures a measure depends on."""
            if m.qualified_name in seen:
                return
            seen.add(m.qualified_name)
            q, bare = find_references(m.expression or "")
            for t, c in sorted(q):
                col = _find_column(model, t, c)
                mm = None if col else _find_measure(model, t, c)
                if col:
                    add(get_dp(model, t, c, "column", col=col), via=top)
                elif mm:
                    add(get_dp(model, mm.table, mm.name, "measure", meas=mm), via=top)
                    expand_measure(mm, top, seen)
            for b in sorted(bare):
                mm = _find_measure(model, m.table, b)
                if mm:
                    add(get_dp(model, mm.table, mm.name, "measure", meas=mm), via=top)
                    expand_measure(mm, top, seen)
                else:
                    col = _find_column(model, m.table, b)
                    if col:
                        add(get_dp(model, m.table, b, "column", col=col), via=top)

        for v in report.all_visuals():
            used = set()
            for f in v.fields:
                if not f.field:
                    continue
                table = f.table or ""
                meas = _find_measure(model, f.table, f.field) if f.kind == "measure" else None
                if meas:
                    dp = get_dp(model, meas.table, meas.name, "measure", meas=meas)
                    expand_measure(meas, dp.key, set())
                else:
                    col = _find_column(model, f.table, f.field)
                    dp = get_dp(model, table, f.field, "column", col=col)
                if dp.key not in used:
                    used.add(dp.key)
                    add(dp, direct=1, role=f.role)
    status_of = {i.entry.dashboard: i.entry.status for i in infos}
    # retired dashboards drive no demand
    for dp in dps.values():
        for d in [d for d in dp.visual_counts if status_of[d] == "retire"]:
            del dp.visual_counts[d]
    dps = {k: v for k, v in dps.items() if v.visual_counts}
    return Analysis(dashboards=infos, datapoints=dps, status_of=status_of)
