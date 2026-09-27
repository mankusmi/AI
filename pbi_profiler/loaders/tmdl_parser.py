"""A small, generic parser for TMDL (Tabular Model Definition Language).

TMDL is the text format Power BI Desktop writes into a `.SemanticModel/definition`
folder (PBIP projects) and that Tabular Editor 3 can also read/write. It is an
indentation-sensitive, line-oriented format. This module does not attempt to
implement the full TMDL grammar; it implements the subset needed to recover
tables, columns, measures, hierarchies, partitions, relationships and roles,
which is what model profiling needs.

Indentation rule used here (matches how Power BI Desktop / Tabular Editor emit
TMDL): a header line that ends with a bare ``=`` opens a multi-line expression
body indented *two* levels deeper than the header; everything indented exactly
one level deeper than the header is a child node (nested object or a simple
``key: value`` / ``key = value`` property).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TmdlNode:
    keyword: str
    args: str = ""
    value: Optional[str] = None
    is_expression: bool = False
    children: list["TmdlNode"] = field(default_factory=list)
    line_no: int = 0

    def prop(self, key: str, default=None):
        """Return the (single-line) value of the first child with this keyword."""
        for child in self.children:
            if child.keyword == key:
                return child.value
        return default

    def flag(self, key: str, default: bool = False) -> bool:
        """Return a boolean property: bare keyword or `key: true` both mean True."""
        for child in self.children:
            if child.keyword == key:
                if child.value is None:
                    return True
                return child.value.strip().lower() != "false"
        return default

    def find_all(self, keyword: str) -> list["TmdlNode"]:
        return [c for c in self.children if c.keyword == keyword]

    def find(self, keyword: str) -> Optional["TmdlNode"]:
        return next((c for c in self.children if c.keyword == keyword), None)


def _indent_of(line: str) -> int:
    stripped_tabs = line.lstrip("\t")
    n_tabs = len(line) - len(stripped_tabs)
    if n_tabs > 0 or line == stripped_tabs:
        if n_tabs == 0:
            # no tabs at all -- fall back to counting leading spaces in groups of 4
            stripped_spaces = line.lstrip(" ")
            n_spaces = len(line) - len(stripped_spaces)
            return n_spaces // 4
        return n_tabs
    return n_tabs


def _split_header(text: str) -> tuple[str, str, Optional[str], Optional[str]]:
    """Split a header line into (keyword, args, delimiter, value).

    `delimiter` is ``':'``, ``'='`` or ``None`` (bare line, no value).
    `value` is the trimmed text after the delimiter (``None`` if delimiter is
    None, or if it's a bare trailing ``=``/``:``  meaning "value follows below").
    """
    in_quote: Optional[str] = None
    delim_idx = None
    delim_char = None
    for i, ch in enumerate(text):
        if in_quote:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            continue
        if ch in ("=", ":"):
            delim_idx = i
            delim_char = ch
            break
    if delim_idx is None:
        left = text.strip()
        keyword, _, args = left.partition(" ")
        return keyword, args.strip(), None, None

    left = text[:delim_idx].strip()
    right = text[delim_idx + 1 :].strip()
    keyword, _, args = left.partition(" ")
    value = right if right else None
    return keyword, args.strip(), delim_char, value


def _dequote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1]
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1]
    return token


def parse_tmdl(text: str) -> list[TmdlNode]:
    """Parse TMDL source text into a forest of top-level TmdlNode objects."""
    raw_lines = text.splitlines()
    entries: list[tuple[int, str, int]] = []  # (indent, content, line_no)
    for i, raw in enumerate(raw_lines):
        if not raw.strip():
            continue
        content = raw.strip("\n")
        stripped = content.strip()
        if stripped.startswith("///") or stripped.startswith("//"):
            continue
        indent = _indent_of(content)
        entries.append((indent, content, i + 1))

    pos = 0

    def parse_block(min_indent: int) -> list[TmdlNode]:
        nonlocal pos
        nodes: list[TmdlNode] = []
        while pos < len(entries):
            indent, content, line_no = entries[pos]
            if indent < min_indent:
                break
            if indent > min_indent:
                # Orphaned deeper line with no matching header at this level;
                # skip defensively rather than raising, TMDL details we don't
                # model shouldn't break the whole parse.
                pos += 1
                continue
            pos += 1
            keyword, args, delim, value = _split_header(content.strip())
            node = TmdlNode(
                keyword=keyword,
                args=_dequote(args),
                value=None,
                is_expression=False,
                line_no=line_no,
            )
            if delim == "=" and value is None:
                node.is_expression = True
                # contiguous lines indented two deeper are the expression body
                body_lines = []
                while pos < len(entries) and entries[pos][0] >= min_indent + 2:
                    body_lines.append(entries[pos][1])
                    pos += 1
                if body_lines:
                    base_indent = min(_indent_of(l) for l in body_lines)
                    dedented = []
                    for l in body_lines:
                        n = _indent_of(l)
                        dedented.append(("\t" * (n - base_indent)) + l.strip())
                    node.value = "\n".join(dedented)
            else:
                node.value = value

            # children at exactly min_indent + 1
            if pos < len(entries) and entries[pos][0] == min_indent + 1:
                node.children = parse_block(min_indent + 1)
            nodes.append(node)
        return nodes

    return parse_block(0)
