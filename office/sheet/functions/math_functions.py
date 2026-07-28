"""Math functions: ``ROUND``, ``ABS``, ``MOD``, ``POWER``, ``SQRT``.

PURE — see ``office/sheet/__init__.py``.
"""
from __future__ import annotations

import math

from ..values import ErrorCode, ErrorValue, coerce_to_number, is_error
from ._registry import register_function


def _evaluate_number(node, context, evaluate):
    value = evaluate(node, context)
    if is_error(value):
        return value
    return coerce_to_number(value)


def _round_half_away_from_zero(value: float, digits: int) -> float:
    """Spreadsheets round halves away from zero (``ROUND(2.5, 0) == 3``,
    ``ROUND(-2.5, 0) == -3``) — different from Python's banker's rounding,
    so this cannot simply delegate to the builtin ``round``."""
    factor = 10.0**digits
    scaled = value * factor
    rounded = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    return rounded / factor


@register_function("ROUND", 2, 2)
def round_function(argument_nodes, context, evaluate):
    number = _evaluate_number(argument_nodes[0], context, evaluate)
    if is_error(number):
        return number
    digits_value = _evaluate_number(argument_nodes[1], context, evaluate)
    if is_error(digits_value):
        return digits_value
    return _round_half_away_from_zero(number, int(digits_value))


@register_function("ABS", 1, 1)
def abs_function(argument_nodes, context, evaluate):
    number = _evaluate_number(argument_nodes[0], context, evaluate)
    if is_error(number):
        return number
    return abs(number)


@register_function("MOD", 2, 2)
def mod_function(argument_nodes, context, evaluate):
    number = _evaluate_number(argument_nodes[0], context, evaluate)
    if is_error(number):
        return number
    divisor = _evaluate_number(argument_nodes[1], context, evaluate)
    if is_error(divisor):
        return divisor
    if divisor == 0:
        return ErrorValue(ErrorCode.DIV0)
    return number % divisor


@register_function("POWER", 2, 2)
def power_function(argument_nodes, context, evaluate):
    base = _evaluate_number(argument_nodes[0], context, evaluate)
    if is_error(base):
        return base
    exponent = _evaluate_number(argument_nodes[1], context, evaluate)
    if is_error(exponent):
        return exponent
    try:
        return float(base) ** float(exponent)
    except (OverflowError, ValueError):
        return ErrorValue(ErrorCode.NUMBER)


@register_function("SQRT", 1, 1)
def sqrt_function(argument_nodes, context, evaluate):
    number = _evaluate_number(argument_nodes[0], context, evaluate)
    if is_error(number):
        return number
    if number < 0:
        return ErrorValue(ErrorCode.NUMBER)
    return math.sqrt(number)
