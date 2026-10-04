"""Translate a Power Query (M) dataflow query into a DuckDB SQL pipeline.

The translated query reads a staged table ``src`` (one Excel sheet, columns named like the M code
expects) and ends in a relation whose columns are the entity's attributes. Every ``let`` step becomes
a CTE. Steps outside the supported subset are reported (never silently skipped) and the pipeline is
marked incomplete so the caller can require explicit acceptance or a hand-written SQL override.

Supported table steps: PromoteHeaders, RenameColumns, RemoveColumns, SelectColumns, ReorderColumns,
TransformColumnTypes, TransformColumns, AddColumn, ReplaceValue, SelectRows, Distinct, Sort, FirstN,
Skip/RemoveFirstN, FillDown, Combine, NestedJoin + ExpandTableColumn, Join, #table / FromRows.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from .mapping_load import date_order_for_culture
from .mparse import MParseError, parse_expr


class Unsupported(Exception):
    pass


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def slug(s: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower() or "x"


# ----------------------------------------------------------------------------- conversion macros
# Pure-SQL macros (not Python UDFs: those crash DuckDB when called from server threads). They mirror
# mapping_load.coerce(): thousands separators, currency symbols, (negatives), culture-ordered dates, Excel serials.
def _ts_macro(name: str, preferred: list[str], fallback: list[str]) -> str:
    return (f"""CREATE OR REPLACE MACRO {name}(x) AS CASE
        WHEN x IS NULL OR trim(x) = '' THEN NULL
        WHEN regexp_matches(trim(x), '^[0-9]{{5}}(\\.[0-9]+)?$')
            THEN TIMESTAMP '1899-12-30' + to_seconds(CAST(round(CAST(trim(x) AS DOUBLE) * 86400) AS BIGINT))
        WHEN regexp_matches(trim(x), '^[0-9]{{1,2}}[/.-][0-9]{{1,2}}[/.-][0-9]{{2,4}}')
            THEN COALESCE(try_strptime(trim(x), [{', '.join(preferred)}]),
                          try_strptime(trim(x), [{', '.join(fallback)}]))
        ELSE COALESCE(TRY_CAST(trim(x) AS TIMESTAMP),
                      try_strptime(trim(x), ['%Y/%m/%d', '%d %b %Y', '%d-%b-%Y', '%d %B %Y'])) END""")


_NUM = r"""CASE WHEN regexp_matches(trim(x), '^\(.*\)$')
        THEN '-' || regexp_replace(trim(x), '[()\s,£$€]', '', 'g')
        ELSE regexp_replace(trim(x), '[\s,£$€]', '', 'g') END"""
MACROS = [
    f"CREATE OR REPLACE MACRO sp_dbl(x) AS TRY_CAST({_NUM} AS DOUBLE)",
    f"CREATE OR REPLACE MACRO sp_dec(x) AS TRY_CAST({_NUM} AS DECIMAL(38,10))",
    "CREATE OR REPLACE MACRO sp_int(x) AS CASE WHEN sp_dec(x) IS NOT NULL AND sp_dec(x) = floor(sp_dec(x)) "
    "THEN CAST(sp_dec(x) AS BIGINT) END",
    _ts_macro("sp_ts_dmy", ["'%d/%m/%Y'", "'%d-%m-%Y'", "'%d.%m.%Y'", "'%d/%m/%y'", "'%d-%m-%y'", "'%d/%m/%Y %H:%M:%S'", "'%d/%m/%Y %H:%M'"],
              ["'%m/%d/%Y'", "'%m-%d-%Y'"]),
    _ts_macro("sp_ts_mdy", ["'%m/%d/%Y'", "'%m-%d-%Y'", "'%m.%d.%Y'", "'%m/%d/%y'", "'%m-%d-%y'", "'%m/%d/%Y %H:%M:%S'", "'%m/%d/%Y %H:%M'"],
              ["'%d/%m/%Y'", "'%d-%m-%Y'"]),
    "CREATE OR REPLACE MACRO sp_ts(x) AS sp_ts_dmy(x)",
    "CREATE OR REPLACE MACRO sp_tstz_dmy(x) AS CAST(sp_ts_dmy(x) AS TIMESTAMPTZ)",
    "CREATE OR REPLACE MACRO sp_tstz_mdy(x) AS CAST(sp_ts_mdy(x) AS TIMESTAMPTZ)",
    "CREATE OR REPLACE MACRO sp_bool(x) AS CASE lower(trim(x)) WHEN 'true' THEN TRUE WHEN 'yes' THEN TRUE "
    "WHEN 'y' THEN TRUE WHEN '1' THEN TRUE WHEN 'false' THEN FALSE WHEN 'no' THEN FALSE WHEN 'n' THEN FALSE "
    "WHEN '0' THEN FALSE END",
    "CREATE OR REPLACE MACRO sp_proper(s) AS array_to_string(list_transform(string_split(lower(s), ' '), "
    "w -> upper(w[1:1]) || w[2:]), ' ')",
]


def ensure_macros(cur) -> None:
    """Make sure the sp_* conversion macros exist (they live in the database; idempotent)."""
    for ddl in MACROS:
        cur.execute(ddl)


register_udfs = ensure_macros          # old name, kept for callers/tests


def _plain(fn):
    return lambda x, order: fn(x)


def _date_cast(x, order):
    return f"CAST(sp_ts_{order.lower()}(CAST({x} AS VARCHAR)) AS DATE)"


def _ts_cast(x, order):
    return f"sp_ts_{order.lower()}(CAST({x} AS VARCHAR))"


def _tstz_cast(x, order):
    return f"sp_tstz_{order.lower()}(CAST({x} AS VARCHAR))"


_dbl = _plain(lambda x: f"sp_dbl(CAST({x} AS VARCHAR))")
_int = _plain(lambda x: f"sp_int(CAST({x} AS VARCHAR))")
_dec = _plain(lambda x: f"sp_dec(CAST({x} AS VARCHAR))")
TYPE_SQL = {
    "text": _plain(lambda x: f"CAST({x} AS VARCHAR)"),
    "number": _dbl, "Number.Type": _dbl, "Percentage.Type": _dbl,
    "Int64.Type": _int, "Int32.Type": _int, "Int16.Type": _int,
    "Currency.Type": _dec, "Decimal.Type": _dec,
    "date": _date_cast, "Date.Type": _date_cast, "datetime": _ts_cast, "DateTime.Type": _ts_cast,
    "datetimezone": _tstz_cast, "DateTimeZone.Type": _tstz_cast,
    "logical": _plain(lambda x: f"sp_bool(CAST({x} AS VARCHAR))"),
    "Logical.Type": _plain(lambda x: f"sp_bool(CAST({x} AS VARCHAR))"),
    "any": _plain(lambda x: x),
}
TYPE_KIND = {"text": "text", "number": "num", "Number.Type": "num", "Percentage.Type": "num", "Int64.Type": "num",
             "Int32.Type": "num", "Int16.Type": "num", "Currency.Type": "num", "Decimal.Type": "num",
             "date": "date", "Date.Type": "date", "datetime": "date", "DateTime.Type": "date",
             "datetimezone": "date", "DateTimeZone.Type": "date", "logical": "bool", "Logical.Type": "bool"}


def type_name(node) -> str:
    if node["t"] in ("type", "id"):
        return node["name"]
    raise Unsupported("unsupported type expression")


def cast_type(sql: str, tname: str, order: str = "DMY") -> str:
    fn = TYPE_SQL.get(tname)
    if not fn:
        raise Unsupported(f"unsupported column type {tname!r}")
    return fn(sql, order)


# ----------------------------------------------------------------------------- relations
@dataclass
class Rel:
    cte: str
    known: set = field(default_factory=set)       # columns definitely present
    open: bool = True                             # more (unlisted) columns may exist
    from_src: bool = False                        # derives from the staged sheet (so unknown cols are inputs)
    has_row: bool = True                          # carries the "__row" provenance column
    raw: bool = False                             # still an un-promoted source (navigation / skip steps)
    nested: dict = field(default_factory=dict)    # NestedJoin columns waiting for ExpandTableColumn
    kinds: dict = field(default_factory=dict)     # column -> num/text/date/bool once a step has typed it


@dataclass
class Pipeline:
    entity: str
    steps: list = field(default_factory=list)     # [{name, func, ok, error}]
    ctes: list = field(default_factory=list)      # [(name, sql)]
    final: str = "src"
    inputs: list = field(default_factory=list)
    references: dict = field(default_factory=dict)
    complete: bool = False
    error: str = ""
    warnings: list = field(default_factory=list)
    passthrough: list = field(default_factory=list)   # attributes no step touches: they flow from the source unchanged
    sources: list = field(default_factory=list)       # external sources the entity query reads and how each was satisfied
    source_kinds: set = field(default_factory=set)      # 'file' (Excel/CSV/SharePoint) and/or 'dataflow' (linked entity)
    dynamic_columns: bool = False        # UnpivotOtherColumns: every other sheet column must be staged, not just the inputs

    def sql(self, final: Optional[str] = None, limit: Optional[int] = None) -> str:
        parts = [f'{q(n)} AS ({s})' for n, s in self.ctes]
        tail = f"\nLIMIT {int(limit)}" if limit else ""
        return "WITH " + ",\n".join(parts) + f"\nSELECT * FROM {q(final or self.final)}{tail}"


SOURCE_FUNCS = ("Excel.", "Csv.", "File.", "Web.", "SharePoint.", "Folder.", "AzureStorage.", "Json.", "OData.",
                "Sql.", "Lakehouse.", "PowerPlatform.", "Table.FromColumns")
INLINE_FUNCS = ("#table", "Table.FromRows", "Table.FromRecords")


EXT_RE = re.compile(r"\.(xlsx|xlsm|xls|csv)\s*$", re.I)


def _walk(node):
    """Every dict node of an AST (including those inside lists and tuples)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _walk(v)


