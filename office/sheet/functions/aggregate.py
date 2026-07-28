"""Aggregate functions: ``SUM``, ``AVERAGE``, ``MIN``, ``MAX``, ``COUNT``,
``COUNTA``, ``SUMIF``, ``COUNTIF``.

PURE — see ``office/sheet/__init__.py``.

The coercion rule these functions share (requirement #5 in the sprint doc):
a value read from a cell/range reference that is not a number is silently
ignored by ``SUM``/``AVERAGE``/``MIN``/``MAX``; a value passed as a direct
literal argument is coerced, and a failed coercion is an error
(``SUM("5", 1)`` is ``6``; ``SUM(A1:A2)`` with a text cell skips it).
``COUNT``/``COUNTA`` never propagate errors — matching real spreadsheet
behaviour, an error-valued cell is simply "not a number" / "not blank".
"""
from __future__ import annotations

import fnmatch
import re
from typing import List

from ..values import (
    ErrorCode,
    ErrorValue,
    coerce_to_number,
    coerce_to_text,
    is_blank,
    is_error,
    is_number,
)
from ._registry import (
    addresses_of_reference_node,
    iterate_argument_values,
    register_function,
)


@register_function("SUM", 1, None)
def sum_function(argument_nodes, context, evaluate):
    total = 0.0
    for argument in iterate_argument_values(argument_nodes, context, evaluate):
        if is_error(argument.value):
            return argument.value
        if argument.from_cell_reference:
            if is_number(argument.value):
                total += argument.value
            continue
        number = coerce_to_number(argument.value)
        if is_error(number):
            return number
        total += number
    return total


@register_function("AVERAGE", 1, None)
def average_function(argument_nodes, context, evaluate):
    total = 0.0
    count = 0
    for argument in iterate_argument_values(argument_nodes, context, evaluate):
        if is_error(argument.value):
            return argument.value
        if argument.from_cell_reference:
            if is_number(argument.value):
                total += argument.value
                count += 1
            continue
        number = coerce_to_number(argument.value)
        if is_error(number):
            return number
        total += number
        count += 1
    if count == 0:
        return ErrorValue(ErrorCode.DIV0)
    return total / count


@register_function("MIN", 1, None)
def min_function(argument_nodes, context, evaluate):
    return _extreme(argument_nodes, context, evaluate, minimum=True)


@register_function("MAX", 1, None)
def max_function(argument_nodes, context, evaluate):
    return _extreme(argument_nodes, context, evaluate, minimum=False)


def _extreme(argument_nodes, context, evaluate, *, minimum: bool):
    numbers: List[float] = []
    for argument in iterate_argument_values(argument_nodes, context, evaluate):
        if is_error(argument.value):
            return argument.value
        if argument.from_cell_reference:
            if is_number(argument.value):
                numbers.append(float(argument.value))
            continue
        number = coerce_to_number(argument.value)
        if is_error(number):
            return number
        numbers.append(number)
    if not numbers:
        return 0.0
    return min(numbers) if minimum else max(numbers)


@register_function("COUNT", 1, None)
def count_function(argument_nodes, context, evaluate):
    total = 0
    for argument in iterate_argument_values(argument_nodes, context, evaluate):
        if is_number(argument.value):
            total += 1
    return float(total)


@register_function("COUNTA", 1, None)
def counta_function(argument_nodes, context, evaluate):
    total = 0
    for argument in iterate_argument_values(argument_nodes, context, evaluate):
        if not is_blank(argument.value):
            total += 1
    return float(total)


_COMPARISON_CRITERIA_PATTERN = re.compile(r"^(<=|>=|<>|=|<|>)(.*)$")


def _matches_criteria(cell_value, criteria_value) -> bool:
    """Evaluate one ``SUMIF``/``COUNTIF`` criteria: a leading comparison
    operator (``>10``, ``<>0``, …) compares numerically when possible and
    falls back to a case-insensitive text comparison; anything else is a
    numeric-equality or wildcard (``*``/``?``) text match."""
    if is_error(cell_value):
        return False
    criteria_text = coerce_to_text(criteria_value)
    match = _COMPARISON_CRITERIA_PATTERN.match(criteria_text)
    if match:
        operator, operand_text = match.group(1), match.group(2)
        operand_number = coerce_to_number(operand_text)
        cell_number = coerce_to_number(cell_value)
        if is_error(operand_number) or is_error(cell_number):
            operand_compare = operand_text.strip().upper()
            cell_compare = coerce_to_text(cell_value).upper()
            if operator == "=":
                return cell_compare == operand_compare
            if operator == "<>":
                return cell_compare != operand_compare
            return False
        return {
            "=": cell_number == operand_number,
            "<>": cell_number != operand_number,
            "<": cell_number < operand_number,
            "<=": cell_number <= operand_number,
            ">": cell_number > operand_number,
            ">=": cell_number >= operand_number,
        }[operator]
    criteria_number = coerce_to_number(criteria_value)
    if is_number(cell_value) and not is_error(criteria_number):
        return float(cell_value) == criteria_number
    return fnmatch.fnmatch(coerce_to_text(cell_value).upper(), criteria_text.upper())


@register_function("SUMIF", 2, 3)
def sum_if_function(argument_nodes, context, evaluate):
    criteria_range_node = argument_nodes[0]
    criteria_addresses = addresses_of_reference_node(criteria_range_node)
    if not criteria_addresses:
        return ErrorValue(ErrorCode.VALUE)
    criteria_value = evaluate(argument_nodes[1], context)
    if is_error(criteria_value):
        return criteria_value
    sum_range_node = (
        argument_nodes[2] if len(argument_nodes) == 3 else criteria_range_node
    )
    sum_addresses = addresses_of_reference_node(sum_range_node)
    if not sum_addresses:
        return ErrorValue(ErrorCode.VALUE)
    total = 0.0
    for criteria_address, sum_address in zip(criteria_addresses, sum_addresses):
        cell_value = context.get_cell_value(criteria_address)
        if _matches_criteria(cell_value, criteria_value):
            sum_value = context.get_cell_value(sum_address)
            if is_number(sum_value):
                total += sum_value
    return total


@register_function("COUNTIF", 2, 2)
def count_if_function(argument_nodes, context, evaluate):
    criteria_addresses = addresses_of_reference_node(argument_nodes[0])
    if not criteria_addresses:
        return ErrorValue(ErrorCode.VALUE)
    criteria_value = evaluate(argument_nodes[1], context)
    if is_error(criteria_value):
        return criteria_value
    total = 0
    for address in criteria_addresses:
        if _matches_criteria(context.get_cell_value(address), criteria_value):
            total += 1
    return float(total)
