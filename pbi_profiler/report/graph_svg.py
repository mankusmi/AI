"""Hand-rolled, dependency-free SVG rendering for the two dependency
diagrams: measure-to-measure references (a layered DAG) and table
relationships (a circular layout, since relationships aren't guaranteed
hierarchical/acyclic).

No JS, no vendored libraries, no layout engine: everything here is plain
geometry, kept deterministic (sorted iteration everywhere) so it's testable
with plain string assertions. Colors/fonts aren't set here -- the emitted
markup only uses classes (`dep-node`, `dep-edge`, `dep-edge-inactive`,
`dep-label`), styled by `report/html.py`'s existing `_STYLE` block so the
diagrams follow the report's light/dark theme like everything else in it.
"""
from __future__ import annotations

import math
from html import escape
from typing import TYPE_CHECKING, Optional

from ..model import Relationship

if TYPE_CHECKING:
    from ..profiling.graph import MeasureDependencyEdge

_COLUMN_GAP = 60.0
_ROW_HEIGHT = 56.0
_NODE_HEIGHT = 34.0
_NODE_PAD_X = 14.0
_CHAR_WIDTH = 7.2
_MARGIN = 24.0

def _arrow_marker_defs(marker_id: str) -> str:
    # Each diagram gets its own marker id: some browsers resolve url(#id)
    # fragment references against the whole HTML document rather than the
    # nearest enclosing <svg>, so two inline <svg> roots both defining
    # id="dep-arrow" could otherwise make the first one win for both.
    return (
        f'<defs><marker id="{marker_id}" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" /></marker></defs>'
    )


def _box_width(label: str) -> float:
    return max(90.0, len(label) * _CHAR_WIDTH + 2 * _NODE_PAD_X)


