"""Per-entity Power Query transformation settings and the pipeline that loading actually runs."""
from __future__ import annotations

from typing import Optional

from .m2sql import Pipeline, Translator, register_udfs
from .store import read_blob
from .mapping_load import date_order_for_culture, table_for


def dataflow_queries(con, dataflow_id: str) -> dict[str, str]:
    return dict(con.execute("SELECT name, m_query FROM dataflow_queries WHERE dataflow_id = ?", [dataflow_id]).fetchall())


def bindings(con, dataflow_id: str) -> dict[str, str]:
    return dict(con.execute("SELECT query_name, table_name FROM query_bindings WHERE dataflow_id = ?", [dataflow_id]).fetchall())


def entity_tables(con, dataflow_id: str) -> dict[str, str]:
    """Entities of this dataflow that already have a loaded ``df_`` table (usable as lookup sources)."""
    out = {}
    for (ent,) in con.execute("SELECT entity FROM dataflow_entities WHERE dataflow_id = ?", [dataflow_id]).fetchall():
        tbl = table_for(con, ent)
        if con.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [tbl]).fetchone():
            out[ent] = tbl
    return out


def settings(con, entity: str) -> Optional[dict]:
    r = con.execute("SELECT use_m, override_sql, accept_partial, extra_inputs, date_order FROM entity_transforms "
                    "WHERE entity = ?", [entity]).fetchone()
    return None if not r else {"use_m": r[0], "override_sql": r[1] or "", "accept_partial": bool(r[2]),
                               "extra_inputs": r[3] or [], "date_order": r[4] or "auto"}


