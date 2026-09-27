"""Renders a `ProfileResult` into a single, dependency-free, self-contained
HTML report (no external CSS/JS/fonts, works offline, readable in light or
dark browser themes)."""
from __future__ import annotations

from html import escape

from ..profile_runner import ProfileResult

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

_STYLE = """
:root { color-scheme: light dark; }
body {
	font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
	margin: 0; padding: 2rem; line-height: 1.5;
	background: Canvas; color: CanvasText;
}
h1 { margin-bottom: 0.1rem; }
.subtitle { color: GrayText; margin-top: 0; margin-bottom: 1.5rem; }
.cards { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 2rem; }
.card {
	border: 1px solid color-mix(in srgb, CanvasText 20%, transparent);
	border-radius: 8px; padding: 0.75rem 1.1rem; min-width: 8rem;
}
.card .n { font-size: 1.6rem; font-weight: 700; display: block; }
.card .l { color: GrayText; font-size: 0.85rem; }
table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid color-mix(in srgb, CanvasText 15%, transparent); }
th { color: GrayText; font-weight: 600; }
tr:hover td { background: color-mix(in srgb, CanvasText 6%, transparent); }
.sev { font-weight: 700; padding: 0.1rem 0.5rem; border-radius: 4px; font-size: 0.78rem; }
.sev-error { background: #e5484d; color: white; }
.sev-warning { background: #f5a623; color: black; }
.sev-info { background: #6b7280; color: white; }
section { margin-bottom: 2.5rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9em; }
.muted { color: GrayText; }
"""


def _card(value, label) -> str:
    return f'<div class="card"><span class="n">{value}</span><span class="l">{escape(label)}</span></div>'


def _sev_badge(sev: str) -> str:
    return f'<span class="sev sev-{escape(sev)}">{escape(sev.upper())}</span>'


