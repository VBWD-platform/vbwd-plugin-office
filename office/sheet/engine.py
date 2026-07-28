"""The interpreter and the recalculation orchestrator.

PURE — see ``office/sheet/__init__.py``.

``Workbook``/``Sheet``/``Cell`` here are a minimal, storage-agnostic
in-memory representation good enough to drive the pure engine end to end
(and to test it with) — they are NOT the persistence model. The
office plugin's ``models``/``storage`` layers (built separately, outside
this package) translate their own JSON/ORM representation into calls
against this same ``recalculate`` entry point.

``recalculate(workbook, changed_cells, now)`` is the one function the rest
of the plugin calls: it walks only the dirty sub-graph (``changed_cells``
plus their transitive dependents), evaluates it in topological order, and
returns just the cells whose value changed — never the whole sheet.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, Iterator, Optional, Tuple

from .cell import (
    CellAddress,
    resolve_address,
    shift_address_for_column_delete,
    shift_address_for_column_insert,
    shift_address_for_row_delete,
    shift_address_for_row_insert,
    translate_address_for_copy,
)
from .functions import EvaluationContext, get_function
from .graph import build_dependency_graph, compute_dirty_closure, topological_order
from .parser import (
    BinaryOpNode,
    BooleanLiteral,
    CellReferenceNode,
    ErrorLiteral,
    FormulaSyntaxError,
    FunctionCallNode,
    Node,
    NumberLiteral,
    RangeReferenceNode,
    TextLiteral,
    UnaryOpNode,
    parse_formula,
    serialize,
    transform_references,
)
from .values import (
    BLANK,
    CellValue,
    ErrorCode,
    ErrorValue,
    coerce_to_number,
    coerce_to_text,
    compare_values,
    is_error,
)

_COMPARISON_OPERATORS = {"=", "<>", "<", ">", "<=", ">="}


@dataclass
class Cell:
    """One spreadsheet cell: either a formula (``formula`` set, ``value``
    caches the last computed result) or a literal (``formula`` is
    ``None``, ``value`` is what the user typed/pasted)."""

    formula: Optional[str] = None
    value: CellValue = BLANK


@dataclass
class Sheet:
    name: str
    cells: Dict[Tuple[int, int], Cell] = field(default_factory=dict)

    def get_cell(self, column: int, row: int) -> Cell:
        return self.cells.get((column, row), Cell())

    def set_cell(self, column: int, row: int, cell: Cell) -> None:
        self.cells[(column, row)] = cell


@dataclass
class Workbook:
    """A minimal multi-sheet workbook: exactly what ``recalculate`` needs
    and nothing the persistence layer would need to translate through."""

    sheets: Dict[str, Sheet] = field(default_factory=dict)

    def add_sheet(self, name: str) -> Sheet:
        sheet = Sheet(name=name)
        self.sheets[name] = sheet
        return sheet

    def set_formula(self, address: CellAddress, formula: str) -> None:
        sheet = self.sheets.setdefault(address.sheet, Sheet(name=address.sheet))
        sheet.set_cell(address.column, address.row, Cell(formula=formula))

    def set_literal(self, address: CellAddress, value: CellValue) -> None:
        sheet = self.sheets.setdefault(address.sheet, Sheet(name=address.sheet))
        sheet.set_cell(address.column, address.row, Cell(value=value))

    def get_formula(self, address: CellAddress) -> Optional[str]:
        sheet = self.sheets.get(address.sheet)
        if sheet is None:
            return None
        return sheet.get_cell(address.column, address.row).formula

    def get_cached_value(self, address: CellAddress) -> CellValue:
        sheet = self.sheets.get(address.sheet)
        if sheet is None:
            return ErrorValue(ErrorCode.REF)
        return sheet.get_cell(address.column, address.row).value

    def iter_formula_addresses(self) -> Iterator[Tuple[CellAddress, str]]:
        for sheet_name, sheet in self.sheets.items():
            for (column, row), cell in sheet.cells.items():
                if cell.formula is not None:
                    yield CellAddress(
                        sheet=sheet_name, column=column, row=row
                    ), cell.formula


def evaluate(node: Node, context: EvaluationContext) -> CellValue:
    """The interpreter: walk one formula AST to a lattice value. Every
    branch here is a value-in, value-out mapping — no branch may raise for
    a well-formed AST; :func:`_evaluate_cell` is the only place an
    exception is ever caught, as a safety net for the case that indicates a
    bug in this function, not in a user's formula."""
    if isinstance(node, NumberLiteral):
        return node.value
    if isinstance(node, TextLiteral):
        return node.value
    if isinstance(node, BooleanLiteral):
        return node.value
    if isinstance(node, ErrorLiteral):
        return ErrorValue(node.code)
    if isinstance(node, CellReferenceNode):
        return context.get_cell_value(node.address)
    if isinstance(node, RangeReferenceNode):
        # A bare range used where a scalar is expected (e.g. ``=A1:A3``) —
        # spreadsheets treat this as a #VALUE! error, not an implicit sum.
        return ErrorValue(ErrorCode.VALUE)
    if isinstance(node, UnaryOpNode):
        return _evaluate_unary(node, context)
    if isinstance(node, BinaryOpNode):
        return _evaluate_binary(node, context)
    if isinstance(node, FunctionCallNode):
        return _evaluate_function_call(node, context)
    raise AssertionError(f"unknown AST node type: {type(node)!r}")  # engine bug


