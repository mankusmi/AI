"""Fast bulk inserts into DuckDB.

``executemany`` managed only ~700 rows/s; staging rows in a temporary CSV and running
``INSERT ... SELECT FROM read_csv`` is ~250x faster. Values are written as text and cast to the
target column types, ``None`` is distinguished from the empty string via a sentinel.
"""
from __future__ import annotations

import csv
import os
import tempfile
from datetime import date, datetime, time
from decimal import Decimal
from typing import Iterable, Sequence

NULL = "\x1fNULL\x1f"


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fmt(v) -> str:
    if v is None:
        return NULL
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, (int, Decimal)):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return v.replace("\x00", "")
    raise TypeError(f"bulk_insert cannot write {type(v).__name__} values")


def bulk_insert(con, table: str, columns: Sequence[str], rows: Iterable[Sequence], chunk: int = 100_000) -> int:
    """Insert ``rows`` (tuples/lists aligned with ``columns``) into ``table``. Returns the row count."""
    types = dict(con.execute(f"SELECT column_name, column_type FROM (DESCRIBE {qi(table)})").fetchall())
    missing = [c for c in columns if c not in types]
    if missing:
        raise KeyError(f"Unknown column(s) in {table}: {missing}")
    spec = ", ".join(f"'c{i}': 'VARCHAR'" for i in range(len(columns)))
    sel = ", ".join(f"c{i}" if types[c] == "VARCHAR" else f"CAST(c{i} AS {types[c]})" for i, c in enumerate(columns))
    sql = (f"INSERT INTO {qi(table)} ({', '.join(qi(c) for c in columns)}) SELECT {sel} FROM "
           f"read_csv(?, header = false, all_varchar = true, nullstr = ?, quote = '\"', escape = '\"', "
           f"columns = {{{spec}}}, auto_detect = false)")
    total, buf = 0, []

    def flush():
        nonlocal total, buf
        if not buf:
            return
        fd, path = tempfile.mkstemp(suffix=".csv", prefix="sp_bulk_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh, lineterminator="\n")
                for r in buf:
                    w.writerow([_fmt(v) for v in r])
            con.execute(sql, [path, NULL])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        total += len(buf)
        buf = []

    for r in rows:
        buf.append(r)
        if len(buf) >= chunk:
            flush()
    flush()
    return total
