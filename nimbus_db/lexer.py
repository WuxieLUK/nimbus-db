"""SQL tokenizer.

Produces a flat token stream: identifiers/keywords (one token type,
keywords are recognized by the parser), integer/float literals, single-
quoted strings (``''`` escapes a quote), double-quoted identifiers
(``""`` escapes a quote), ``--`` line comments and the operator set.
Error messages carry 1-based line/column positions.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import List

from .errors import ParseError


class TokenType(Enum):
    IDENT = auto()   # identifier or keyword (parser decides)
    INT = auto()
    FLOAT = auto()
    STRING = auto()
    OP = auto()
    EOF = auto()


@dataclass
class Token:
    type: TokenType
    value: object
    pos: int
    line: int
    col: int

    def is_kw(self, *words: str) -> bool:
        return (self.type is TokenType.IDENT
                and isinstance(self.value, str)
                and self.value.upper() in words)

    @property
    def where(self) -> str:
        return f"line {self.line}, column {self.col}"


_MULTI_OPS = ("<=", ">=", "<>", "!=", "||")
_SINGLE_OPS = set("=<>+-*/%(),.;")


def tokenize(sql: str) -> List[Token]:
    toks: List[Token] = []
    i, n = 0, len(sql)
    line, col = 1, 1

    def advance(k: int = 1) -> None:
        nonlocal i, line, col
        for _ in range(k):
            if i < n and sql[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    def err(msg: str) -> None:
        raise ParseError(f"{msg} (line {line}, column {col})")

    while i < n:
        ch = sql[i]

        if ch.isspace():
            advance()
            continue

        # -- line comment
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                advance()
            continue

        pos, ln, cl = i, line, col

        # BLOB literal x'AB CD'
        if ch in "xX" and i + 1 < n and sql[i + 1] == "'":
            advance(2)
            hexdigits = []
            while True:
                if i >= n:
                    err("unterminated blob literal")
                c = sql[i]
                if c == "'":
                    advance()
                    break
                if c.isspace():
                    advance()
                    continue
                if c in "0123456789abcdefABCDEF":
                    hexdigits.append(c)
                    advance()
                    continue
                err(f"invalid blob literal character {c!r}")
            if len(hexdigits) % 2:
                err("blob literal must contain an even number of hex digits")
            toks.append(Token(TokenType.STRING,
                              bytes.fromhex("".join(hexdigits)), pos, ln, cl))
            continue

        # string literal
        if ch == "'":
            advance()
            buf = []
            while True:
                if i >= n:
                    err("unterminated string literal")
                c = sql[i]
                if c == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append("'")
                        advance(2)
                        continue
                    advance()
                    break
                buf.append(c)
                advance()
            toks.append(Token(TokenType.STRING, "".join(buf), pos, ln, cl))
            continue

        # quoted identifier
        if ch == '"':
            advance()
            buf = []
            while True:
                if i >= n:
                    err("unterminated quoted identifier")
                c = sql[i]
                if c == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        buf.append('"')
                        advance(2)
                        continue
                    advance()
                    break
                buf.append(c)
                advance()
            toks.append(Token(TokenType.IDENT, "".join(buf), pos, ln, cl))
            continue

        # number: digits [. digits] [e [+/-] digits]
        if ch.isdigit():
            start = i
            while i < n and sql[i].isdigit():
                advance()
            is_float = False
            if i < n and sql[i] == "." and i + 1 < n and sql[i + 1].isdigit():
                is_float = True
                advance()
                while i < n and sql[i].isdigit():
                    advance()
            if i < n and sql[i] in "eE":
                j = i + 1
                if j < n and sql[j] in "+-":
                    j += 1
                if j < n and sql[j].isdigit():
                    is_float = True
                    advance(j - i)
                    while i < n and sql[i].isdigit():
                        advance()
                else:
                    err("malformed number exponent")
            text = sql[start:i]
            toks.append(Token(
                TokenType.FLOAT if is_float else TokenType.INT,
                float(text) if is_float else int(text),
                pos, ln, cl))
            continue

        # identifier / keyword
        if ch.isalpha() or ch == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] in "_$"):
                advance()
            toks.append(Token(TokenType.IDENT, sql[start:i], pos, ln, cl))
            continue

        # operators
        two = sql[i:i + 2]
        if two in _MULTI_OPS:
            advance(2)
            toks.append(Token(TokenType.OP, two, pos, ln, cl))
            continue
        if ch in _SINGLE_OPS:
            advance()
            toks.append(Token(TokenType.OP, ch, pos, ln, cl))
            continue

        err(f"unexpected character {ch!r}")

    toks.append(Token(TokenType.EOF, None, n, line, col))
    return toks
