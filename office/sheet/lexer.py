"""Tokenizer for spreadsheet formula text.

PURE — see ``office/sheet/__init__.py``.

``lex(text)`` turns a formula string into a flat list of :class:`Token`. It
never inspects the value lattice, never resolves a reference and never
executes anything — that discipline is what makes ``parser.py`` a plain,
easily-tested transformation on a token stream, with no ``eval`` anywhere in
the chain.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

NUMBER = "NUMBER"
STRING = "STRING"
BOOLEAN = "BOOLEAN"
ERROR = "ERROR"
CELL_REF = "CELL_REF"
IDENTIFIER = "IDENTIFIER"
OPERATOR = "OPERATOR"
LPAREN = "LPAREN"
RPAREN = "RPAREN"
COMMA = "COMMA"
COLON = "COLON"
EOF = "EOF"


@dataclass(frozen=True)
class Token:
    type: str
    text: str
    position: int


class LexError(ValueError):
    """A character sequence that cannot be tokenized at all — reserved for
    genuinely malformed input (e.g. a stray ``@``); this is a controlled,
    documented exception, never a bare ``eval`` failure."""


#: Matched in this order at each position; the first regex that matches wins.
#: ``CELL_REF`` must be tried before ``IDENTIFIER`` since ``A1`` would
#: otherwise lex as a bare name.
_TOKEN_PATTERNS: List[tuple] = [
    (
        NUMBER,
        re.compile(
            r"\d+\.\d+(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?"
        ),
    ),
    (STRING, re.compile(r'"(?:[^"]|"")*"')),
    (ERROR, re.compile(r"#(?:DIV/0!|REF!|VALUE!|NAME\?|N/A|NUM!|CYCLE!)")),
    (
        CELL_REF,
        re.compile(
            r"(?:(?:'(?:[^']|'')+')|[A-Za-z_][A-Za-z0-9_.]*)!\$?[A-Za-z]{1,3}\$?[1-9][0-9]*"
            r"|\$?[A-Za-z]{1,3}\$?[1-9][0-9]*"
        ),
    ),
    (IDENTIFIER, re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")),
    (OPERATOR, re.compile(r"<=|>=|<>|[=<>+\-*/^&]")),
    (LPAREN, re.compile(r"\(")),
    (RPAREN, re.compile(r"\)")),
    (COMMA, re.compile(r",")),
    (COLON, re.compile(r":")),
]

_WHITESPACE = re.compile(r"[ \t\r\n]+")
_BOOLEAN_TEXTS = {"TRUE", "FALSE"}


def _next_non_whitespace_character(text: str, position: int) -> Optional[str]:
    match = _WHITESPACE.match(text, position)
    index = match.end() if match else position
    return text[index] if index < len(text) else None


def lex(text: str) -> List[Token]:
    """Tokenize a formula. A leading ``=`` (as typed by a spreadsheet user)
    is stripped by the caller — see ``parser.parse_formula`` — this function
    tokenizes exactly the expression text it is given."""
    tokens: List[Token] = []
    position = 0
    length = len(text)
    while position < length:
        whitespace_match = _WHITESPACE.match(text, position)
        if whitespace_match:
            position = whitespace_match.end()
            continue

        matched_type = None
        matched_text = ""
        for token_type, pattern in _TOKEN_PATTERNS:
            match = pattern.match(text, position)
            if match:
                matched_type = token_type
                matched_text = match.group(0)
                break

        if matched_type is None:
            raise LexError(
                f"unrecognised character {text[position]!r} at position {position}"
            )

        if matched_type == CELL_REF:
            # A token that lexically matches a cell reference but is
            # immediately followed by "(" is a function call whose name
            # happens to look like a reference (e.g. a hypothetical
            # ``LOG10(``) — Excel resolves this ambiguity the same way.
            following_character = _next_non_whitespace_character(
                text, position + len(matched_text)
            )
            if following_character == "(":
                matched_type = IDENTIFIER

        if matched_type == IDENTIFIER and matched_text.upper() in _BOOLEAN_TEXTS:
            matched_type = BOOLEAN

        tokens.append(Token(type=matched_type, text=matched_text, position=position))
        position += len(matched_text)

    tokens.append(Token(type=EOF, text="", position=length))
    return tokens