def _evaluate_unary(node: UnaryOpNode, context: EvaluationContext) -> CellValue:
    operand = evaluate(node.operand, context)
    if is_error(operand):
        return operand
    number = coerce_to_number(operand)
    if is_error(number):
        return number
    return -number if node.operator == "-" else number


def _apply_comparison(operator: str, comparison: int) -> bool:
    if operator == "=":
        return comparison == 0
    if operator == "<>":
        return comparison != 0
    if operator == "<":
        return comparison < 0
    if operator == ">":
        return comparison > 0
    if operator == "<=":
        return comparison <= 0
    return comparison >= 0  # ">="


def _evaluate_binary(node: BinaryOpNode, context: EvaluationContext) -> CellValue:
    left = evaluate(node.left, context)
    if is_error(left):
        return left
    right = evaluate(node.right, context)
    if is_error(right):
        return right

    if node.operator in _COMPARISON_OPERATORS:
        comparison = compare_values(left, right)
        if is_error(comparison):
            return comparison
        return _apply_comparison(node.operator, comparison)

    if node.operator == "&":
        return coerce_to_text(left) + coerce_to_text(right)

    left_number = coerce_to_number(left)
    if is_error(left_number):
        return left_number
    right_number = coerce_to_number(right)
    if is_error(right_number):
        return right_number

    if node.operator == "+":
        return left_number + right_number
    if node.operator == "-":
        return left_number - right_number
    if node.operator == "*":
        return left_number * right_number
    if node.operator == "/":
        if right_number == 0:
            return ErrorValue(ErrorCode.DIV0)
        return left_number / right_number
    if node.operator == "^":
        try:
            return float(left_number) ** float(right_number)
        except (OverflowError, ValueError):
            return ErrorValue(ErrorCode.NUMBER)
    raise AssertionError(f"unknown operator: {node.operator!r}")  # engine bug


def _evaluate_function_call(
    node: FunctionCallNode, context: EvaluationContext
) -> CellValue:
    spec = get_function(node.name)
    if spec is None:
        return ErrorValue(ErrorCode.NAME)
    argument_count = len(node.arguments)
    too_few = argument_count < spec.minimum_arguments
    too_many = (
        spec.maximum_arguments is not None and argument_count > spec.maximum_arguments
    )
    if too_few or too_many:
        return ErrorValue(ErrorCode.VALUE)
    return spec.implementation(node.arguments, context, evaluate)


def _evaluate_cell(
    address: CellAddress,
    workbook: Workbook,
    computed_values: Dict[CellAddress, CellValue],
    now: datetime.datetime,
) -> CellValue:
    formula = workbook.get_formula(address)
    if formula is None:
        return workbook.get_cached_value(address)

    context = EvaluationContext(
        workbook=workbook,
        current_sheet=address.sheet,
        computed_values=computed_values,
        now=now,
    )
    try:
        # No ``default_sheet`` here: an unqualified reference in the AST
        # stays unqualified (``sheet is None``); ``EvaluationContext.
        # get_cell_value`` resolves it against ``current_sheet`` at lookup
        # time instead, so a formula's own text is never mutated just by
        # being evaluated.
        formula_ast = parse_formula(formula)
    except FormulaSyntaxError:
        return ErrorValue(ErrorCode.VALUE)

    try:
        return evaluate(formula_ast, context)
    except Exception:
        # A raised exception here is a bug in this engine, never in a
        # user's formula (requirement #3) — it must degrade the ONE broken
        # cell to an error value, not abort the whole recalculation pass.
        return ErrorValue(ErrorCode.VALUE)