def save_settings(con, dataflow_id: str, entity: str, use_m: bool, override_sql: str, accept_partial: bool,
                  extra_inputs: list[str], date_order: str = "auto") -> None:
    if date_order not in ("auto", "DMY", "MDY"):
        raise ValueError("date_order must be auto, DMY or MDY")
    con.execute("DELETE FROM entity_transforms WHERE entity = ?", [entity])
    con.execute("INSERT INTO entity_transforms (entity, dataflow_id, use_m, override_sql, accept_partial, extra_inputs, "
                "date_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [entity, dataflow_id, use_m, override_sql.strip(), accept_partial, extra_inputs, date_order])


def date_order(con, dataflow_id: str, entity: str) -> tuple[str, str, str]:
    """(order, culture, source): 'DMY'/'MDY' from the entity setting, else from the dataflow culture."""
    st = settings(con, entity)
    row = con.execute("SELECT culture FROM dataflows WHERE dataflow_id = ?", [dataflow_id]).fetchone()
    culture = (row[0] if row else "") or ""
    if st and st["date_order"] in ("DMY", "MDY"):
        return st["date_order"], culture, "setting"
    return date_order_for_culture(culture), culture, "culture"


def translate(con, dataflow_id: str, entity: str) -> Pipeline:
    return Translator(dataflow_queries(con, dataflow_id), bindings(con, dataflow_id),
                      entity_tables(con, dataflow_id), date_order(con, dataflow_id, entity)[0]).translate(entity)


def resolve(con, dataflow_id: str, entity: str) -> dict:
    """Decide what a load will run: {mode: 'm'|'attribute', sql, inputs, status, pipeline, ...}."""
    p = translate(con, dataflow_id, entity)
    st = settings(con, entity)
    use = (st["use_m"] if st else p.complete)            # no explicit choice: auto-on only if fully translated
    extra = (st or {}).get("extra_inputs", [])
    order, culture, order_src = date_order(con, dataflow_id, entity)
    out = {"pipeline": p, "settings": st, "translated": p.complete, "use_m": bool(use), "mode": "attribute",
           "date_order": order, "culture": culture, "date_order_source": order_src,
           "sql": None, "inputs": [], "status": "disabled", "generated_sql": p.sql() if len(p.ctes) > 1 else "",
           "dynamic_columns": p.dynamic_columns}
    if not use:
        return out
    inputs = list(dict.fromkeys(p.inputs + extra))
    if st and st["override_sql"]:
        out.update(mode="m", sql=st["override_sql"], inputs=inputs, status="override")
    elif p.complete:
        out.update(mode="m", sql=p.sql(), inputs=inputs, status="complete")
    elif st and st["accept_partial"] and len(p.ctes) > 1:
        out.update(mode="m", sql=p.sql(), inputs=inputs, status="partial (accepted)")
    else:
        out.update(status="blocked: " + (p.error or "some steps are not supported; edit the SQL or accept partial"))
    return out


def output_columns(cur, plan: dict) -> list[tuple[str, str]]:
    register_udfs(cur)
    from .m2sql import stage_table
    stage_table(cur, plan["inputs"], [])
    return [(r[0], r[1]) for r in cur.execute(f"SELECT column_name, column_type FROM (DESCRIBE {plan['sql']})").fetchall()]


def stage_sample(con, plan: dict, entity: str, layout_hash: str, sample_rows: int = 500) -> int:
    """Stage the first rows of the first stored sheet with this layout (input mapping applied). Returns row count."""
    from .m2sql import sheet_columns, stage_table, stage_value
    from .mapping_load import _norm_header_cells, _sheet_rows
    row = con.execute("""SELECT f.sha256, s.sheet, s.header_row FROM sheets s JOIN files f USING (run_id, rel_path)
                         JOIN file_blobs b ON b.sha256 = f.sha256
                         WHERE s.layout_hash = ? AND s.header_row IS NOT NULL LIMIT 1""", [layout_hash]).fetchone()
    if not row:
        raise LookupError("No stored file with that layout (was it profiled with 'keep file bytes'?)")
    mapping = dict(con.execute("SELECT norm_header, attribute FROM column_mappings WHERE entity = ? AND layout_hash = ? "
                               "AND kind = 'input'", [entity, layout_hash]).fetchall())
    header, rows = _sheet_rows(read_blob(con, row[0]), row[1], row[2])
    cols = sheet_columns(header, plan["inputs"], mapping, _norm_header_cells(header), plan.get("dynamic_columns", False))
    staged = []
    for k, r in rows:
        vals = [stage_value(r[i]) if i is not None and i < len(r) else None for _, i in cols]
        if all(v is None or not v.strip() for v in vals):
            continue
        staged.append((k, *vals))
        if len(staged) >= sample_rows:
            break
    stage_table(con, [n for n, _ in cols], staged)
    return len(staged)


def import_reference(con, path: str, table_name: str = "", sheet: str = "") -> dict:
    """Load a CSV/Excel lookup file into ``ref_<name>`` (all columns VARCHAR) so a query can be bound to it."""
    import re
    from pathlib import Path
    from .inspect_excel import detect_header_row
    from .m2sql import q
    p = Path(path).expanduser()
    name = table_name or "ref_" + re.sub(r"[^0-9a-zA-Z]+", "_", p.stem).strip("_").lower()
    if not name.replace("_", "").isalnum():
        raise ValueError("Table name may only contain letters, digits and underscores")
    if p.suffix.lower() == ".csv":
        con.execute(f"CREATE OR REPLACE TABLE {q(name)} AS SELECT * FROM read_csv(?, all_varchar = true, header = true)", [str(p)])
    elif p.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        try:
            ws = wb[sheet] if sheet else wb.worksheets[0]
            all_rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
        hi = detect_header_row(all_rows[:50], 2)
        if hi is None:
            raise ValueError("Could not find a header row")
        hdr = [str(c).strip() if c is not None and str(c).strip() else f"Column{i + 1}" for i, c in enumerate(all_rows[hi])]
        from .m2sql import stage_value
        con.execute(f"CREATE OR REPLACE TABLE {q(name)} ({', '.join(q(h) + ' VARCHAR' for h in hdr)})")
        data = [[stage_value(v) for v in (r + (None,) * len(hdr))[:len(hdr)]] for r in all_rows[hi + 1:]
                if any(v is not None for v in r)]
        if data:
            con.executemany(f"INSERT INTO {q(name)} VALUES ({', '.join('?' for _ in hdr)})", data)
    else:
        raise ValueError("Only .csv, .xlsx and .xlsm reference files are supported")
    return {"table": name, "rows": con.execute(f"SELECT count(*) FROM {q(name)}").fetchone()[0],
            "columns": [r[0] for r in con.execute(f"SELECT column_name FROM (DESCRIBE {q(name)})").fetchall()]}