def _ids_in(node) -> set:
    return {n["name"] for n in _walk(node) if n.get("t") == "id"}


def _basename(s: str) -> str:
    from urllib.parse import unquote
    return unquote(re.split(r"[\\/]", s.split("?")[0].rstrip("/\\"))[-1])


def _source_hints(exprs) -> dict:
    """What an external source's M mentions: file names, sheet/Item, linked entity, dataflow id, plain strings."""
    h = {"files": [], "entity": [], "dataflow_id": [], "sheet": [], "strings": []}
    for expr in exprs:
        for n in _walk(expr):
            if n.get("t") == "lit" and n.get("kind") == "str":
                h["strings"].append(n["v"])
                if EXT_RE.search(n["v"]):
                    h["files"].append(_basename(n["v"]))
            elif n.get("t") == "record":
                for key, val in n["fields"]:
                    if val.get("t") == "lit" and val.get("kind") == "str":
                        k = key.lower()
                        if k == "entity":
                            h["entity"].append(val["v"])
                        elif k == "dataflowid":
                            h["dataflow_id"].append(val["v"])
                        elif k == "item":
                            h["sheet"].append(val["v"])
                        elif k == "name" and EXT_RE.search(val["v"]):
                            h["files"].append(val["v"])
    return {k: list(dict.fromkeys(v)) for k, v in h.items()}


