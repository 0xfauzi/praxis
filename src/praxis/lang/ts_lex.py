"""Pass one of the TypeScript and JavaScript reader (x1-ts-v1): source text to tokens.

Comments, strings, template literals with nested ``${...}``, regular-expression literals
told from division by the previous significant token, and JSX told from comparison by
the previous token in files that may hold JSX (never in ``.ts``, where ``<T>x`` is a
type assertion). A run of code holding nothing pass two reads is one bulk token, so the
Python cost per byte stays low. A ``/// <reference path>`` directive before the first
token and a JSDoc comment holding ``import(`` are recorded beside the tokens.

Design: docs/design/04-languages.md section 2.5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast

KW, PUNCT, STR, BULK, OPAQUE = "k", "p", "s", "b", "x"
# (kind, text, start, end); a string's text is its decoded value.
Token = tuple[str, str, int, int]

_WORDS = (
    "import", "export", "require", "function", "typeof", "type", "from", "defer",
    "source", "new", "const", "let", "var", "class", "if", "for", "while", "switch",
    "catch", "with", "return", "in", "of", "delete", "void", "throw", "case", "do",
    "else", "yield", "await", "extends", "instanceof", "default", "try",
)  # fmt: skip
_KW = "(?:" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")(?![\w$])"
# Python's \w is Unicode-aware; a class spelled with a \u0080-\uffff range compiles
# about ten times slower (measured: 9.3 ms against 0.8 ms for the master expression),
# a cost every hook process that reads TypeScript pays.
IDENT = r"(?:[^\W\d]|\$)[\w$]*"
_MASTER = re.compile(
    r"(?P<ws>[\s\ufeff]+)"
    r"|(?P<lc>//[^\n]*)"
    r"|(?P<bc>/\*.*?(?:\*/|\Z))"
    r"|(?P<str>\"(?:\\.|[^\"\\\n])*\"?|'(?:\\.|[^'\\\n])*'?)"
    r"|(?P<kw>(?<![\w$])" + _KW + r")"
    r"|(?P<p>=>|[{}()\[\];,=<])"
    r"|(?P<bulk>(?:(?<=\.)(?<!\.\.)" + IDENT + "|(?!" + _KW + ")" + IDENT + r""
    r"|[0-9][\w.]*|[^'\"`/{}()\[\];,=<\s\w$]|[ \t])+)"
    r"|(?P<other>.)",
    re.S,
)
_TEMPLATE = re.compile(r"(?:[^`\\$]+|\\.|\$(?!\{))*", re.S)
_REGEX = re.compile(r"/(?:[^/\\\n\[]+|\\.|\[(?:[^\]\\\n]|\\.)*\])*/?[\w$]*")
_JSX_TAG = re.compile(r"[^{\"'/>]*")
_JSX_TEXT = re.compile(r"[^{<]*")
_JSX_START = re.compile(r"<(?:>|[A-Za-z_$])")
# In .tsx a type parameter list looks like an element: `<T,>`, `<T = U,>`,
# `<T extends U>`.
_GENERIC = re.compile(r"<\s*(?:const\s+)?" + IDENT + r"\s*(?:,|=|extends\s)")
_REFERENCE = re.compile(r"///\s*<reference\s+path\s*=\s*([\"'])(.*?)\1")
_ESCAPE = re.compile(
    r"\\(u\{[0-9A-Fa-f]+\}|u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|\r\n|.)", re.S
)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_REGEX_AFTER = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
    "case", "do", "else", "yield", "await", "extends",
})  # fmt: skip
_JSX_AFTER_WORD = frozenset({"return", "default", "yield", "await", "case"})
_JSX_AFTER_PUNCT = frozenset({"(", ",", "=", "[", "{", "}", "=>", ";"})
_JSX_AFTER_CHAR = frozenset(":?!&|")
_OPEN: Token = (PUNCT, "{", -1, -1)
DEPTH = {"{": 1, "}": -1}


def _unescape(text: str) -> str:
    def one(m: re.Match[str]) -> str:
        code = m.group(1)
        if code[0] in "ux" and len(code) > 1:
            point = int(code[1:].strip("{}"), 16)
            return chr(point) if point <= 0x10FFFF else m.group(0)
        return "" if code in ("\r", "\n", "\r\n") else _ESCAPES.get(code, code)

    return _ESCAPE.sub(one, text) if "\\" in text else text


@dataclass
class Lexer:
    s: str
    jsx: bool
    toks: list[Token] = field(default_factory=list)
    references: list[tuple[int, int, str]] = field(default_factory=list)
    jsdoc: list[tuple[int, int]] = field(default_factory=list)
    i: int = 0
    floor: int = 0

    def prev(self) -> Token | None:
        if len(self.toks) > self.floor:
            return self.toks[-1]
        return _OPEN if self.floor else None

    def run(self, nested: bool = False) -> None:
        """Tokens until the end, or, when nested, until the `}` that closes the
        `${` or JSX `{` the caller consumed."""
        s, n, toks, match = self.s, len(self.s), self.toks, _MASTER.match
        depth = 0
        while self.i < n:
            if s[self.i] in "`/<" and self._special(s[self.i]):
                continue
            # The last alternative matches any character, so there is always a match.
            m = cast("re.Match[str]", match(s, self.i))
            group, end = m.lastgroup, m.end()
            if group == "p":
                depth += DEPTH.get(m.group(), 0)
                if nested and depth < 0:
                    self.i = end
                    return
                toks.append((PUNCT, m.group(), self.i, end))
            elif group in ("kw", "bulk", "other", "str"):
                toks.append(_token(group, m.group(), self.i, end))
            elif group != "ws":
                self._comment(m.group(), self.i, end)
            self.i = end

    def nested(self) -> None:
        outer, self.floor = self.floor, len(self.toks)
        self.run(nested=True)
        self.floor = outer

    def _special(self, c: str) -> bool:
        if c == "`":
            self._template()
            return True
        if c == "/":
            return self._maybe_regex()
        return self._maybe_jsx()

    def _maybe_regex(self) -> bool:
        if self.s.startswith(("//", "/*"), self.i) or not _regex_ok(self.prev()):
            return False
        end = _REGEX.match(self.s, self.i).end()  # type: ignore[union-attr]
        self.toks.append((OPAQUE, "", self.i, end))
        self.i = end
        return True

    def _maybe_jsx(self) -> bool:
        if not self.jsx or not _jsx_ok(self.prev()):
            return False
        if not _JSX_START.match(self.s, self.i) or _GENERIC.match(self.s, self.i):
            return False
        start = self.i
        self._element()
        self.toks.append((OPAQUE, "", start, self.i))
        return True

    def _template(self) -> None:
        s, n, start = self.s, len(self.s), self.i
        j, parts, plain = self.i + 1, [], True
        while True:
            k = _TEMPLATE.match(s, j).end()  # type: ignore[union-attr]
            parts.append(s[j:k])
            if k >= n or s[k] == "`":
                j = min(k + 1, n)
                break
            plain = False
            self.i = k + 2
            self.nested()
            j = self.i
        text = _unescape("".join(parts)) if plain else ""
        self.toks.append((STR if plain else OPAQUE, text, start, j))
        self.i = j

    def _element(self) -> None:
        """At `<` of an element or a fragment: consumes it, its children and its
        closing tag."""
        self.i += 1
        if self.s.startswith(">", self.i):
            self.i += 1
            self._children()
            return
        s, n = self.s, len(self.s)
        while self.i < n:
            self.i = _JSX_TAG.match(s, self.i).end()  # type: ignore[union-attr]
            if self.i >= n or self._tag_char(s[self.i]):
                return

    def _tag_char(self, c: str) -> bool:
        """One significant character inside an opening tag; True when the tag ended."""
        s = self.s
        if c == "{":
            self.i += 1
            self.nested()
        elif c in "\"'":
            close = s.find(c, self.i + 1)
            self.i = len(s) if close < 0 else close + 1
        elif s.startswith("/>", self.i):
            self.i += 2
            return True
        elif c == ">":
            self.i += 1
            self._children()
            return True
        else:
            self.i += 1
        return False

    def _children(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            self.i = _JSX_TEXT.match(s, self.i).end()  # type: ignore[union-attr]
            if self.i >= n:
                return
            if s[self.i] == "{":
                self.i += 1
                self.nested()
            elif s.startswith("</", self.i):
                close = s.find(">", self.i)
                self.i = n if close < 0 else close + 1
                return
            else:
                self._element()

    def _comment(self, text: str, start: int, end: int) -> None:
        if text.startswith("/**") and "import(" in text:
            self.jsdoc.append((start, end))
        elif text.startswith("///") and not self.toks:
            m = _REFERENCE.match(text)
            if m:
                self.references.append((start, end, m.group(2)))


def _token(group: str, text: str, start: int, end: int) -> Token:
    if group == "str":
        body = text[1:-1] if len(text) > 1 and text[-1] == text[0] else text[1:]
        return (STR, _unescape(body), start, end)
    return (KW if group == "kw" else BULK, text, start, end)


def last_char(text: str) -> str:
    return text.rstrip()[-1:]


def _regex_ok(prev: Token | None) -> bool:
    if prev is None:
        return True
    kind, text = prev[0], prev[1]
    if kind == PUNCT:
        return text not in (")", "]", "}")
    if kind == KW:
        return text in _REGEX_AFTER
    if kind == BULK:
        last = last_char(text)
        return not (last.isalnum() or last in "_$")
    return False


def _jsx_ok(prev: Token | None) -> bool:
    if prev is None:
        return True
    kind, text = prev[0], prev[1]
    if kind == PUNCT:
        return text in _JSX_AFTER_PUNCT
    if kind == KW:
        return text in _JSX_AFTER_WORD
    return kind == BULK and last_char(text) in _JSX_AFTER_CHAR