def recalculate(
    workbook: Workbook, changed_cells: Iterable[CellAddress], now: datetime.datetime
) -> Dict[CellAddress, CellValue]:
    """Recalculate the dirty sub-graph reachable from ``changed_cells`` and
    write the results back into ``workbook`` as the new cached values.
    Returns only the recalculated cells, not the whole workbook."""
    changed = list(changed_cells)
    formulas = dict(workbook.iter_formula_addresses())
    graph = build_dependency_graph(formulas)
    for address in changed:
        graph.add_node(address)

    dirty = compute_dirty_closure(graph, changed)
    ordered, cyclic = topological_order(graph, dirty)

    computed_values: Dict[CellAddress, CellValue] = {
        address: ErrorValue(ErrorCode.CYCLE) for address in cyclic
    }
    for address in ordered:
        computed_values[address] = _evaluate_cell(
            address, workbook, computed_values, now
        )

    results = {address: computed_values[address] for address in dirty}
    for address, value in results.items():
        sheet = workbook.sheets.setdefault(address.sheet, Sheet(name=address.sheet))
        cell = sheet.get_cell(address.column, address.row)
        sheet.set_cell(address.column, address.row, replace(cell, value=value))
    return results


def translate_formula_for_copy(formula: str, row_delta: int, column_delta: int) -> str:
    """Copy/paste reference translation: relative references shift by the
    paste delta, ``$``-absolute references stay put."""
    formula_ast = parse_formula(formula)
    translated = transform_references(
        formula_ast,
        lambda address: translate_address_for_copy(address, row_delta, column_delta),
    )
    return serialize(translated)


def _same_sheet(address: CellAddress, sheet_name: str) -> bool:
    """Whether ``address`` — resolved against ``sheet_name`` if unqualified
    — targets ``sheet_name``. A row/column insert or delete on one sheet
    must not shift a reference that explicitly names a different sheet."""
    return resolve_address(address, sheet_name).sheet == sheet_name


def _structural_edit_transform(sheet_name: str, shift):
    """Build a reference-transform function for a row/column insert or
    delete: references into ``sheet_name`` are shifted (or turned into
    ``#REF!`` if their target was deleted); references into any other,
    explicitly-qualified sheet pass through unchanged."""

    def transform(address: CellAddress):
        if not _same_sheet(address, sheet_name):
            return address
        return shift(address)

    return transform


def translate_formula_for_row_insert(
    formula: str, sheet_name: str, at_row: int, count: int
) -> str:
    formula_ast = parse_formula(formula)
    transform = _structural_edit_transform(
        sheet_name, lambda address: shift_address_for_row_insert(address, at_row, count)
    )
    return serialize(transform_references(formula_ast, transform))


def translate_formula_for_row_delete(
    formula: str, sheet_name: str, at_row: int, count: int
) -> str:
    formula_ast = parse_formula(formula)
    transform = _structural_edit_transform(
        sheet_name, lambda address: shift_address_for_row_delete(address, at_row, count)
    )
    return serialize(transform_references(formula_ast, transform))


def translate_formula_for_column_insert(
    formula: str, sheet_name: str, at_column: int, count: int
) -> str:
    formula_ast = parse_formula(formula)
    transform = _structural_edit_transform(
        sheet_name,
        lambda address: shift_address_for_column_insert(address, at_column, count),
    )
    return serialize(transform_references(formula_ast, transform))


def translate_formula_for_column_delete(
    formula: str, sheet_name: str, at_column: int, count: int
) -> str:
    formula_ast = parse_formula(formula)
    transform = _structural_edit_transform(
        sheet_name,
        lambda address: shift_address_for_column_delete(address, at_column, count),
    )
    return serialize(transform_references(formula_ast, transform))