class Translator:
    """Builds the CTE chain for one entity query, resolving lookups against other queries."""

    def __init__(self, queries: dict[str, str], bindings: dict[str, str] | None = None,
                 entity_tables: dict[str, str] | None = None, date_order: str = "DMY",
                 reference_index: dict | None = None, source_resolver=None):
        self.order = date_order
        # lookup files imported as tables: file name (lower) -> [(table, sheet)]; matched against names found in the M code
        self.reference_index = reference_index or {}
        # callback(source dict) -> table for a linked source that has no explicit binding (the pipeline's merge stage)
        self.source_resolver = source_resolver
        self._analysis: dict[str, dict] = {}
        self._source_cache: dict[tuple, Rel] = {}
        self._lookup_used: dict[str, tuple] = {}
        self._failed: dict[str, set] = {}
        self.query_src = queries
        self.queries: dict[str, dict] = {}
        self.parse_errors: dict[str, str] = {}
        for name, text in queries.items():
            try:
                self.queries[name] = parse_expr(text)
            except MParseError as e:
                self.parse_errors[name] = str(e)
        self.bindings = bindings or {}
        self.entity_tables = entity_tables or {}
        self.p: Pipeline
        self._n = 0
        self._resolving: list[str] = []
        self._query_rels: dict[str, Rel] = {}

    def constant(self, name: str):
        """A parameter query (a literal, e.g. ``shared Year = 2024 meta [IsParameterQuery = true]``) as an AST literal."""
        ast = self.queries.get(name)
        if ast is not None and ast["t"] == "lit":
            return ast
        if ast is not None and ast["t"] == "un" and ast["op"] == "-" and ast["e"]["t"] == "lit":
            return ast
        return None

    # ---- public ----------------------------------------------------------------
    def translate(self, entity: str, attributes: Optional[list[str]] = None) -> Pipeline:
        """Translate ``entity``. ``attributes`` (its dataflow attributes) lets columns that no step mentions, but that
        flow through to the output as in Power Query, be carried as inputs instead of silently loading as NULL."""
        self.p = Pipeline(entity)
        self._n = 0
        self._query_rels = {}
        self._source_cache = {}
        self._lookup_used = {}
        self.p.ctes.append(("src", 'SELECT * FROM "_stg"'))
        if entity in self.parse_errors:
            self.p.error = f"Could not parse M: {self.parse_errors[entity]}"
            return self.p
        if entity not in self.queries:
            self.p.error = "No Power Query code stored for this entity"
            return self.p
        try:
            rel = self._run_query(entity, main=True)
            self.p.final = rel.cte if rel else self.p.final
            if rel is not None and rel.open and attributes:        # an open schema keeps every source column
                have = {k.lower() for k in rel.known} | {i.lower() for i in self.p.inputs}
                self.p.passthrough = [a for a in attributes if a.lower() not in have and not a.startswith("_")]
                self.p.inputs += self.p.passthrough
            self.p.complete = rel is not None and not self.p.error and all(s["ok"] for s in self.p.steps)
        except Unsupported as e:
            self.p.error = str(e)
        return self.p

    # ---- helpers -----------------------------------------------------------------
    def _new(self, hint: str) -> str:
        self._n += 1
        return f"{slug(hint)}_{self._n}"

    def _emit(self, hint: str, sql: str, **kw) -> Rel:
        name = self._new(hint)
        self.p.ctes.append((name, sql))
        return Rel(name, **kw)

    def _use(self, rel: Rel, cols, ctx: str, main: bool) -> None:
        for c in cols:
            if c in rel.known:
                continue
            if rel.open:
                if rel.from_src and main and c not in self.p.inputs:
                    self.p.inputs.append(c)
                rel.known.add(c)
            else:
                raise Unsupported(f"{ctx}: column {c!r} does not exist at this point")

    def _is_source(self, e, scope) -> bool:
        t = e["t"]
        if t == "id":
            r = scope.get(e["name"])
            return bool(r and r.raw)
        if t in ("item", "field"):
            base = e["of"]
            return base is not None and self._is_source(base, scope)
        if t == "call" and e["fn"]["t"] == "id":
            fn = e["fn"]["name"]
            if fn.startswith(SOURCE_FUNCS):
                if getattr(self, "_main_ctx", False):
                    self.p.source_kinds.add("dataflow" if fn.startswith("PowerPlatform.") else "file")
                return True
        return False

    # ---- query / step evaluation ------------------------------------------------------
    def _run_query(self, qname: str, main: bool) -> Optional[Rel]:
        if qname in self._resolving:
            raise Unsupported(f"circular reference through {qname!r}")
        self._resolving.append(qname)
        outer_ctx, self._main_ctx = getattr(self, "_main_ctx", False), main
        try:
            ast = self.queries[qname]
            if ast["t"] != "let":
                ast = {"t": "let", "steps": [("Result", ast)], "in": {"t": "id", "name": "Result"}}
            scope: dict[str, Rel] = {}
            last: Optional[Rel] = None
            self._failed[qname] = set()
            for name, expr in ast["steps"]:
                func = self._head(expr)
                rec = {"query": qname, "name": name, "func": func, "ok": True, "error": ""}
                try:
                    rel = self._step(expr, scope, main, qname)
                    scope[name] = rel
                    last = rel
                    rec["cte"] = rel.cte
                except (Unsupported, MParseError) as e:
                    rec.update(ok=False, error=str(e))
                    self._failed[qname].add(name)
                    if not main:
                        raise Unsupported(f"lookup query {qname!r}, step {name!r}: {e}")
                if main:
                    self.p.steps.append(rec)
            res = ast["in"]
            if res["t"] == "id" and res["name"] in scope:
                return self._promote(scope[res["name"]], main, qname, res["name"])
            if main and res["t"] == "id":
                self.p.error = self.p.error or f"Result step {res['name']!r} could not be translated"
            if self._failed[qname]:
                return None                       # something failed: the last good step is not the query's result
            return self._promote(last, main, qname) if last else None
        finally:
            self._resolving.pop()
            self._main_ctx = outer_ctx

    @staticmethod
    def _head(e) -> str:
        if e["t"] == "call" and e["fn"]["t"] == "id":
            return e["fn"]["name"]
        return {"item": "navigation", "field": "navigation", "id": "reference", "list": "list", "record": "record",
                "lit": "literal"}.get(e["t"], e["t"])

    # ---- external sources ---------------------------------------------------------------
    def _raw_expr(self, e, raw: set) -> bool:
        """Is this step expression an external source (or a navigation / row-skip of one)?"""
        t = e["t"]
        if t == "id":
            return e["name"] in raw
        if t in ("item", "field"):
            return e["of"] is not None and self._raw_expr(e["of"], raw)
        if t == "call" and e["fn"]["t"] == "id":
            fn = e["fn"]["name"]
            if fn.startswith(SOURCE_FUNCS):
                return True
            if fn in ("Table.Skip", "Table.RemoveFirstN") and e["args"] and self._raw_expr(e["args"][0], raw):
                return True
        return False

    def _analyse(self, qname: str) -> dict:
        """Which steps of a query are external sources, which of them the rest of the query actually reads, and what each mentions."""
        if qname in self._analysis:
            return self._analysis[qname]
        ast = self.queries[qname]
        if ast["t"] != "let":
            ast = {"t": "let", "steps": [("Result", ast)], "in": {"t": "id", "name": "Result"}}
        steps = dict(ast["steps"])
        raw: list[str] = []
        for name, expr in ast["steps"]:
            if self._raw_expr(expr, set(raw)):
                raw.append(name)
        used: set[str] = set()
        for name, expr in ast["steps"]:
            if name not in raw:
                used |= _ids_in(expr) & set(raw)
        used |= _ids_in(ast["in"]) & set(raw)

        def chain(n, seen=None):
            seen = seen or set()
            if n in seen:
                return []
            seen.add(n)
            out = [steps[n]]
            for ref in _ids_in(steps[n]) & set(raw):
                out += chain(ref, seen)
            return out
        info = {"raw": raw, "consumed": [n for n in raw if n in used],
                "hints": {n: _source_hints(chain(n)) for n in raw}}
        self._analysis[qname] = info
        return info

    def _auto_reference(self, hints: dict):
        """A lookup table imported from the file this source names (and the sheet it picks), if unambiguous."""
        for f in hints["files"]:
            cands = self.reference_index.get(f.lower(), [])
            if hints["sheet"]:
                sheet = [c for c in cands if (c[1] or "").lower() in {s.lower() for s in hints["sheet"]}]
                cands = sheet or cands
            if len({c[0] for c in cands}) == 1:
                return cands[0][0], f
        return None

    def _source_rel(self, qname: str, name: Optional[str], main: bool) -> Rel:
        """The relation for an external source: bound table, auto-matched table, or (single source of a stage) the stage input."""
        key = (qname, name)
        if key in self._source_cache:
            return self._source_cache[key]
        an = self._analyse(qname) if qname in self.queries else {"consumed": [], "hints": {}}
        hints = an["hints"].get(name or "", {"files": [], "entity": [], "dataflow_id": [], "sheet": [], "strings": []})
        table, mode, extra = None, None, {}
        bound = self.bindings.get(f"{qname}::{name}") if name else None
        if bound:
            table, mode = bound, "bound"
        elif not main and self.bindings.get(qname):
            table, mode = self.bindings[qname], "bound"
        elif not main:
            auto = self._auto_reference(hints)
            if auto:
                table, mode, extra = auto[0], "auto", {"file": auto[1]}
        elif len(an["consumed"]) >= 2 and self.source_resolver:
            src = {"query": qname, "step": name, "hints": hints, "candidates": []}
            table = self.source_resolver(src)
            if table:
                mode = "auto"
            else:
                extra = {"candidates": src["candidates"]}
        if table:
            rel = self._emit(f"source_{slug(name or qname)}", f"SELECT * FROM {q(table)}", open=True, has_row=False)
            self._lookup_used[qname] = (table, mode == "auto", extra.get("file"))
        elif main and len(an["consumed"]) < 2:
            rel, mode = Rel("src", set(), True, from_src=True, has_row=True), "stage_input"       # the stage's input table
        else:
            if main:
                self.p.sources.append({"query": qname, "step": name, "hints": hints, "table": None, "mode": "missing", **extra})
            what = hints["files"] or hints["entity"] or ["data"]
            raise Unsupported(f"{'source' if main else 'query'} {name or qname!r} reads {', '.join(what)} and has no table bound to it")
        if main:
            self.p.sources.append({"query": qname, "step": name, "hints": hints, "table": table, "mode": mode, **extra})
            if table:
                self.p.references[f"src:{qname}::{name}"] = {"kind": "binding", "table": table}
        self._source_cache[key] = rel
        return rel

    def _promote(self, rel: Optional[Rel], main: bool, qname: str, name: Optional[str] = None) -> Optional[Rel]:
        if rel is not None and rel.raw:
            return self._source_rel(qname, name, main)
        return rel

    def _rel(self, e, scope, main, qname) -> Rel:
        """Evaluate an expression that must yield a table, as a Rel."""
        if e["t"] == "id":
            n = e["name"]
            if n in scope:
                return self._promote(scope[n], main, qname, n)
            if n in self._failed.get(qname, ()):
                raise Unsupported(f"depends on step {n!r}, which could not be translated")
            return self._resolve_query(n)
        return self._promote(self._step(e, scope, main, qname), main, qname)

    def _resolve_query(self, name: str) -> Rel:
        if name in self._query_rels:
            return self._query_rels[name]
        external = name in self.queries and bool(self._analyse(name)["consumed"])
        if name in self.bindings and not external:         # a bound table replaces a query that has no external source
            rel = self._emit(f"ref_{name}", f"SELECT * FROM {q(self.bindings[name])}", open=True, has_row=False)
            self.p.references[name] = {"kind": "binding", "table": self.bindings[name]}
        elif name in self.queries:
            try:
                self._lookup_used.pop(name, None)
                rel = self._run_query(name, main=False)
                used = self._lookup_used.get(name)       # the query's own steps run over the bound / matched lookup table
                self.p.references[name] = ({"kind": "binding", "table": used[0], **({"auto": True, "file": used[2]} if used[1] else {})}
                                           if used else {"kind": "query"})
            except Unsupported as e:
                if name in self.entity_tables:
                    rel = self._emit(f"ref_{name}", f"SELECT * FROM {q(self.entity_tables[name])}", open=True, has_row=False)
                    self.p.references[name] = {"kind": "entity_table", "table": self.entity_tables[name]}
                else:
                    files = list(dict.fromkeys(f for h in self._analyse(name)["hints"].values() for f in h["files"]))
                    self.p.references[name] = {"kind": "missing", "reason": str(e), "files": files}
                    raise Unsupported(f"lookup {name!r} needs data: {e}. Bind it to a table")
        elif name in self.entity_tables:
            rel = self._emit(f"ref_{name}", f"SELECT * FROM {q(self.entity_tables[name])}", open=True, has_row=False)
            self.p.references[name] = {"kind": "entity_table", "table": self.entity_tables[name]}
        else:
            self.p.references[name] = {"kind": "missing", "reason": "unknown query"}
            raise Unsupported(f"unknown query or identifier {name!r}")
        self._query_rels[name] = rel
        return rel

    def _step(self, e, scope, main, qname) -> Rel:
        if self._is_source(e, scope):
            return Rel("src", raw=True)
        t = e["t"]
        if t == "id":
            return self._rel(e, scope, main, qname)
        if t != "call" or e["fn"]["t"] != "id":
            raise Unsupported(f"unsupported expression ({t})")
        fn, args = e["fn"]["name"], e["args"]
        if fn in INLINE_FUNCS or fn == "#table":
            return self._inline_table(fn, args)
        handler = getattr(self, "f_" + fn.replace(".", "_").replace("#", ""), None)
        if handler is None:
            raise Unsupported(f"{fn} is not supported")
        return handler(args, scope, main, qname)

    # ---- inline tables ------------------------------------------------------------------
    def _inline_table(self, fn, args) -> Rel:
        if fn == "Table.FromRecords":
            raise Unsupported("Table.FromRecords is not supported")
        if len(args) < 2 or args[0]["t"] != "list" or args[1]["t"] != "list":
            raise Unsupported(f"{fn} needs literal rows and column names")
        cols = [self._const_str(c) for c in (args[0]["items"] if fn == "#table" else args[1]["items"])]
        rows_node = args[1] if fn == "#table" else args[0]
        rows = []
        for r in rows_node["items"]:
            if r["t"] != "list" or len(r["items"]) != len(cols):
                raise Unsupported("inline table row does not match its column count")
            rows.append("(" + ", ".join(self._const_sql(v) for v in r["items"]) + ")")
        sql = f"SELECT * FROM (VALUES {', '.join(rows)}) AS t({', '.join(q(c) for c in cols)})"
        kinds = {}
        for i, c in enumerate(cols):
            ks = {r["items"][i]["kind"] for r in rows_node["items"] if r["items"][i]["t"] == "lit"
                  and r["items"][i]["kind"] != "null"}
            if len(ks) == 1 and ks <= {"str", "num", "bool"}:
                kinds[c] = {"str": "text", "num": "num", "bool": "bool"}[ks.pop()]
        return self._emit("inline", sql, known=set(cols), open=False, has_row=False, kinds=kinds)

    def _const_str(self, n) -> str:
        if n["t"] == "lit" and n["kind"] == "str":
            return n["v"]
        raise Unsupported("expected a text literal")

    def _const_sql(self, n) -> str:
        if n["t"] == "lit":
            k, v = n["kind"], n["v"]
            return "NULL" if k == "null" else ("TRUE" if v else "FALSE") if k == "bool" else lit(v) if k == "str" else repr(v)
        if n["t"] == "un" and n["op"] == "-" and n["e"]["t"] == "lit":
            return "-" + repr(n["e"]["v"])
        raise Unsupported("inline tables may only contain literal values")

    # ---- argument helpers ------------------------------------------------------------------
    def _str_list(self, n) -> list[str]:
        if n["t"] == "lit" and n["kind"] == "str":
            return [n["v"]]
        if n["t"] == "list":
            return [self._const_str(i) for i in n["items"]]
        raise Unsupported("expected a column name or list of column names")

    def _pairs(self, n) -> list[list]:
        if n["t"] != "list":
            raise Unsupported("expected a list of {column, value} pairs")
        out = []
        for it in n["items"]:
            if it["t"] != "list" or len(it["items"]) < 2:
                raise Unsupported("expected {column, value} pairs")
            out.append(it["items"])
        return out

    def _missing_ignore(self, args, idx) -> bool:
        return len(args) > idx and args[idx]["t"] == "id" and args[idx]["name"].endswith("Ignore")

    def _ex(self, rel: Rel, main: bool, row_ctx=None) -> "Expr":
        return Expr(self, rel, main, row_ctx)

    # ---- table functions ------------------------------------------------------------------
    def f_Table_PromoteHeaders(self, args, scope, main, qname):
        rel = self._rel_or_raw(args[0], scope, main, qname)
        return self._promote(rel, main, qname, args[0]["name"] if args[0]["t"] == "id" else None) if rel.raw else rel   # no-op otherwise

    def f_Table_DemoteHeaders(self, args, scope, main, qname):
        raise Unsupported("Table.DemoteHeaders is not supported")

    def _rel_or_raw(self, e, scope, main, qname) -> Rel:
        if e["t"] == "id" and e["name"] in scope:
            return scope[e["name"]]
        return self._step(e, scope, main, qname) if e["t"] != "id" else self._rel(e, scope, main, qname)

    def _src_arg(self, args, scope, main, qname) -> Rel:
        return self._rel(args[0], scope, main, qname)

    def _same(self, rel: Rel, sql_body: str, hint: str, **over) -> Rel:
        new = self._emit(hint, sql_body, known=set(rel.known), open=rel.open, from_src=rel.from_src,
                         has_row=rel.has_row, kinds=dict(rel.kinds))
        for k, v in over.items():
            setattr(new, k, v)
        return new

    def f_Table_Skip(self, args, scope, main, qname):
        rel = self._rel_or_raw(args[0], scope, main, qname)
        if rel.raw:
            return rel                                  # title rows above the header: handled by header detection
        n = int(self._num(args[1]))
        return self._same(rel, f"SELECT * FROM {q(rel.cte)} ORDER BY {q('__row')} OFFSET {n}" if rel.has_row
                          else f"SELECT * FROM {q(rel.cte)} OFFSET {n}", "skip")

    f_Table_RemoveFirstN = f_Table_Skip

    def f_Table_FirstN(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        n = int(self._num(args[1]))
        order = f" ORDER BY {q('__row')}" if rel.has_row else ""
        return self._same(rel, f"SELECT * FROM {q(rel.cte)}{order} LIMIT {n}", "firstn")

    def _num(self, n) -> float:
        if n["t"] == "lit" and n["kind"] == "num":
            return n["v"]
        raise Unsupported("expected a number literal")

    def f_Table_Buffer(self, args, scope, main, qname):
        return self._src_arg(args, scope, main, qname)

    def f_Table_RemoveRowsWithErrors(self, args, scope, main, qname):
        return self._src_arg(args, scope, main, qname)       # no error values exist in staged text

    def f_Table_ReorderColumns(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        self._use(rel, self._str_list(args[1]), "ReorderColumns", main)
        return rel

    def f_Table_RenameColumns(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        ignore = self._missing_ignore(args, 2)
        pairs = []
        for it in self._pairs(args[1]):
            old, new = self._const_str(it[0]), self._const_str(it[1])
            if ignore and old not in rel.known and not rel.open:
                continue
            pairs.append((old, new))
        self._use(rel, [o for o, _ in pairs], "RenameColumns", main)
        if not pairs:
            return rel
        sql = f"SELECT * RENAME ({', '.join(f'{q(o)} AS {q(n)}' for o, n in pairs)}) FROM {q(rel.cte)}"
        new = self._same(rel, sql, "rename")
        olds = {o for o, _ in pairs}
        new.known = (rel.known - olds) | {n for _, n in pairs}
        ren = dict(pairs)
        new.kinds = {ren.get(c, c): k for c, k in rel.kinds.items()}
        return new

    def f_Table_RemoveColumns(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        cols = self._str_list(args[1])
        if self._missing_ignore(args, 2):
            cols = [c for c in cols if c in rel.known]
        else:
            self._use(rel, cols, "RemoveColumns", main)
        if not cols:
            return rel
        new = self._same(rel, f"SELECT * EXCLUDE ({', '.join(q(c) for c in cols)}) FROM {q(rel.cte)}", "remove")
        new.known = rel.known - set(cols)
        new.kinds = {c: k for c, k in rel.kinds.items() if c not in cols}
        return new

    def f_Table_SelectColumns(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        cols = self._str_list(args[1])
        if self._missing_ignore(args, 2):
            cols = [c for c in cols if c in rel.known or rel.open]
        self._use(rel, cols, "SelectColumns", main)
        sel = [q(c) for c in cols] + ([q("__row")] if rel.has_row else [])
        return self._emit("select", f"SELECT {', '.join(sel)} FROM {q(rel.cte)}", known=set(cols), open=False,
                          from_src=rel.from_src, has_row=rel.has_row,
                          kinds={c: k for c, k in rel.kinds.items() if c in cols})

    def f_Table_TransformColumnTypes(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        reps, kinds = [], dict(rel.kinds)
        order = date_order_for_culture(args[2]["v"]) if len(args) > 2 and args[2]["t"] == "lit" \
            and args[2]["kind"] == "str" else self.order
        for it in self._pairs(args[1]):
            col = self._const_str(it[0])
            self._use(rel, [col], "TransformColumnTypes", main)
            tname = type_name(it[1])
            reps.append(f"{cast_type(q(col), tname, order)} AS {q(col)}")
            if TYPE_KIND.get(tname):
                kinds[col] = TYPE_KIND[tname]
            else:
                kinds.pop(col, None)
        if not reps:
            return rel
        new = self._same(rel, f"SELECT * REPLACE ({', '.join(reps)}) FROM {q(rel.cte)}", "types")
        new.kinds = kinds
        return new

    def f_Table_TransformColumns(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        reps, kinds = [], dict(rel.kinds)
        for it in self._pairs(args[1]):
            col = self._const_str(it[0])
            self._use(rel, [col], "TransformColumns", main)
            fn = it[1]
            ex = self._ex(rel, main, row_ctx=q(col))
            if fn["t"] == "each":
                val, kind = ex.sql(fn["body"])
            elif fn["t"] == "id":                                    # e.g. Text.Upper
                val, kind = ex.sql({"t": "call", "fn": fn, "args": [{"t": "id", "name": "_"}]})
            elif fn["t"] == "fn" and len(fn["params"]) == 1:
                val, kind = ex.sql(fn["body"], bind={fn["params"][0]: q(col)})
            else:
                raise Unsupported("unsupported transformation function")
            if len(it) > 2:
                tname = type_name(it[2])
                val, kind = cast_type(val, tname, self.order), TYPE_KIND.get(tname, "unknown")
            if kind in ("num", "text", "date", "bool"):
                kinds[col] = kind
            else:
                kinds.pop(col, None)
            reps.append(f"{val} AS {q(col)}")
        new = self._same(rel, f"SELECT * REPLACE ({', '.join(reps)}) FROM {q(rel.cte)}", "transform")
        new.kinds = kinds
        return new

    def f_Table_AddColumn(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        name = self._const_str(args[1])
        fn = args[2]
        if fn["t"] == "fn" and len(fn["params"]) == 1:
            body, bind = fn["body"], {fn["params"][0]: "*row*"}
        elif fn["t"] == "each":
            body, bind = fn["body"], None
        else:
            raise Unsupported("AddColumn needs an `each` expression")
        val, kind = self._ex(rel, main).sql(body, bind=bind)
        if len(args) > 3 and args[3]["t"] in ("type", "id"):
            tname = type_name(args[3])
            val, kind = cast_type(val, tname, self.order), TYPE_KIND.get(tname, "unknown")
        new = self._same(rel, f"SELECT *, {val} AS {q(name)} FROM {q(rel.cte)}", "add")
        new.known = rel.known | {name}
        if kind in ("num", "text", "date", "bool"):
            new.kinds[name] = kind
        else:
            new.kinds.pop(name, None)
        return new

    def f_Table_ReplaceValue(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        old, new = args[1], args[2]
        replacer = args[3]["name"] if args[3]["t"] == "id" else ""
        cols = self._str_list(args[4])
        self._use(rel, cols, "ReplaceValue", main)
        reps = []
        for c in cols:
            ex = self._ex(rel, main)
            ov, _ = ex.sql(old)
            nv, _ = ex.sql(new)
            col = q(c)
            if replacer == "Replacer.ReplaceText":
                reps.append(f"replace(CAST({col} AS VARCHAR), {ov}, {nv}) AS {col}")
            elif replacer == "Replacer.ReplaceValue":
                cond = f"{col} IS NULL" if old["t"] == "lit" and old["kind"] == "null" else \
                    f"CAST({col} AS VARCHAR) = CAST({ov} AS VARCHAR)"
                reps.append(f"CASE WHEN {cond} THEN {nv} ELSE {col} END AS {col}")
            else:
                raise Unsupported(f"replacer {replacer or '?'} is not supported")
        new = self._same(rel, f"SELECT * REPLACE ({', '.join(reps)}) FROM {q(rel.cte)}", "replace")
        if replacer == "Replacer.ReplaceText":
            new.kinds.update({c: "text" for c in cols})
        return new

    def f_Table_SelectRows(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        fn = args[1]
        if fn["t"] == "each":
            cond, bind = fn["body"], None
        elif fn["t"] == "fn" and len(fn["params"]) == 1:
            cond, bind = fn["body"], {fn["params"][0]: "*row*"}
        else:
            raise Unsupported("SelectRows needs an `each` condition")
        sql, kind = self._ex(rel, main).sql(cond, bind=bind)
        if kind != "bool":
            sql = f"sp_bool(CAST({sql} AS VARCHAR))"
        return self._same(rel, f"SELECT * FROM {q(rel.cte)} WHERE COALESCE({sql}, FALSE)", "filter")

    def f_Table_Distinct(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        if len(args) > 1:
            cols = self._str_list(args[1])
            self._use(rel, cols, "Distinct", main)
            order = f" ORDER BY {q('__row')}" if rel.has_row else ""
            return self._same(rel, f"SELECT * FROM {q(rel.cte)} QUALIFY row_number() OVER "
                                   f"(PARTITION BY {', '.join(q(c) for c in cols)}{order}) = 1", "distinct")
        ex = f" EXCLUDE ({q('__row')})" if rel.has_row else ""
        return self._same(rel, f"SELECT DISTINCT *{ex} FROM {q(rel.cte)}", "distinct", has_row=False)

    def f_Table_Sort(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        specs = []
        items = args[1]["items"] if args[1]["t"] == "list" else []
        if args[1]["t"] == "lit":
            items = [args[1]]
        for it in items:
            if it["t"] == "lit":
                specs.append((it["v"], "ASC"))
            elif it["t"] == "list" and it["items"]:
                d = it["items"][1]["name"] if len(it["items"]) > 1 and it["items"][1]["t"] == "id" else "Order.Ascending"
                specs.append((self._const_str(it["items"][0]), "DESC" if d == "Order.Descending" else "ASC"))
            else:
                raise Unsupported("unsupported sort specification")
        self._use(rel, [c for c, _ in specs], "Sort", main)
        return self._same(rel, f"SELECT * FROM {q(rel.cte)} ORDER BY {', '.join(f'{q(c)} {d}' for c, d in specs)}", "sort")

    def f_Table_FillDown(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        if not rel.has_row:
            raise Unsupported("FillDown needs original row order, which was lost by an earlier step")
        cols = self._str_list(args[1])
        self._use(rel, cols, "FillDown", main)
        reps = [f"last_value({q(c)} IGNORE NULLS) OVER (ORDER BY {q('__row')} ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS {q(c)}"
                for c in cols]
        return self._same(rel, f"SELECT * REPLACE ({', '.join(reps)}) FROM {q(rel.cte)}", "filldown")

    def f_Table_Combine(self, args, scope, main, qname):
        if args[0]["t"] != "list" or not args[0]["items"]:
            raise Unsupported("Table.Combine needs a literal list of tables")
        rels = [self._rel(i, scope, main, qname) for i in args[0]["items"]]
        parts = [f"SELECT * {('EXCLUDE (' + q('__row') + ') ') if r.has_row else ''}FROM {q(r.cte)}" for r in rels]
        known = set().union(*[r.known for r in rels])
        kinds = {c: k for c, k in rels[0].kinds.items() if all(r.kinds.get(c) == k for r in rels)}
        return self._emit("combine", " UNION ALL BY NAME ".join(parts), known=known, open=any(r.open for r in rels),
                          from_src=any(r.from_src for r in rels), has_row=False, kinds=kinds)

    # -- aggregation / reshaping ---------------------------------------------------------
    _AGGS = {"List.Sum": "sum", "List.Max": "max", "List.Min": "min", "List.Average": "avg", "List.Count": "count"}

    def _agg_sql(self, body, rel, main) -> tuple[str, str]:
        """SQL for one Table.Group aggregation body such as ``List.Sum([amt])`` or ``Table.RowCount(_)``."""
        if body["t"] == "call" and body["fn"]["t"] == "id":
            fn, args = body["fn"]["name"], body["args"]
            if fn == "Table.RowCount":
                return "count(*)", "num"
            if fn in self._AGGS and len(args) == 1:
                arg = args[0]
                distinct = False
                if arg["t"] == "call" and arg["fn"]["t"] == "id" and arg["fn"]["name"] == "List.Distinct" and fn == "List.Count":
                    arg, distinct = arg["args"][0], True
                if arg["t"] == "field" and (arg["of"] is None or arg["of"]["t"] == "id" and arg["of"]["name"] == "_"):
                    sql, kind = self._ex(rel, main).sql({"t": "field", "of": None, "name": arg["name"]})
                    if fn == "List.Count":
                        return (f"count(DISTINCT {sql})" if distinct else f"count({sql})"), "num"
                    if fn in ("List.Max", "List.Min") and kind == "text":
                        return f"{self._AGGS[fn]}({sql})", "text"
                    return f"{self._AGGS[fn]}({Expr.num(sql, kind)})", "num"
        raise Unsupported("unsupported aggregation (supported: List.Sum/Max/Min/Average/Count of a column, "
                          "List.Count(List.Distinct(column)), Table.RowCount(_))")

    def f_Table_Group(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        if len(args) > 3 and not (args[3]["t"] == "id" and args[3]["name"] == "GroupKind.Global"):
            raise Unsupported("only global grouping is supported (GroupKind.Local is not)")
        keys = self._str_list(args[1])
        self._use(rel, keys, "Group", main)
        sel, kinds = [q(k) for k in keys], {k: v for k, v in rel.kinds.items() if k in keys}
        for it in self._pairs(args[2]):
            name, fn = self._const_str(it[0]), it[1]
            if fn["t"] != "each":
                raise Unsupported("aggregations must be `each` expressions")
            sql, kind = self._agg_sql(fn["body"], rel, main)
            if len(it) > 2:
                tname = type_name(it[2])
                sql, kind = cast_type(sql, tname, self.order), TYPE_KIND.get(tname, kind)
            sel.append(f"{sql} AS {q(name)}")
            if kind in ("num", "text", "date", "bool"):
                kinds[name] = kind
        group = f" GROUP BY {', '.join(q(k) for k in keys)}" if keys else ""
        return self._emit("group", f"SELECT {', '.join(sel)} FROM {q(rel.cte)}{group}",
                          known=set(keys) | {self._const_str(i[0]) for i in self._pairs(args[2])},
                          open=False, from_src=rel.from_src, has_row=False, kinds=kinds)

    def _unpivot(self, rel, on_sql: str, attr: str, val: str, known, open_, main):
        return self._emit("unpivot", f"SELECT * FROM (UNPIVOT {q(rel.cte)} ON {on_sql} INTO NAME {q(attr)} VALUE {q(val)})",
                          known=known | {attr, val}, open=open_, from_src=rel.from_src, has_row=rel.has_row,
                          kinds={k: v for k, v in rel.kinds.items() if k in known})

    def f_Table_UnpivotOtherColumns(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        keep = self._str_list(args[1])
        self._use(rel, keep, "UnpivotOtherColumns", main)
        attr, val = self._const_str(args[2]), self._const_str(args[3])
        if rel.from_src and main:
            self.p.dynamic_columns = True
        excl = ", ".join(q(c) for c in keep + (["__row"] if rel.has_row else []))
        return self._unpivot(rel, f"COLUMNS(* EXCLUDE ({excl}))", attr, val, set(keep), False, main)

    def f_Table_Unpivot(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        cols = self._str_list(args[1])
        self._use(rel, cols, "Unpivot", main)
        attr, val = self._const_str(args[2]), self._const_str(args[3])
        return self._unpivot(rel, ", ".join(q(c) for c in cols), attr, val, rel.known - set(cols), rel.open, main)

    @staticmethod
    def _delimiter(node, what: str) -> str:
        if node["t"] == "call" and node["fn"]["t"] == "id" and node["fn"]["name"] == what and node["args"] \
                and node["args"][0]["t"] == "lit" and node["args"][0]["kind"] == "str":
            return node["args"][0]["v"]
        raise Unsupported(f"only {what}(\"<delimiter>\") is supported")

    def f_Table_SplitColumn(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        col = self._const_str(args[1])
        self._use(rel, [col], "SplitColumn", main)
        delim = self._delimiter(args[2], "Splitter.SplitTextByDelimiter")
        if len(args) < 4 or args[3]["t"] != "list":
            raise Unsupported("SplitColumn needs a literal list of new column names")
        names = self._str_list(args[3])
        parts = ", ".join(f"list_extract(string_split(CAST({q(col)} AS VARCHAR), {lit(delim)}), {i}) AS {q(n)}"
                          for i, n in enumerate(names, 1))
        new = self._same(rel, f"SELECT * EXCLUDE ({q(col)}), {parts} FROM {q(rel.cte)}", "split")
        new.known = (rel.known - {col}) | set(names)
        new.kinds = {**{k: v for k, v in rel.kinds.items() if k != col}, **{n: "text" for n in names}}
        return new

    def f_Table_CombineColumns(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        cols = self._str_list(args[1])
        self._use(rel, cols, "CombineColumns", main)
        delim = self._delimiter(args[2], "Combiner.CombineTextByDelimiter")
        name = self._const_str(args[3])
        joined = f"concat_ws({lit(delim)}, {', '.join(f'CAST({q(c)} AS VARCHAR)' for c in cols)})"
        new = self._same(rel, f"SELECT * EXCLUDE ({', '.join(q(c) for c in cols)}), {joined} AS {q(name)} FROM {q(rel.cte)}", "combine_cols")
        new.known = (rel.known - set(cols)) | {name}
        new.kinds = {**{k: v for k, v in rel.kinds.items() if k not in cols}, name: "text"}
        return new

    _KINDS = {"JoinKind.LeftOuter": "LEFT", "JoinKind.Inner": "INNER", "JoinKind.RightOuter": "RIGHT",
              "JoinKind.FullOuter": "FULL"}

    def _join_kind(self, args, idx, default: str) -> str:
        if len(args) <= idx:
            return default
        n = args[idx]
        if n["t"] == "id" and n["name"] in self._KINDS:
            return self._KINDS[n["name"]]
        raise Unsupported(f"join kind {n.get('name', '?')} is not supported")

    def f_Table_NestedJoin(self, args, scope, main, qname):
        left = self._src_arg(args, scope, main, qname)
        right = self._rel(args[2], scope, main, qname)
        k1, k2 = self._str_list(args[1]), self._str_list(args[3])
        if len(k1) != len(k2):
            raise Unsupported("join key lists differ in length")
        self._use(left, k1, "NestedJoin", main)
        self._use(right, k2, "NestedJoin", main)
        newcol = self._const_str(args[4])
        kind = self._join_kind(args, 5, "LEFT")   # NestedJoin defaults to LeftOuter
        out = Rel(left.cte, set(left.known), left.open, left.from_src, left.has_row, kinds=dict(left.kinds))
        out.nested = {**left.nested, newcol: (right, k1, k2, kind)}
        return out

    def f_Table_ExpandTableColumn(self, args, scope, main, qname):
        rel = self._src_arg(args, scope, main, qname)
        col = self._const_str(args[1])
        if col not in rel.nested:
            raise Unsupported(f"{col!r} is not a nested-join column built earlier in this query")
        right, k1, k2, kind = rel.nested[col]
        cols = self._str_list(args[2])
        names = self._str_list(args[3]) if len(args) > 3 else cols
        self._use(right, cols, "ExpandTableColumn", main)
        on = " AND ".join(f"CAST(l.{q(a)} AS VARCHAR) = CAST(r.{q(b)} AS VARCHAR)" for a, b in zip(k1, k2))
        sel = ", ".join(f"r.{q(c)} AS {q(n)}" for c, n in zip(cols, names))
        sql = f"SELECT l.*, {sel} FROM {q(rel.cte)} l {kind} JOIN {q(right.cte)} r ON {on}"
        kinds = {**rel.kinds, **{n: right.kinds[c] for c, n in zip(cols, names) if c in right.kinds}}
        new = self._emit("lookup", sql, known=rel.known | set(names), open=rel.open, from_src=rel.from_src,
                         has_row=rel.has_row, kinds=kinds)
        new.nested = {k: v for k, v in rel.nested.items() if k != col}
        return new

    def f_Table_Join(self, args, scope, main, qname):
        left = self._src_arg(args, scope, main, qname)
        right = self._rel(args[2], scope, main, qname)
        k1, k2 = self._str_list(args[1]), self._str_list(args[3])
        self._use(left, k1, "Join", main)
        self._use(right, k2, "Join", main)
        kind = self._join_kind(args, 4, "INNER")  # Table.Join defaults to Inner
        on = " AND ".join(f"CAST(l.{q(a)} AS VARCHAR) = CAST(r.{q(b)} AS VARCHAR)" for a, b in zip(k1, k2))
        excl = [b for a, b in zip(k1, k2) if a == b]
        rrow = f"{q('__row')}" if right.has_row else None
        exr = f" EXCLUDE ({', '.join(q(c) for c in excl + ([ '__row'] if rrow else []))})" if (excl or rrow) else ""
        return self._emit("join", f"SELECT l.*, r.*{exr} FROM {q(left.cte)} l {kind} JOIN {q(right.cte)} r ON {on}",
                          known=left.known | right.known, open=left.open or right.open, from_src=left.from_src,
                          has_row=left.has_row, kinds={**right.kinds, **left.kinds})


# ----------------------------------------------------------------------------- expressions
class Expr:
    """Translate an M expression to DuckDB SQL; returns ``(sql, kind)`` with kind in num/text/date/bool/unknown."""

    def __init__(self, tr: Translator, rel: Rel, main: bool, row_ctx: Optional[str] = None):
        self.tr, self.rel, self.main, self.under = tr, rel, main, row_ctx

    # coercion helpers
    @staticmethod
    def num(s, k):
        return s if k == "num" else f"TRY_CAST({s} AS DOUBLE)"

    @staticmethod
    def text(s, k):
        return s if k == "text" else f"CAST({s} AS VARCHAR)"

    def date(self, s, k):
        return s if k == "date" else f"sp_ts_{self.tr.order.lower()}(CAST({s} AS VARCHAR))"

    @staticmethod
    def boolean(s, k):
        return s if k == "bool" else f"sp_bool(CAST({s} AS VARCHAR))"

    def sql(self, n, bind=None):
        bind = bind or {}
        t = n["t"]
        if t == "lit":
            k, v = n["kind"], n["v"]
            if k == "null":
                return "NULL", "unknown"
            if k == "bool":
                return ("TRUE" if v else "FALSE"), "bool"
            if k == "str":
                return lit(v), "text"
            return repr(v), "num"
        if t == "id":
            nm = n["name"]
            if nm not in bind and nm != "_":
                const = self.tr.constant(nm)
                if const is not None:
                    return self.sql(const, bind)
            if nm == "_" or nm in bind:
                if nm in bind and bind[nm] == "*row*":
                    raise Unsupported("a whole-row reference (_) cannot be translated")
                if nm == "_" and self.under is None:
                    raise Unsupported("`_` used outside a column transformation")
                return (bind.get(nm) or self.under), "unknown"
            raise Unsupported(f"identifier {nm!r} is not a column or supported value")
        if t == "field":
            base = n["of"]
            if base is not None and not (base["t"] == "id" and (base["name"] == "_" or bind.get(base["name"]) == "*row*")):
                raise Unsupported("nested field access is not supported")
            self.tr._use(self.rel, [n["name"]], "expression", self.main)
            return q(n["name"]), self.rel.kinds.get(n["name"], "unknown")
        if t == "un":
            s, k = self.sql(n["e"], bind)
            if n["op"] == "not":
                return f"(NOT {self.boolean(s, k)})", "bool"
            return f"({n['op']}{self.num(s, k)})", "num"
        if t == "bin":
            return self.binary(n, bind)
        if t == "if":
            c, ck = self.sql(n["c"], bind)
            a, ak = self.sql(n["a"], bind)
            b, bk = self.sql(n["b"], bind)
            kinds = {ak, bk} - {"unknown"}
            kind = kinds.pop() if len(kinds) == 1 else "unknown"
            return f"(CASE WHEN COALESCE({self.boolean(c, ck)}, FALSE) THEN {a} ELSE {b} END)", kind
        if t == "is":
            s, _ = self.sql(n["e"], bind)
            if n["type"] != "null":
                raise Unsupported(f"`is {n['type']}` type tests are not supported")
            return f"({s} IS NULL)", "bool"
        if t == "try":
            raise Unsupported("try/otherwise is not supported")
        if t == "call":
            return self.call(n, bind)
        if t == "list":
            raise Unsupported("list values are only supported as arguments (e.g. List.Contains)")
        raise Unsupported(f"unsupported expression ({t})")

    def binary(self, n, bind):
        op = n["op"]
        left, lk = self.sql(n["l"], bind)
        right, rk = self.sql(n["r"], bind)
        if op in ("and", "or"):
            return f"({self.boolean(left, lk)} {op.upper()} {self.boolean(right, rk)})", "bool"
        if op == "&":
            return f"({self.text(left, lk)} || {self.text(right, rk)})", "text"
        if op in ("+", "-", "*", "/"):
            if op == "+" and "date" in (lk, rk):
                raise Unsupported("date arithmetic: use Date.AddDays / Date.AddMonths")
            return f"({self.num(left, lk)} {op} {self.num(right, rk)})", "num"
        # comparisons
        if op in ("=", "<>"):
            for side, other, k in ((n["l"], right, rk), (n["r"], left, lk)):
                if side["t"] == "lit" and side["kind"] == "null":
                    return f"({other} IS {'NOT ' if op == '<>' else ''}NULL)", "bool"
        known = {lk, rk} - {"unknown"}
        kind = known.pop() if len(known) == 1 else ("mixed" if known else "text")
        if kind == "mixed":
            raise Unsupported("comparison between different value types")
        conv = {"num": self.num, "date": self.date, "bool": self.boolean, "text": self.text}[kind]
        sqlop = {"=": "=", "<>": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">="}[op]
        return f"({conv(left, lk)} {sqlop} {conv(right, rk)})", "bool"

    def call(self, n, bind):
        if n["fn"]["t"] != "id":
            raise Unsupported("unsupported function call")
        fn, a = n["fn"]["name"], n["args"]
        S = lambda i: self.sql(a[i], bind)
        T = lambda i: self.text(*S(i))
        N = lambda i: self.num(*S(i))
        D = lambda i: self.date(*S(i))
        def argc(*ok):
            if len(a) not in ok:
                raise Unsupported(f"{fn} called with {len(a)} arguments")
        if fn == "Text.Upper": argc(1); return f"upper({T(0)})", "text"
        if fn == "Text.Lower": argc(1); return f"lower({T(0)})", "text"
        if fn == "Text.Proper": argc(1); return f"sp_proper(lower({T(0)}))", "text"
        if fn == "Text.Trim": argc(1, 2); return f"trim({T(0)})", "text"
        if fn == "Text.TrimStart": argc(1, 2); return f"ltrim({T(0)})", "text"
        if fn == "Text.TrimEnd": argc(1, 2); return f"rtrim({T(0)})", "text"
        if fn == "Text.Clean": argc(1); return f"regexp_replace({T(0)}, '[\\x00-\\x1f]', '', 'g')", "text"
        if fn == "Text.Length": argc(1); return f"length({T(0)})", "num"
        if fn == "Text.Start": argc(2); return f"left({T(0)}, {N(1)}::INTEGER)", "text"
        if fn == "Text.End": argc(2); return f"right({T(0)}, {N(1)}::INTEGER)", "text"
        if fn == "Text.Middle": argc(3); return f"substr({T(0)}, {N(1)}::INTEGER + 1, {N(2)}::INTEGER)", "text"
        if fn == "Text.Replace": argc(3); return f"replace({T(0)}, {T(1)}, {T(2)})", "text"
        if fn == "Text.Contains": argc(2); return f"contains({T(0)}, {T(1)})", "bool"
        if fn == "Text.StartsWith": argc(2); return f"starts_with({T(0)}, {T(1)})", "bool"
        if fn == "Text.EndsWith": argc(2); return f"ends_with({T(0)}, {T(1)})", "bool"
        if fn == "Text.PadStart": argc(3); return f"lpad({T(0)}, {N(1)}::INTEGER, {T(2)})", "text"
        if fn == "Text.PadEnd": argc(3); return f"rpad({T(0)}, {N(1)}::INTEGER, {T(2)})", "text"
        if fn == "Text.From": argc(1, 2); return f"CAST({S(0)[0]} AS VARCHAR)", "text"
        if fn == "Text.BeforeDelimiter": argc(2); return f"split_part({T(0)}, {T(1)}, 1)", "text"
        if fn == "Text.AfterDelimiter":
            argc(2)
            return f"substr({T(0)}, strpos({T(0)}, {T(1)}) + length({T(1)}))", "text"
        if fn == "Text.Combine":
            if a[0]["t"] != "list":
                raise Unsupported("Text.Combine needs a literal list")
            sep = T(1) if len(a) > 1 else "''"
            return f"concat_ws({sep}, {', '.join(self.text(*self.sql(i, bind)) for i in a[0]['items'])})", "text"
        if fn == "Number.From": argc(1, 2); return f"sp_dbl(CAST({S(0)[0]} AS VARCHAR))", "num"
        if fn in ("Number.Round", "Number.RoundUp", "Number.RoundDown"):
            argc(1, 2, 3)
            digits = f"{N(1)}::INTEGER" if len(a) > 1 else "0"
            mode = {"Number.RoundUp": "RoundingMode.Up", "Number.RoundDown": "RoundingMode.Down"}.get(fn, "RoundingMode.ToEven")
            if len(a) > 2 and a[2]["t"] == "id":
                mode = a[2]["name"]
            x, p = N(0), f"power(10, {digits})"
            # Power Query's default is round-half-to-even; DuckDB's round() rounds half away from zero.
            exprs = {"RoundingMode.ToEven": f"round_even({x}, {digits})", "RoundingMode.AwayFromZero": f"round({x}, {digits})",
                     "RoundingMode.Up": f"(ceil({x} * {p}) / {p})", "RoundingMode.Down": f"(floor({x} * {p}) / {p})",
                     "RoundingMode.TowardZero": f"(trunc({x} * {p}) / {p})"}
            if mode not in exprs:
                raise Unsupported(f"{mode} is not supported")
            return exprs[mode], "num"
        if fn == "Number.Abs": argc(1); return f"abs({N(0)})", "num"
        if fn == "Number.Mod": argc(2); return f"({N(0)} % {N(1)})", "num"
        if fn == "Number.IntegerDivide": argc(2); return f"({N(0)} // {N(1)})", "num"
        if fn == "Number.ToText": argc(1, 3); return f"CAST({S(0)[0]} AS VARCHAR)", "text"
        if fn in ("Date.Year", "Date.Month", "Date.Day", "Date.Quarter"):
            argc(1)
            return f"{fn.split('.')[1].lower()}({D(0)})", "num"
        if fn in ("Date.From", "DateTime.Date"): argc(1, 2); return f"CAST({D(0)} AS DATE)", "date"
        if fn == "DateTime.From": argc(1, 2); return D(0), "date"
        if fn == "Date.AddDays": argc(2); return f"({D(0)} + to_days({N(1)}::INTEGER))", "date"
        if fn == "Date.AddMonths": argc(2); return f"({D(0)} + to_months({N(1)}::INTEGER))", "date"
        if fn == "Date.AddYears": argc(2); return f"({D(0)} + to_years({N(1)}::INTEGER))", "date"
        if fn == "Date.StartOfMonth": argc(1); return f"CAST(date_trunc('month', {D(0)}) AS DATE)", "date"
        if fn == "Date.EndOfMonth": argc(1); return f"CAST(last_day({D(0)}) AS DATE)", "date"
        if fn == "Date.StartOfYear": argc(1); return f"CAST(date_trunc('year', {D(0)}) AS DATE)", "date"
        if fn == "Date.ToText":
            argc(1, 2, 3)
            fmt = a[1]["v"] if len(a) > 1 and a[1]["t"] == "lit" else "yyyy-MM-dd"
            f2 = fmt
            for k, v in (("yyyy", "%Y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"), ("dd", "%d"), ("yy", "%y")):
                f2 = f2.replace(k, v)
            return f"strftime({D(0)}, {lit(f2)})", "text"
        if fn in ("DateTime.LocalNow", "DateTime.FixedLocalNow"): return "now()::TIMESTAMP", "date"
        if fn == "#date":
            argc(3)
            return f"make_date({N(0)}::INTEGER, {N(1)}::INTEGER, {N(2)}::INTEGER)", "date"
        if fn == "#datetime":
            argc(6)
            return f"make_timestamp({N(0)}::BIGINT, {N(1)}::BIGINT, {N(2)}::BIGINT, {N(3)}::BIGINT, {N(4)}::BIGINT, {N(5)}::DOUBLE)", "date"
        if fn == "List.Contains":
            argc(2, 3)
            if a[0]["t"] != "list":
                raise Unsupported("List.Contains needs a literal list")
            x, xk = S(1)
            items = [self.sql(i, bind) for i in a[0]["items"]]
            conv = {"num": self.num, "date": self.date}.get(xk) or self.text
            return f"({conv(x, xk)} IN ({', '.join(conv(s, k) for s, k in items)}))", "bool"
        if fn == "Value.Equals": argc(2); return f"({T(0)} = {T(1)})", "bool"
        if fn == "Record.Field": raise Unsupported("Record.Field is not supported")
        raise Unsupported(f"{fn} is not supported")


# ----------------------------------------------------------------------------- staging / running
def stage_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def sheet_columns(header, inputs: list[str], mapping: dict, norm_cells: list[str], dynamic: bool):
    """Which sheet columns to stage: ``[(column name, sheet column index)]``.

    The mapped input columns, plus (when the pipeline unpivots "other columns") every remaining named column of the
    sheet under its own header text, so the steps see the same columns Power Query would.
    """
    from .inspect_excel import cell_text
    used, cols = set(), []
    for name in inputs:
        for i, h in enumerate(norm_cells):
            if mapping.get(h) == name and i not in used:
                used.add(i)
                cols.append((name, i))
                break
        else:
            cols.append((name, None))
    if dynamic:
        taken = {n.lower() for n in inputs}
        for i, cell in enumerate(header):
            text = cell_text(cell)
            if i in used or not text:
                continue
            name, n = text, 2
            while name.lower() in taken:
                name, n = f"{text}_{n}", n + 1
            taken.add(name.lower())
            cols.append((name, i))
    return cols


def stage_table(cur, columns: list[str], rows: list[tuple]) -> None:
    """(Re)create TEMP table ``_stg`` with a ``__row`` column plus VARCHAR columns."""
    from .bulk import bulk_insert
    ddl = ", ".join(f"{q(c)} VARCHAR" for c in columns)
    cur.execute(f'CREATE OR REPLACE TEMP TABLE "_stg" ("__row" INTEGER{", " + ddl if ddl else ""})')
    if rows:
        bulk_insert(cur, "_stg", ["__row", *columns], rows)


def describe_final(cur, p: Pipeline) -> list[tuple[str, str]]:
    """Output (column, type) of the pipeline, computed against an empty staging table."""
    stage_table(cur, p.inputs, [])
    return [(r[0], r[1]) for r in cur.execute(f"SELECT column_name, column_type FROM (DESCRIBE {p.sql()})").fetchall()]
