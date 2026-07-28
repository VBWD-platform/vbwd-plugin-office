"""Lookup functions: ``VLOOKUP``, ``HLOOKUP``, ``INDEX``, ``MATCH``.

PURE — see ``office/sheet/__init__.py``.

Approximate-match (``VLOOKUP``/``HLOOKUP``/``MATCH`` with the default match
type) assumes the lookup column/row is sorted ascending — the same
precondition real spreadsheets document rather than enforce.
"""
from __future__ import annotations

from typing import List, Optional

from ..cell import CellAddress, CellRange
from ..parser import CellReferenceNode, Node, RangeReferenceNode
from ..values import (
    ErrorCode,
    ErrorValue,
    coerce_to_boolean,
    coerce_to_number,
    compare_values,
    is_error,
)
from ._registry import addresses_of_reference_node, register_function


def _range_of(node: Node) -> Optional[CellRange]:
    if isinstance(node, RangeReferenceNode):
        return node.cell_range
    if isinstance(node, CellReferenceNode):
        return CellRange(node.address, node.address)
    return None


def _table_rows(cell_range: CellRange) -> List[List[CellAddress]]:
    sheet = cell_range.start.sheet
    minimum_column, maximum_column = sorted(
        (cell_range.start.column, cell_range.end.column)
    )
    minimum_row, maximum_row = sorted((cell_range.start.row, cell_range.end.row))
    return [
        [
            CellAddress(sheet=sheet, column=column, row=row)
            for column in range(minimum_column, maximum_column + 1)
        ]
        for row in range(minimum_row, maximum_row + 1)
    ]


def _table_columns(cell_range: CellRange) -> List[List[CellAddress]]:
    return [list(column) for column in zip(*_table_rows(cell_range))]


def _values_equal(left, right) -> bool:
    comparison = compare_values(left, right)
    return not is_error(comparison) and comparison == 0


def _approximate_match(vectors: List[List[CellAddress]], lookup_value, context):
    best_index = None
    for index, vector in enumerate(vectors):
        cell_value = context.get_cell_value(vector[0])
        comparison = compare_values(cell_value, lookup_value)
        if is_error(comparison):
            continue
        if comparison <= 0:
            best_index = index
        else:
            break
    return best_index


def _exact_match(vectors: List[List[CellAddress]], lookup_value, context):
    for index, vector in enumerate(vectors):
        if _values_equal(context.get_cell_value(vector[0]), lookup_value):
            return index
    return None


def _lookup(argument_nodes, context, evaluate, *, vectors_of):
    lookup_value = evaluate(argument_nodes[0], context)
    if is_error(lookup_value):
        return lookup_value
    table_range = _range_of(argument_nodes[1])
    if table_range is None:
        return ErrorValue(ErrorCode.VALUE)
    index_value = evaluate(argument_nodes[2], context)
    index_number = coerce_to_number(index_value)
    if is_error(index_number):
        return index_number
    target_index = int(index_number)

    approximate = True
    if len(argument_nodes) == 4:
        range_lookup_value = evaluate(argument_nodes[3], context)
        approximate_result = coerce_to_boolean(range_lookup_value)
        if is_error(approximate_result):
            return approximate_result
        approximate = approximate_result

    vectors = vectors_of(table_range)
    if not vectors or target_index < 1 or target_index > len(vectors[0]):
        return ErrorValue(ErrorCode.REF)

    matched_index = (
        _approximate_match(vectors, lookup_value, context)
        if approximate
        else _exact_match(vectors, lookup_value, context)
    )
    if matched_index is None:
        return ErrorValue(ErrorCode.NOT_AVAILABLE)
    return context.get_cell_value(vectors[matched_index][target_index - 1])


@register_function("VLOOKUP", 3, 4)
def vlookup_function(argument_nodes, context, evaluate):
    return _lookup(argument_nodes, context, evaluate, vectors_of=_table_rows)


@register_function("HLOOKUP", 3, 4)
def hlookup_function(argument_nodes, context, evaluate):
    return _lookup(argument_nodes, context, evaluate, vectors_of=_table_columns)


@register_function("INDEX", 2, 3)
def index_function(argument_nodes, context, evaluate):
    table_range = _range_of(argument_nodes[0])
    if table_range is None:
        return ErrorValue(ErrorCode.VALUE)

    row_value = evaluate(argument_nodes[1], context)
    row_number = coerce_to_number(row_value)
    if is_error(row_number):
        return row_number
    row_index = int(row_number)

    rows = _table_rows(table_range)
    row_count = len(rows)
    column_count = len(rows[0]) if rows else 0

    if len(argument_nodes) == 3:
        column_value = evaluate(argument_nodes[2], context)
        column_number = coerce_to_number(column_value)
        if is_error(column_number):
            return column_number
        column_index = int(column_number)
    elif row_count == 1 and column_count > 1:
        # A single-row range with only the row/column argument omitted:
        # the two-argument form addresses positions along that one row.
        column_index = row_index
        row_index = 1
    else:
        column_index = 1

    if (
        row_index < 1
        or row_index > row_count
        or column_index < 1
        or column_index > column_count
    ):
        return ErrorValue(ErrorCode.REF)
    return context.get_cell_value(rows[row_index - 1][column_index - 1])


@register_function("MATCH", 2, 3)
def match_function(argument_nodes, context, evaluate):
    lookup_value = evaluate(argument_nodes[0], context)
    if is_error(lookup_value):
        return lookup_value

    addresses = addresses_of_reference_node(argument_nodes[1])
    if not addresses:
        return ErrorValue(ErrorCode.VALUE)

    match_type = 1
    if len(argument_nodes) == 3:
        match_type_value = evaluate(argument_nodes[2], context)
        match_type_number = coerce_to_number(match_type_value)
        if is_error(match_type_number):
            return match_type_number
        match_type = int(match_type_number)

    if match_type == 0:
        for position, address in enumerate(addresses, start=1):
            if _values_equal(context.get_cell_value(address), lookup_value):
                return float(position)
        return ErrorValue(ErrorCode.NOT_AVAILABLE)

    if match_type not in (1, -1):
        return ErrorValue(ErrorCode.NUMBER)

    best_position = None
    for position, address in enumerate(addresses, start=1):
        comparison = compare_values(context.get_cell_value(address), lookup_value)
        if is_error(comparison):
            continue
        is_candidate = comparison <= 0 if match_type == 1 else comparison >= 0
        if is_candidate:
            best_position = position
        else:
            break
    if best_position is None:
        return ErrorValue(ErrorCode.NOT_AVAILABLE)
    return float(best_position)
