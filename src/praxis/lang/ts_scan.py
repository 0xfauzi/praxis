"""TypeScript and JavaScript import sites (x1-ts-v1), pass two of a hand lexer.

Pass one (``ts_lex``) gives tokens. This pass reads them as statements: static imports
and re-exports, ``import x = require()``, ``require()`` and ``import()`` calls,
type-position ``import()``, and ``/// <reference path>`` directives at the top of a
file. A brace stack marks function bodies, which gives ``deferred``; a line break
before a word that cannot continue an expression ends an arrow function's expression
body, as automatic semicolon insertion does.

Every site is a statement or a call with one specifier, so a site is its own statement.
The specifier never leaves the process: it is resolved and dropped (invariant 5).

Design: docs/design/04-languages.md sections 1.2 to 1.4 and 2.5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import accumulate

from praxis.lang import ImportSite, Unparsed
from praxis.lang.ts_lex import BULK, DEPTH, KW, PUNCT, STR, Lexer, Token, last_char

# A committed bundle is not source anyone reads; the cut is a design choice (04 4.7).
MAX_BYTES = 512 * 1024
_CONTROL = frozenset({"if", "for", "while", "switch", "catch", "with"})
# Words that start a statement and never sit in an arrow function's expression body: an
# arrow body that ended without a semicolon has ended before one of them.
_STATEMENT_START = frozenset({
    "const", "let", "var", "class", "function", "export", "if", "for", "while",
    "switch", "try", "do", "return", "throw", "with",
})  # fmt: skip
_PROMISE = frozenset({"then", "catch", "finally"})
_TYPE_BEFORE_BRACE = frozenset(":|&<")
# Words that continue an expression across a line break.
_CONTINUING = frozenset({"in", "of", "instanceof", "extends"})
_WORD_START = re.compile(r"[^\W\d]|\$")
_DECLARATION = (".d.ts", ".d.mts", ".d.cts")
_JSX_SUFFIXES = (".tsx", ".jsx", ".js", ".mjs", ".cjs")
# How far a static import or re-export is read for its `from`, in tokens.
_WINDOW = 4000


@dataclass
class _Frame:
    body: bool
    arrow: bool = False
    # A `{` that opens an object type in a return type annotation, not a body.
    annotation: bool = False


@dataclass
class _Reader:
    """Pass two: statements over the token list."""

    toks: list[Token]
    declaration: bool
    source: str
    sites: list[ImportSite] = field(default_factory=list)
    stack: list[_Frame] = field(default_factory=lambda: [_Frame(False)])
    owners: list[str | None] = field(default_factory=list)
    # 0: nothing; 1: after a `)` that may end a parameter list; 2: in its return type.
    pending: int = 0

    def tok(self, k: int) -> Token | None:
        return self.toks[k] if 0 <= k < len(self.toks) else None

    def text(self, k: int) -> str:
        t = self.tok(k)
        return "" if t is None else t[1]

    def read(self) -> list[ImportSite]:
        k, n = 0, len(self.toks)
        while k < n:
            kind = self.toks[k][0]
            if self.stack[-1].arrow and self._inserted(k):
                self.stack[-1].arrow = False
            if kind == PUNCT:
                self._punct(k)
                k += 1
            elif kind == KW:
                k = self._word(k)
            else:
                self._value(k)
                k += 1
        return self.sites

    def _inserted(self, k: int) -> bool:
        """Whether a semicolon is inserted before token k (ASI): a line break after a
        token that ends an expression, before a word that cannot continue one."""
        prev, tok = self.toks[k - 1], self.toks[k]
        if "\n" not in self.source[prev[3] : tok[2]] or not _ends_expression(prev):
            return False
        if tok[0] == KW:
            return tok[1] not in _CONTINUING
        return tok[0] == BULK and bool(_WORD_START.match(tok[1]))

    def deferred(self) -> bool:
        return any(f.body or f.arrow for f in self.stack)

    def add(self, k: int, end: int, spec: str | None, kind: str, flags: str) -> None:
        """``flags`` holds `d` for deferred, `t` for type-only and `b` for blind."""
        blind = "dynamic" if "b" in flags else None
        site = ImportSite(
            self.toks[k][2], end, spec, kind, "d" in flags, "t" in flags, blind
        )
        self.sites.append(site)

    # --- brace and parenthesis tracking -------------------------------------------

    def _punct(self, k: int) -> None:
        text = self.toks[k][1]
        handler = _PUNCT_HANDLERS.get(text)
        if handler is not None:
            handler(self, k)
        elif text == "=" or (text == "<" and self.pending == 1):
            self.pending = 0

    def _open_paren(self, k: int) -> None:
        prev = self.tok(k - 1)
        owner = prev[1] if prev is not None and prev[0] == KW else None
        if owner == "await" and self.text(k - 2) == "for":
            owner = "for"
        self.owners.append(owner)
        self.stack.append(_Frame(False))

    def _open_bracket(self, _k: int) -> None:
        self.stack.append(_Frame(False))

    def _open_brace(self, k: int) -> None:
        if self.pending == 2 and _opens_type(self.tok(k - 1)):
            self.stack.append(_Frame(False, annotation=True))
            return
        body = self.pending > 0 or self.text(k - 1) == "=>"
        self.stack.append(_Frame(body))
        self.pending = 0

    def _close(self, k: int) -> None:
        frame = self.stack.pop() if len(self.stack) > 1 else None
        self.pending = 2 if frame is not None and frame.annotation else 0
        if self.toks[k][1] == ")":
            owner = self.owners.pop() if self.owners else None
            self.pending = int(owner not in _CONTROL)

    def _arrow(self, k: int) -> None:
        if self.text(k + 1) != "{":
            self.stack[-1].arrow = True
        self.pending = 0

    def _separator(self, k: int) -> None:
        # A return type keeps its state through the commas of its type arguments.
        if self.pending != 2 or self.toks[k][1] == ";":
            self.pending = 0
        self.stack[-1].arrow = False

    def _value(self, k: int) -> None:
        kind, text = self.toks[k][0], self.toks[k][1]
        if self.pending == 1:
            self.pending = 2 if kind == BULK and text.lstrip().startswith(":") else 0

    # --- statements ---------------------------------------------------------------

    def _word(self, k: int) -> int:
        text = self.toks[k][1]
        if self.pending == 1 or text in _CONTROL:
            self.pending = 0
        if text in _STATEMENT_START:
            self.stack[-1].arrow = False
        handler = _WORD_HANDLERS.get(text)
        if handler is None or self._member(k):
            return k + 1
        return handler(self, k)

    def _member(self, k: int) -> bool:
        prev = self.tok(k - 1)
        if prev is None or prev[0] != BULK:
            return False
        text = prev[1].rstrip()
        return text.endswith(".") and not text.endswith("...")

    def _import(self, k: int) -> int:
        nxt = self.tok(k + 1)
        if nxt is None:
            return k + 1
        if nxt[1] == "(":
            self._call(k, "import")
            return k + 1
        if nxt[0] == BULK and nxt[1].startswith("."):
            self._meta(k, nxt[1])
            return k + 1
        self.stack[-1].arrow = False
        return _static(self, k)

    def _meta(self, k: int, text: str) -> None:
        if text.startswith((".meta.resolve", ".meta.glob")):
            kind = "resolve" if "resolve" in text else "context"
            self.add(k, self.toks[k + 1][3], None, kind, "db")

    def _require(self, k: int) -> int:
        nxt = self.tok(k + 1)
        if nxt is None or self.text(k - 1) == "function":
            return k + 1
        if nxt[1] == "(":
            self._call(k, "require")
        elif nxt[0] == BULK and nxt[1].startswith((".resolve", ".context")):
            kind = "resolve" if nxt[1].startswith(".resolve") else "context"
            self.add(k, nxt[3], None, kind, "db" if self.deferred() else "b")
        return k + 1

    def _call(self, k: int, name: str) -> None:
        """`import(` or `require(` at k: a literal argument is a site, any other is a
        blind site, and a method of that name (`require(x) {`) is neither."""
        arg, close = self.tok(k + 2), self.tok(k + 3)
        literal = arg is not None and arg[0] == STR and close is not None
        if name == "require" and literal and close is not None and close[1] == ")":
            flags = "d" if self.deferred() else ""
            self.add(k, close[3], arg[1], "require", flags)  # type: ignore[index]
        elif (
            name == "import"
            and literal
            and close is not None
            and close[1] in (")", ",")
        ):
            self._import_call(k, arg[1], close[3])  # type: ignore[index]
        elif not self._method(k + 1):
            flags = "db" if name == "import" or self.deferred() else "b"
            kind = "dynamic_import" if name == "import" else "require"
            self.add(k, self.toks[k + 1][3], None, kind, flags)

    def _import_call(self, k: int, spec: str, end: int) -> None:
        if self._type_position(k):
            self.add(k, end, spec, "import_type_node", "dt")
        else:
            self.add(k, end, spec, "dynamic_import", "d")

    def _type_position(self, k: int) -> bool:
        """A type-position `import("s")`: in a declaration file, after `typeof`, as the
        whole right side of `type X =`, or followed by a member that is not a promise
        method."""
        if self.declaration or self.text(k - 1) == "typeof":
            return True
        if self.text(k - 1) == "=" and self.text(k - 3) == "type":
            return True
        after = self.tok(_matching(self.toks, k + 1) + 1)
        if after is None or after[0] != BULK or not after[1].startswith("."):
            return False
        name = re.match(r"\.\s*([\w$]*)", after[1])
        return name is not None and name.group(1) not in _PROMISE

    def _method(self, k: int) -> bool:
        return self.text(_matching(self.toks, k) + 1) == "{"

    def _export(self, k: int) -> int:
        return _reexport(self, k)

    def _new(self, k: int) -> int:
        """`new URL("./x", import.meta.url)`: resolved at run time, a blind site."""
        texts = [self.text(k + d) for d in range(1, 7)]
        arg = self.tok(k + 3)
        shape = texts[0].strip() == "URL" and texts[1] == "(" and texts[3] == ","
        literal = arg is not None and arg[0] == STR and texts[4] == "import"
        if shape and literal and texts[5].startswith(".meta.url"):
            flags = "db" if self.deferred() else "b"
            self.add(k, self.toks[k + 6][3], None, "resolve", flags)
        return k + 1


_PUNCT_HANDLERS = {
    "(": _Reader._open_paren,
    "[": _Reader._open_bracket,
    "{": _Reader._open_brace,
    ")": _Reader._close,
    "]": _Reader._close,
    "}": _Reader._close,
    "=>": _Reader._arrow,
    ";": _Reader._separator,
    ",": _Reader._separator,
}
_WORD_HANDLERS = {
    "import": _Reader._import,
    "require": _Reader._require,
    "export": _Reader._export,
    "new": _Reader._new,
}


def _opens_type(prev: Token | None) -> bool:
    """In a return type, a `{` after `:`, `|`, `&`, `<` or `,` opens an object type."""
    if prev is None:
        return False
    if prev[0] == PUNCT:
        return prev[1] in ("<", ",")
    return prev[0] == BULK and last_char(prev[1]) in _TYPE_BEFORE_BRACE


def _ends_expression(tok: Token) -> bool:
    kind, text = tok[0], tok[1]
    if kind == PUNCT:
        return text in (")", "]", "}")
    if kind == BULK:
        last = last_char(text)
        return last.isalnum() or last in "_$"
    return kind != KW


def _matching(toks: list[Token], k: int) -> int:
    """The index of the `)` closing the `(` at k, or the last index read."""
    depth, j, n = 0, k, min(len(toks), k + _WINDOW)
    while j < n:
        text = toks[j][1] if toks[j][0] == PUNCT else ""
        depth += {"(": 1, ")": -1}.get(text, 0)
        if depth == 0:
            return j
        j += 1
    return j - 1


def _static_head(r: _Reader, k: int) -> tuple[int, str] | None:
    """Past `import` and its modifiers: (index, flags), or None when the word starts
    no import declaration (an object key, a label)."""
    j = k + 1
    first = r.toks[j]
    if first[0] == PUNCT and first[1] != "{":
        return None
    if first[0] == BULK and not re.match(r"[\w$*]", first[1]):
        return None
    flags = ""
    if first[1] == "type" and r.text(j + 1) not in ("=", ",") and not _from(r, j + 1):
        flags, j = "dt", j + 1
    if r.text(j) in ("defer", "source") and not _from(r, j + 1):
        flags = flags or ("d" if r.text(j) == "defer" else "")
        j += 1
    return j, flags


def _from(r: _Reader, k: int) -> bool:
    nxt = r.tok(k + 1)
    return r.text(k) == "from" and nxt is not None and nxt[0] == STR


def _static(r: _Reader, k: int) -> int:
    first = r.tok(k + 1)
    if first is not None and first[0] == STR:
        r.add(k, first[3], first[1], "side_effect", "")
        return k + 2
    head = _static_head(r, k)
    if head is None:
        return k + 1
    j, flags = head
    equals = _import_equals(r, k, j, flags)
    if equals is not None:
        return equals
    kind = "import_type" if "t" in flags else "import"
    return _scan_from(r, k, j, kind, flags)


def _import_equals(r: _Reader, k: int, j: int, flags: str) -> int | None:
    if r.text(j + 1) != "=":
        return None
    arg = r.tok(j + 4)
    if r.text(j + 2) == "require" and r.text(j + 3) == "(" and arg and arg[0] == STR:
        r.add(k, arg[3], arg[1], "import_equals", flags)
        return j + 6 if r.text(j + 5) == ")" else j + 5
    return j + 2


def _scan_from(r: _Reader, k: int, j: int, kind: str, flags: str) -> int:
    """Read to `from "s"` at brace depth 0; a `;` or another declaration ends it."""
    depth, n = 0, min(len(r.toks), k + _WINDOW)
    while j < n:
        tok_kind, text = r.toks[j][0], r.toks[j][1]
        if tok_kind == PUNCT:
            depth += DEPTH.get(text, 0)
            if depth == 0 and text == ";":
                return j
        elif depth == 0 and tok_kind == KW and text in ("import", "export"):
            return k + 1
        elif depth == 0 and _from(r, j):
            spec = r.toks[j + 1]
            r.add(k, spec[3], spec[1], kind, flags)
            return j + 2
        j += 1
    return k + 1


def _reexport(r: _Reader, k: int) -> int:
    j, flags = k + 1, ""
    if r.text(j) == "type" and r.text(j + 1)[:1] in ("{", "*"):
        j, flags = j + 1, "dt"
    lead = r.tok(j)
    if lead is None:
        return k + 1
    star = lead[0] == BULK and lead[1].startswith("*")
    if not star and lead[1] != "{":
        return k + 1
    kind = "export_type_from" if flags else "export_from"
    return _scan_export(r, k, j, kind, flags)


def _scan_export(r: _Reader, k: int, j: int, kind: str, flags: str) -> int:
    depth, n = 0, min(len(r.toks), k + _WINDOW)
    while j < n:
        tok_kind, text = r.toks[j][0], r.toks[j][1]
        if tok_kind == PUNCT:
            depth += DEPTH.get(text, 0)
            ends = text == ";" or (text == "}" and r.text(j + 1) != "from")
            if depth == 0 and ends:
                return j + (text == "}")
        elif depth == 0 and _from(r, j):
            spec = r.toks[j + 1]
            r.add(k, spec[3], spec[1], kind, flags)
            return j + 2
        j += 1
    return k + 1


def _byte_offsets(text: str, sites: list[ImportSite]) -> list[ImportSite]:
    """Character offsets to UTF-8 byte offsets, one encode per gap between offsets."""
    if text.isascii() or not sites:
        return sites
    points = sorted({p for s in sites for p in (s.start, s.end)})
    gaps = [
        len(text[a:b].encode("utf-8", "surrogateescape")) for a, b in _pairs(points)
    ]
    where = dict(zip(points, accumulate(gaps), strict=True))
    return [
        ImportSite(
            where[s.start], where[s.end], s.specifier, s.kind, s.deferred, s.type_only,
            s.blind,
        )
        for s in sites
    ]  # fmt: skip


def _pairs(points: list[int]) -> list[tuple[int, int]]:
    return list(zip([0, *points[:-1]], points, strict=True))


def read(path: str, source: bytes) -> list[ImportSite]:
    """The import sites of one file, in source order. Pure: bytes in, sites out."""
    if len(source) > MAX_BYTES:
        raise Unparsed("too_large")
    text = source.decode("utf-8", "surrogateescape")
    lexer = Lexer(text, path.endswith(_JSX_SUFFIXES))
    if text.startswith("#!"):
        newline = text.find("\n")
        lexer.i = len(text) if newline < 0 else newline
    try:
        lexer.run()
        sites = _Reader(lexer.toks, path.endswith(_DECLARATION), text).read()
    except RecursionError:
        raise Unparsed("RecursionError") from None
    for start, end, spec in lexer.references:
        sites.append(ImportSite(start, end, spec, "reference_path", True, True, None))
    for start, end in lexer.jsdoc:
        sites.append(ImportSite(start, end, None, "jsdoc", True, True, "jsdoc"))
    sites.sort(key=lambda s: (s.start, s.end))
    return _byte_offsets(text, sites)