def _findings_table(findings) -> str:
    if not findings:
        return "<p class='muted'>No findings.</p>"
    ordered = sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.category, f.rule_id))
    rows = "\n".join(
        f"<tr><td>{_sev_badge(f.severity)}</td><td>{escape(f.category)}</td>"
        f"<td><code>{escape(f.rule_id)}</code></td><td>{escape(f.object_type)}</td>"
        f"<td><code>{escape(f.object_name)}</code></td><td>{escape(f.message)}</td></tr>"
        for f in ordered
    )
    return (
        "<table><thead><tr><th>Severity</th><th>Category</th><th>Rule</th>"
        f"<th>Type</th><th>Object</th><th>Message</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _tables_table(schema) -> str:
    rows = "\n".join(
        f"<tr><td><code>{escape(t.name)}</code></td><td>{'yes' if t.is_hidden else ''}</td>"
        f"<td>{'yes' if t.has_description else ''}</td><td>{t.column_count}</td>"
        f"<td>{t.calculated_column_count}</td><td>{t.measure_count}</td>"
        f"<td>{t.hierarchy_count}</td><td>{escape(', '.join(t.partition_modes) or '—')}</td></tr>"
        for t in schema.tables
    )
    return (
        "<table><thead><tr><th>Table</th><th>Hidden</th><th>Has description</th>"
        "<th>Columns</th><th>Calc. columns</th><th>Measures</th><th>Hierarchies</th>"
        f"<th>Partition mode(s)</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _data_profile_tables(data) -> str:
    if not data or not data.tables:
        return ""
    sections = []
    for t in data.tables:
        if t.error:
            body = f"<p class='muted'>Row count: {t.row_count if t.row_count is not None else '—'}. Error: {escape(t.error)}</p>"
        else:
            rows = "\n".join(
                f"<tr><td><code>{escape(c.column)}</code></td>"
                f"<td>{c.distinct_count if c.distinct_count is not None else '—'}</td>"
                f"<td>{c.null_count if c.null_count is not None else '—'}</td>"
                f"<td>{c.null_percentage if c.null_percentage is not None else '—'}</td>"
                f"<td>{escape(str(c.min_value)) if c.min_value is not None else '—'}</td>"
                f"<td>{escape(str(c.max_value)) if c.max_value is not None else '—'}</td></tr>"
                for c in t.columns
            )
            body = (
                "<table><thead><tr><th>Column</th><th>Distinct</th><th>Nulls</th>"
                f"<th>Null %</th><th>Min</th><th>Max</th></tr></thead><tbody>{rows}</tbody></table>"
            )
        sections.append(f"<h3><code>{escape(t.table)}</code> — {t.row_count if t.row_count is not None else '—'} rows</h3>{body}")
    return "\n".join(sections)


def _report_section(report) -> str:
    if report is None:
        return "<section><h2>Report visuals</h2><p class='muted'>Not computed (no report was provided).</p></section>"

    type_rows = "".join(
        f"<tr><td><code>{escape(t)}</code></td><td>{n}</td></tr>"
        for t, n in sorted(report.visuals_by_type.items(), key=lambda kv: -kv[1])
    )
    type_table = (
        "<table><thead><tr><th>Visual type</th><th>Count</th></tr></thead>"
        f"<tbody>{type_rows}</tbody></table>"
    )

    page_rows = "".join(
        f"<tr><td><code>{escape(p.name)}</code></td><td>{escape(p.display_name or '')}</td>"
        f"<td>{'yes' if p.is_hidden else ''}</td><td>{p.visual_count}</td></tr>"
        for p in report.pages
    )
    pages_table = (
        "<table><thead><tr><th>Page</th><th>Display name</th><th>Hidden</th>"
        f"<th>Visuals</th></tr></thead><tbody>{page_rows}</tbody></table>"
    )

    visual_rows = "".join(
        f"<tr><td><code>{escape(v.page)}</code></td><td><code>{escape(v.name)}</code></td>"
        f"<td>{escape(v.visual_type or '')}</td><td>{escape(v.title or '')}</td>"
        f"<td>{'yes' if v.is_hidden else ''}</td><td>{v.field_count}</td></tr>"
        for v in report.visuals
    )
    visuals_table = (
        "<table><thead><tr><th>Page</th><th>Visual</th><th>Type</th><th>Title</th>"
        f"<th>Hidden</th><th>Fields used</th></tr></thead><tbody>{visual_rows}</tbody></table>"
    )

    return (
        "<section><h2>Report visuals</h2>"
        f"<p class='subtitle'>{escape(report.report_name)} &middot; source: {escape(report.source_kind)} &middot; "
        f"{report.page_count} pages, {report.visual_count} visuals</p>"
        f"<h3>Visual types</h3>{type_table}"
        f"<h3>Pages</h3>{pages_table}"
        f"<h3>Visuals</h3>{visuals_table}"
        "</section>"
    )


def render_html(result: ProfileResult) -> str:
    schema = result.schema
    sev_counts = {"error": 0, "warning": 0, "info": 0}
    for f in result.findings:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1

    cards = "".join(
        [
            _card(schema.table_count, "Tables"),
            _card(schema.column_count, "Columns"),
            _card(schema.calculated_column_count, "Calculated columns"),
            _card(schema.measure_count, "Measures"),
            _card(schema.relationship_count, "Relationships"),
            _card(schema.role_count, "Roles"),
            _card(schema.hierarchy_count, "Hierarchies"),
            _card(sev_counts["error"], "Errors"),
            _card(sev_counts["warning"], "Warnings"),
            _card(sev_counts["info"], "Info findings"),
        ]
        + (
            [_card(result.report.page_count, "Report pages"), _card(result.report.visual_count, "Report visuals")]
            if result.report is not None
            else []
        )
    )

    data_section = ""
    if result.data is not None:
        data_section = f"<section><h2>Data profile</h2>{_data_profile_tables(result.data)}</section>"
    else:
        data_section = (
            "<section><h2>Data profile</h2>"
            "<p class='muted'>Not computed (no live query connection was provided).</p></section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(result.model_name)} — semantic model profile</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>{escape(result.model_name)}</h1>
<p class="subtitle">Source: {escape(result.source_kind)} &middot; Generated {escape(result.generated_at)}</p>

<section class="cards">{cards}</section>

<section>
<h2>Best-practice findings</h2>
{_findings_table(result.findings)}
</section>

<section>
<h2>Tables</h2>
{_tables_table(schema)}
</section>

{data_section}

{_report_section(result.report)}

</body>
</html>
"""
