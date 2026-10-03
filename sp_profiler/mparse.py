"""A small parser for the Power Query M language subset used in dataflow queries.

Produces plain-dict ASTs. It understands ``let ... in``, function calls, lists, records, field access
(``[Col Name]``), ``each``, ``if/then/else``, ``try/otherwise``, operators and type literals.
Anything outside that raises ``MParseError`` so callers can report the query as untranslatable.
"""
from __future__ import annotations

import re

KEYWORDS = {"let", "in", "if", "then", "else", "each", "and", "or", "not", "try", "otherwise", "type",
            "as", "is", "true", "false", "null", "meta", "error"}


class MParseError(ValueError):
    pass


TOKEN_RE = re.compile(r"""
    (?P<ws>\s+|//[^\n]*|/\*.*?\*/)
  | (?P<field>\[\s*(?:\#"(?:[^"]|"")*"|[^\[\]=,"(){}]+?)\s*\])
  | (?P<qid>\#"(?:[^"]|"")*")
  | (?P<str>"(?:[^"]|"")*")
  | (?P<num>0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+)
  | (?P<id>\#?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
  | (?P<op><>|<=|>=|=>|\.\.\.|\.\.|\?\?|[=<>+\-*/&(){}\[\],@?;!])
""", re.X | re.S)


