"""Shared parsing of Power BI's "query expression" JSON shape for field
references (`Column` / `Measure` / `Aggregation` / `HierarchyLevel`), used by
both the PBIR and legacy-layout report loaders. The two formats differ only
in how a `SourceRef` resolves to a table name (PBIR names the table
directly; the legacy format uses a `From`-clause alias) -- callers supply an
`entity_resolver` for that step.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..report_model import FieldRef

EntityResolver = Callable[[dict[str, Any]], Optional[str]]


def extract_field(field_obj: Any, resolve_entity: EntityResolver, role: Optional[str] = None) -> Optional[FieldRef]:
    """Best-effort extraction of a FieldRef from a query expression object.
    Returns None for shapes we don't recognize (e.g. constants/literals used
    as a "field") rather than raising, since this is inherently a best-effort
    static read of an internal, not-fully-documented JSON format.
    """
    if not isinstance(field_obj, dict):
        return None

    if "Column" in field_obj:
        col = field_obj["Column"]
        table = resolve_entity(col.get("Expression", {}))
        return FieldRef(table=table, field=col.get("Property"), kind="column", role=role)

    if "Measure" in field_obj:
        m = field_obj["Measure"]
        table = resolve_entity(m.get("Expression", {}))
        return FieldRef(table=table, field=m.get("Property"), kind="measure", role=role)

    if "Aggregation" in field_obj:
        inner = field_obj["Aggregation"].get("Expression", {})
        ref = extract_field(inner, resolve_entity, role=role)
        if ref is not None:
            ref.kind = "aggregation"
        return ref

    if "HierarchyLevel" in field_obj:
        hl = field_obj["HierarchyLevel"]
        hierarchy_expr = hl.get("Expression", {}).get("Hierarchy", {})
        table = resolve_entity(hierarchy_expr.get("Expression", {}))
        return FieldRef(table=table, field=hl.get("Level"), kind="hierarchy_level", role=role)

    return None


def extract_title(objects: Optional[dict[str, Any]]) -> Optional[str]:
    """Shared shape for a visual's title, whether stored under PBIR's
    `visual.objects.title` or the legacy format's `singleVisual.vcObjects.title`:
    `[{"properties": {"text": {"expr": {"Literal": {"Value": "'My Title'"}}}}}]`.
    Returns None for a dynamic (expression-bound) title, which this static
    read can't evaluate."""
    title_objs = (objects or {}).get("title")
    if not title_objs:
        return None
    try:
        value = title_objs[0]["properties"]["text"]["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return None
    return unquote_dax_string_literal(value)


def unquote_dax_string_literal(value: Optional[str]) -> Optional[str]:
    """DAX string literals in these JSON blobs are stored as `'text'`
    (single-quoted, with `''` as an escaped quote)."""
    if value is None:
        return None
    v = value.strip()
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v
