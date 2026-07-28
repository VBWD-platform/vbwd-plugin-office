"""Formula parser: token stream -> AST. No ``eval`` anywhere in this chain —
a formula is data (an AST) that ``engine.py`` walks to compute a value.

PURE — see ``office/sheet/__init__.py``.

Precedence, low to high (mirrors how a spreadsheet reads ``=A1+B1*2^3``):
comparison (``= <> < > <= >=``) < concatenation (``&``) < additive
(``+ -``) < multiplicative (``* /``) < unary (``+x -x``) < power (``^``,
right-associative).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Union

from .cell import (
    CellAddress,
    CellRange,
    format_cell_reference,
    format_range_reference,
    parse_cell_reference,
)
from .lexer import (
    BOOLEAN,
    CELL_REF,
    COLON,
    COMMA,
    EOF,
    ERROR,
    IDENTIFIER,
    LPAREN,
    NUMBER,
    OPERATOR,
    RPAREN,
    STRING,
    LexError,
    Token,
    lex,
)
from .values import ErrorCode


class FormulaSyntaxError(ValueError):
    """A formula that cannot be parsed at all. Distinct from an evaluation
    error: the caller (``engine.py``) turns this into an ``ErrorValue`` at
    the cell boundary — it never propagates as a raw exception past that
    boundary."""


@dataclass(frozen=True)
class NumberLiteral:
    value: float


@dataclass(frozen=True)
class TextLiteral:
    value: str


@dataclass(frozen=True)
class BooleanLiteral:
    value: bool


@dataclass(frozen=True)
class ErrorLiteral:
    code: ErrorCode


@dataclass(frozen=True)
class CellReferenceNode:
    address: CellAddress


@dataclass(frozen=True)
class RangeReferenceNode:
    cell_range: CellRange


@dataclass(frozen=True)
class UnaryOpNode:
    operator: str
    operand: "Node"


@dataclass(frozen=True)
class BinaryOpNode:
    operator: str
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class FunctionCallNode:
    name: str
    arguments: List["Node"]


Node = Union[
    NumberLiteral,
    TextLiteral,
    BooleanLiteral,
    ErrorLiteral,
    CellReferenceNode,
    RangeReferenceNode,
    UnaryOpNode,
    BinaryOpNode,
    FunctionCallNode,
]

_COMPARISON_OPERATORS = {"=", "<>", "<", ">", "<=", ">="}
_ADDITIVE_OPERATORS = {"+", "-"}
_MULTIPLICATIVE_OPERATORS = {"*", "/"}


class _Parser:
    """Precedence-climbing recursive-descent parser over a token list."""

    def __init__(self, tokens: List[Token], default_sheet: Optional[str]):
        self._tokens = tokens
        self._position = 0
        self._default_sheet = default_sheet

    def _current(self) -> Token:
        return self._tokens[self._position]

    def _advance(self) -> Token:
        token = self._tokens[self._position]
        self._position += 1
        return token

    def _expect(self, token_type: str) -> Token:
        token = self._current()
        if token.type != token_type:
            raise FormulaSyntaxError(
                f"expected {token_type}, found {token.type} {token.text!r} "
                f"at position {token.position}"
            )
        return self._advance()

    def _current_is_operator(self, *operator_texts: str) -> bool:
        token = self._current()
        return token.type == OPERATOR and token.text in operator_texts

    def parse(self) -> Node:
        node = self._parse_comparison()
        self._expect(EOF)
        return node

    def _parse_comparison(self) -> Node:
        left = self._parse_concat()
        while self._current_is_operator(*_COMPARISON_OPERATORS):
            operator = self._advance().text
            right = self._parse_concat()
            left = BinaryOpNode(operator, left, right)
        return left

    def _parse_concat(self) -> Node:
        left = self._parse_additive()
        while self._current_is_operator("&"):
            self._advance()
            right = self._parse_additive()
            left = BinaryOpNode("&", left, right)
        return left

    def _parse_additive(self) -> Node:
        left = self._parse_multiplicative()
        while self._current_is_operator(*_ADDITIVE_OPERATORS):
            operator = self._advance().text
            right = self._parse_multiplicative()
            left = BinaryOpNode(operator, left, right)
        return left

    def _parse_multiplicative(self) -> Node:
        left = self._parse_unary()
        while self._current_is_operator(*_MULTIPLICATIVE_OPERATORS):
            operator = self._advance().text
            right = self._parse_unary()
            left = BinaryOpNode(operator, left, right)
        return left

    def _parse_unary(self) -> Node:
        if self._current_is_operator("+", "-"):
            operator = self._advance().text
            operand = self._parse_unary()
            return UnaryOpNode(operator, operand)
        return self._parse_power()

    def _parse_power(self) -> Node:
        base = self._parse_primary()
        if self._current_is_operator("^"):
            self._advance()
            exponent = self._parse_unary()  # right-associative
            return BinaryOpNode("^", base, exponent)
        return base

    def _parse_function_arguments(self) -> List[Node]:
        arguments: List[Node] = []
        if self._current().type != RPAREN:
            arguments.append(self._parse_comparison())
            while self._current().type == COMMA:
                self._advance()
                arguments.append(self._parse_comparison())
        self._expect(RPAREN)
        return arguments

    def _parse_primary(self) -> Node:
        token = self._current()

        if token.type == NUMBER:
            self._advance()
            return NumberLiteral(float(token.text))

        if token.type == STRING:
            self._advance()
            return TextLiteral(token.text[1:-1].replace('""', '"'))

        if token.type == BOOLEAN:
            self._advance()
            return BooleanLiteral(token.text.upper() == "TRUE")

        if token.type == ERROR:
            self._advance()
            return ErrorLiteral(ErrorCode(token.text))

        if token.type == CELL_REF:
            self._advance()
            start_address = parse_cell_reference(token.text, self._default_sheet)
            if self._current().type == COLON:
                self._advance()
                end_token = self._expect(CELL_REF)
                end_address = parse_cell_reference(end_token.text, self._default_sheet)
                return RangeReferenceNode(CellRange(start_address, end_address))
            return CellReferenceNode(start_address)

        if token.type == IDENTIFIER:
            self._advance()
            self._expect(LPAREN)
            arguments = self._parse_function_arguments()
            return FunctionCallNode(token.text.upper(), arguments)

        if token.type == LPAREN:
            self._advance()
            inner = self._parse_comparison()
            self._expect(RPAREN)
            return inner

        raise FormulaSyntaxError(
            f"unexpected token {token.type} {token.text!r} at position {token.position}"
        )


def parse_formula(text: str, default_sheet: Optional[str] = None) -> Node:
    """Parse formula source into an AST. A leading ``=`` (as a user types
    it) is stripped if present; ``default_sheet`` resolves unqualified cell
    references for graph/lookup purposes."""
    source = text[1:] if text.startswith("=") else text
    try:
        tokens = lex(source)
    except LexError as error:
        raise FormulaSyntaxError(str(error)) from error
    return _Parser(tokens, default_sheet).parse()


def _format_number_literal(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(value)


def serialize(node: Node) -> str:
    """Render an AST back to formula source. Not guaranteed to reproduce the
    original text byte-for-byte, only to be semantically equivalent — the
    only property reference translation (copy/paste, insert/delete) needs."""
    if isinstance(node, NumberLiteral):
        return _format_number_literal(node.value)
    if isinstance(node, TextLiteral):
        return '"' + node.value.replace('"', '""') + '"'
    if isinstance(node, BooleanLiteral):
        return "TRUE" if node.value else "FALSE"
    if isinstance(node, ErrorLiteral):
        return node.code.value
    if isinstance(node, CellReferenceNode):
        return format_cell_reference(node.address)
    if isinstance(node, RangeReferenceNode):
        return format_range_reference(node.cell_range)
    if isinstance(node, UnaryOpNode):
        return f"{node.operator}{serialize(node.operand)}"
    if isinstance(node, BinaryOpNode):
        return f"({serialize(node.left)}{node.operator}{serialize(node.right)})"
    if isinstance(node, FunctionCallNode):
        arguments = ",".join(serialize(argument) for argument in node.arguments)
        return f"{node.name}({arguments})"
    raise AssertionError(f"unknown AST node type: {type(node)!r}")  # engine bug


def transform_references(node: Node, transform) -> Node:
    """Rebuild ``node`` with every cell/range reference passed through
    ``transform(address) -> Optional[CellAddress]``. A ``None`` result marks
    a reference whose target cell no longer exists; it becomes an
    ``ErrorLiteral(#REF!)`` node in place — used by ``engine.py`` to
    implement copy/paste and row/column insert/delete translation."""
    if isinstance(node, CellReferenceNode):
        new_address = transform(node.address)
        if new_address is None:
            return ErrorLiteral(ErrorCode.REF)
        return CellReferenceNode(new_address)
    if isinstance(node, RangeReferenceNode):
        new_start = transform(node.cell_range.start)
        new_end = transform(node.cell_range.end)
        if new_start is None or new_end is None:
            return ErrorLiteral(ErrorCode.REF)
        return RangeReferenceNode(CellRange(new_start, new_end))
    if isinstance(node, UnaryOpNode):
        return UnaryOpNode(node.operator, transform_references(node.operand, transform))
    if isinstance(node, BinaryOpNode):
        return BinaryOpNode(
            node.operator,
            transform_references(node.left, transform),
            transform_references(node.right, transform),
        )
    if isinstance(node, FunctionCallNode):
        return FunctionCallNode(
            node.name,
            [transform_references(argument, transform) for argument in node.arguments],
        )
    return node  # literals carry no reference to transform