def tokenize(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise MParseError(f"Unexpected character {text[pos]!r} at {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        val = m.group()
        if kind == "id" and val in KEYWORDS:
            kind = "kw"
        out.append((kind, val))
    out.append(("eof", ""))
    return out


def unquote(s: str) -> str:
    return s[1:-1].replace('""', '"')


class Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0

    # -- token helpers -------------------------------------------------------
    @property
    def cur(self):
        return self.toks[self.i]

    def at(self, kind, val=None):
        k, v = self.cur
        return k == kind and (val is None or v == val)

    def eat(self, kind, val=None):
        if self.at(kind, val):
            self.i += 1
            return self.toks[self.i - 1][1]
        raise MParseError(f"Expected {val or kind}, found {self.cur[1] or 'end of input'!r}")

    def accept(self, kind, val=None):
        if self.at(kind, val):
            self.i += 1
            return True
        return False

    # -- grammar ---------------------------------------------------------------
    def parse(self):
        e = self.expr()
        if not self.at("eof"):
            raise MParseError(f"Unexpected {self.cur[1]!r}")
        return e

    def expr(self):
        if self.at("kw", "let"):
            return self.let()
        if self.at("kw", "each"):
            self.i += 1
            return {"t": "each", "body": self.expr()}
        if self.at("kw", "if"):
            self.i += 1
            c = self.expr()
            self.eat("kw", "then")
            a = self.expr()
            self.eat("kw", "else")
            return {"t": "if", "c": c, "a": a, "b": self.expr()}
        if self.at("kw", "try"):
            self.i += 1
            e = self.expr()
            other = None
            if self.accept("kw", "otherwise"):
                other = self.expr()
            return {"t": "try", "e": e, "otherwise": other}
        if self.at("kw", "type"):
            return self.type_expr()
        if self.at("op", "(") and self._is_fn():
            return self.fn()
        return self.or_expr()

    def let(self):
        self.eat("kw", "let")
        steps = []
        while True:
            name = self.ident_name()
            self.eat("op", "=")
            steps.append((name, self.expr()))
            if not self.accept("op", ","):
                break
        self.eat("kw", "in")
        return {"t": "let", "steps": steps, "in": self.expr()}

    def ident_name(self) -> str:
        if self.at("qid"):
            return unquote(self.eat("qid")[1:])
        if self.at("id"):
            return self.eat("id")
        raise MParseError(f"Expected identifier, found {self.cur[1]!r}")

    def type_expr(self):
        self.eat("kw", "type")
        self.accept("id", "nullable")
        name = self.eat("id")
        if name == "table" and self.at("field"):      # type table [..] -> keep as opaque
            self.i += 1
        return {"t": "type", "name": name}

    def _is_fn(self) -> bool:
        depth, j = 0, self.i
        while j < len(self.toks):
            k, v = self.toks[j]
            if k == "op" and v == "(":
                depth += 1
            elif k == "op" and v == ")":
                depth -= 1
                if depth == 0:
                    nk, nv = self.toks[j + 1]
                    return (nk, nv) == ("op", "=>") or (nk, nv) == ("kw", "as")
            elif k == "eof":
                return False
            j += 1
        return False

    def fn(self):
        self.eat("op", "(")
        params = []
        while not self.at("op", ")"):
            params.append(self.ident_name())
            if self.accept("kw", "as"):
                self.accept("id", "nullable")
                self.eat("id")
            if not self.accept("op", ","):
                break
        self.eat("op", ")")
        if self.accept("kw", "as"):
            self.eat("id")
        self.eat("op", "=>")
        return {"t": "fn", "params": params, "body": self.expr()}

    def binary(self, sub, ops):
        left = sub()
        while (self.cur[0] in ("op", "kw")) and self.cur[1] in ops:
            op = self.cur[1]
            self.i += 1
            left = {"t": "bin", "op": op, "l": left, "r": sub()}
        return left

    def or_expr(self):
        return self.binary(self.and_expr, {"or"})

    def and_expr(self):
        return self.binary(self.is_expr, {"and"})

    def is_expr(self):
        left = self.eq_expr()
        while self.at("kw", "is") or self.at("kw", "as"):
            self.i += 1
            self.accept("id", "nullable")
            self.eat("id")           # type name is ignored: `x as text`, `x is null`
        return left

    def eq_expr(self):
        return self.binary(self.rel_expr, {"=", "<>"})

    def rel_expr(self):
        return self.binary(self.add_expr, {"<", ">", "<=", ">="})

    def add_expr(self):
        return self.binary(self.mul_expr, {"+", "-", "&"})

    def mul_expr(self):
        return self.binary(self.unary, {"*", "/"})

    def unary(self):
        if self.at("op", "-") or self.at("op", "+") or self.at("kw", "not"):
            op = self.cur[1]
            self.i += 1
            return {"t": "un", "op": op, "e": self.unary()}
        return self.postfix()

    def postfix(self):
        e = self.primary()
        while True:
            if self.at("op", "("):
                self.i += 1
                args = []
                while not self.at("op", ")"):
                    args.append(self.expr())
                    if not self.accept("op", ","):
                        break
                self.eat("op", ")")
                e = {"t": "call", "fn": e, "args": args}
            elif self.at("op", "{"):
                self.i += 1
                idx = self.expr()
                self.eat("op", "}")
                self.accept("op", "?")
                e = {"t": "item", "of": e, "index": idx}
            elif self.at("field"):
                e = {"t": "field", "of": e, "name": self._field_name(self.eat("field"))}
                self.accept("op", "?")
            else:
                return e

    @staticmethod
    def _field_name(tok: str) -> str:
        inner = tok[1:-1].strip()
        return unquote(inner[1:]) if inner.startswith('#"') else inner

    def primary(self):
        k, v = self.cur
        if k == "str":
            self.i += 1
            return {"t": "lit", "kind": "str", "v": unquote(v)}
        if k == "num":
            self.i += 1
            return {"t": "lit", "kind": "num", "v": int(v, 16) if v.lower().startswith("0x") else
                    (float(v) if re.search(r"[.eE]", v) else int(v))}
        if k == "kw" and v in ("true", "false"):
            self.i += 1
            return {"t": "lit", "kind": "bool", "v": v == "true"}
        if k == "kw" and v == "null":
            self.i += 1
            return {"t": "lit", "kind": "null", "v": None}
        if k == "kw" and v == "type":
            return self.type_expr()
        if k == "qid":
            self.i += 1
            return {"t": "id", "name": unquote(v[1:])}
        if k == "id":
            self.i += 1
            return {"t": "id", "name": v}
        if k == "field":
            self.i += 1
            return {"t": "field", "of": None, "name": self._field_name(v)}
        if k == "op" and v == "(":
            self.i += 1
            e = self.expr()
            self.eat("op", ")")
            return e
        if k == "op" and v == "{":
            self.i += 1
            items = []
            while not self.at("op", "}"):
                items.append(self.expr())
                if self.accept("op", ".."):
                    raise MParseError("List ranges are not supported")
                if not self.accept("op", ","):
                    break
            self.eat("op", "}")
            return {"t": "list", "items": items}
        if k == "op" and v == "[":
            self.i += 1
            fields = []
            while not self.at("op", "]"):
                name = self.ident_name()
                self.eat("op", "=")
                fields.append((name, self.expr()))
                if not self.accept("op", ","):
                    break
            self.eat("op", "]")
            return {"t": "record", "fields": fields}
        raise MParseError(f"Unexpected {v or 'end of input'!r}")


def parse_expr(text: str) -> dict:
    return Parser(text).parse()