def _compute_layers(nodes: set[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """layer(node) = 1 + max(layer(p) for p in predecessors(node)), else 0.

    ``predecessors(node)`` are the nodes with an edge pointing *into* node
    (things that reference it), so a node nothing depends on sits at layer 0
    (drawn leftmost) and dependents cascade rightward -- edges then always
    point left-to-right by construction.

    Defensive against cycles (DAX disallows circular measure references, so
    real input is always a DAG, but a malformed/synthetic edge list
    shouldn't hang): a node revisited while still being computed just
    contributes 0 to its dependent's layer instead of recursing forever.
    """
    predecessors: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        if b in predecessors:
            predecessors[b].append(a)

    layers: dict[str, int] = {}
    visiting: set[str] = set()

    def layer_of(node: str) -> int:
        if node in layers:
            return layers[node]
        if node in visiting:
            return 0
        visiting.add(node)
        preds = predecessors.get(node, [])
        result = 1 + max((layer_of(p) for p in preds), default=-1)
        visiting.discard(node)
        layers[node] = result
        return result

    for n in sorted(nodes):
        layer_of(n)
    return layers


def _group_parallel(items: list[tuple[str, str, object]]) -> list[tuple[str, str, object, int, int]]:
    """Group items by their unordered (a, b) node pair -> (a, b, payload,
    index_within_group, group_size), so multiple edges between the same two
    nodes fan out instead of overlapping. Deterministic: group order follows
    each pair's first occurrence in ``items``."""
    groups: dict[frozenset, list[tuple[str, str, object]]] = {}
    for a, b, payload in items:
        groups.setdefault(frozenset((a, b)), []).append((a, b, payload))
    result = []
    for group in groups.values():
        count = len(group)
        for i, (a, b, payload) in enumerate(group):
            result.append((a, b, payload, i, count))
    return result


def _bezier_path(x1: float, y1: float, x2: float, y2: float, index: int, count: int) -> str:
    if count <= 1:
        return f"M {x1:.1f} {y1:.1f} L {x2:.1f} {y2:.1f}"
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    px, py = -dy / length, dx / length  # unit vector perpendicular to the line
    offset = (index - (count - 1) / 2) * 18.0
    mx, my = (x1 + x2) / 2 + px * offset, (y1 + y2) / 2 + py * offset
    return f"M {x1:.1f} {y1:.1f} Q {mx:.1f} {my:.1f} {x2:.1f} {y2:.1f}"


def _node_markup(name: str, x: float, y: float, width: float) -> str:
    return (
        f'<g class="dep-node"><rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{_NODE_HEIGHT:.0f}" rx="6" />'
        f'<text x="{x + width / 2:.1f}" y="{y + _NODE_HEIGHT / 2:.1f}" text-anchor="middle" '
        f'dominant-baseline="central">{escape(name)}</text></g>'
    )


def render_measure_dependency_svg(edges: list[MeasureDependencyEdge]) -> Optional[str]:
    """A layered left-to-right DAG: an arrow from measure A to measure B
    means "A references B". Returns None if there are no edges to draw."""
    if not edges:
        return None

    pairs = [(e.from_measure, e.to_measure) for e in edges]
    nodes = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    layers = _compute_layers(set(nodes), pairs)

    layer_groups: dict[int, list[str]] = {}
    for n in nodes:
        layer_groups.setdefault(layers[n], []).append(n)

    layer_width = {l: max(_box_width(n) for n in ns) for l, ns in layer_groups.items()}
    col_x: dict[int, float] = {}
    x = _MARGIN
    for l in range(max(layer_groups) + 1):
        col_x[l] = x
        x += layer_width.get(l, 90.0) + _COLUMN_GAP

    positions: dict[str, tuple[float, float, float]] = {}
    for l, ns in layer_groups.items():
        for i, n in enumerate(ns):
            positions[n] = (col_x[l], _MARGIN + i * _ROW_HEIGHT, _box_width(n))

    total_width = x - _COLUMN_GAP + _MARGIN
    total_height = _MARGIN * 2 + max(len(ns) for ns in layer_groups.values()) * _ROW_HEIGHT

    parts = [
        f'<svg class="dep-graph" viewBox="0 0 {total_width:.0f} {total_height:.0f}" '
        f'xmlns="http://www.w3.org/2000/svg">',
        _arrow_marker_defs("dep-arrow-measure"),
    ]

    for a, b, _edge, index, count in _group_parallel([(a, b, None) for a, b in pairs]):
        ax, ay, aw = positions[a]
        bx, by, _bw = positions[b]
        x1, y1 = ax + aw, ay + _NODE_HEIGHT / 2
        x2, y2 = bx, by + _NODE_HEIGHT / 2
        path = _bezier_path(x1, y1, x2, y2, index, count)
        parts.append(f'<path class="dep-edge" d="{path}" marker-end="url(#dep-arrow-measure)" fill="none" />')

    for n in nodes:
        nx, ny, nw = positions[n]
        parts.append(_node_markup(n, nx, ny, nw))

    parts.append("</svg>")
    return "".join(parts)


def render_relationship_svg(relationships: list[Relationship]) -> Optional[str]:
    """Tables placed evenly around a circle (relationships aren't guaranteed
    acyclic/hierarchical, e.g. snowflake schemas or a shared date table with
    multiple relationships, so a fixed layout beats trying to detect one).
    Inactive relationships are dashed; bidirectional ones get arrowheads at
    both ends; cardinality (when known) is shown as an edge label. Returns
    None if there are no relationships to draw."""
    if not relationships:
        return None

    nodes = sorted({r.from_table for r in relationships} | {r.to_table for r in relationships})
    n = len(nodes)
    radius = max(120.0, n * 34.0)
    center = radius + _MARGIN + 50.0

    positions: dict[str, tuple[float, float, float]] = {}
    for i, table in enumerate(nodes):
        angle = (2 * math.pi * i / n) if n > 1 else 0.0
        cx = center + radius * math.cos(angle)
        cy = center + radius * math.sin(angle)
        w = _box_width(table)
        positions[table] = (cx - w / 2, cy - _NODE_HEIGHT / 2, w)

    size = center * 2 + 40.0

    parts = [
        f'<svg class="dep-graph" viewBox="0 0 {size:.0f} {size:.0f}" xmlns="http://www.w3.org/2000/svg">',
        _arrow_marker_defs("dep-arrow-rel"),
    ]

    items = [(r.from_table, r.to_table, r) for r in relationships]
    for a, b, rel, index, count in _group_parallel(items):
        ax, ay, aw = positions[a]
        bx, by, bw = positions[b]
        ax_c, ay_c = ax + aw / 2, ay + _NODE_HEIGHT / 2
        bx_c, by_c = bx + bw / 2, by + _NODE_HEIGHT / 2
        path = _bezier_path(ax_c, ay_c, bx_c, by_c, index, count)
        css_class = "dep-edge" if rel.is_active else "dep-edge dep-edge-inactive"
        markers = 'marker-end="url(#dep-arrow-rel)"'
        if rel.cross_filtering_behavior == "bothDirections":
            markers += ' marker-start="url(#dep-arrow-rel)"'
        parts.append(f'<path class="{css_class}" d="{path}" {markers} fill="none" />')

        if rel.from_cardinality and rel.to_cardinality:
            label = f"{rel.from_cardinality}:{rel.to_cardinality}"
            mx, my = (ax_c + bx_c) / 2, (ay_c + by_c) / 2
            label_w = len(label) * 6.0 + 8.0
            parts.append(
                f'<rect class="dep-label-bg" x="{mx - label_w / 2:.1f}" y="{my - 8:.1f}" '
                f'width="{label_w:.1f}" height="16" rx="3" />'
                f'<text class="dep-label" x="{mx:.1f}" y="{my:.1f}" text-anchor="middle" '
                f'dominant-baseline="central">{escape(label)}</text>'
            )

    for table in nodes:
        tx, ty, tw = positions[table]
        parts.append(_node_markup(table, tx, ty, tw))

    parts.append("</svg>")
    return "".join(parts)
