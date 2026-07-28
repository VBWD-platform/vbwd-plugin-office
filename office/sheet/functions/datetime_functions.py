"""Date/time functions: ``DATE``, ``TIME``, ``YEAR``, ``MONTH``, ``DAY``,
``HOUR``, ``MINUTE``, ``SECOND``, ``NOW``, ``TODAY``, ``WEEKDAY``.

PURE — see ``office/sheet/__init__.py``. In particular: this module MUST
NEVER call ``datetime.now()``/``datetime.utcnow()`` — ``NOW``/``TODAY`` read
``context.now``, the caller-injected instant for this recalculation pass
(requirement #5, determinism). The purity oracle enforces this at the AST
level, not just by convention.
"""
from __future__ import annotations

import datetime

from ..values import (
    ErrorCode,
    ErrorValue,
    coerce_to_number,
    is_error,
    serial_to_date,
)
from ._registry import register_function


def _evaluate_number(node, context, evaluate):
    value = evaluate(node, context)
    if is_error(value):
        return value
    return coerce_to_number(value)


def _as_datetime(node, context, evaluate):
    """Resolve an argument to a ``datetime.datetime``, accepting either a
    date/date-time value or a plain serial-number value."""
    value = evaluate(node, context)
    if is_error(value):
        return value
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())
    number = coerce_to_number(value)
    if is_error(number):
        return number
    return serial_to_date(number)


def _normalise_date(year: int, month: int, day: int) -> datetime.date:
    """Excel's ``DATE`` rolls an out-of-range month/day into the following
    period (``DATE(2026, 13, 1)`` is January of 2027) rather than erroring —
    pure integer arithmetic, no calendar library needed."""
    total_months = year * 12 + (month - 1)
    normalised_year, normalised_month_index = divmod(total_months, 12)
    first_of_month = datetime.date(normalised_year, normalised_month_index + 1, 1)
    return first_of_month + datetime.timedelta(days=day - 1)


@register_function("DATE", 3, 3)
def date_function(argument_nodes, context, evaluate):
    year = _evaluate_number(argument_nodes[0], context, evaluate)
    if is_error(year):
        return year
    month = _evaluate_number(argument_nodes[1], context, evaluate)
    if is_error(month):
        return month
    day = _evaluate_number(argument_nodes[2], context, evaluate)
    if is_error(day):
        return day
    return _normalise_date(int(year), int(month), int(day))


@register_function("TIME", 3, 3)
def time_function(argument_nodes, context, evaluate):
    """Excel represents a time of day as the fraction of a 24-hour day it
    occupies — a plain number, not a distinct calendar type."""
    hour = _evaluate_number(argument_nodes[0], context, evaluate)
    if is_error(hour):
        return hour
    minute = _evaluate_number(argument_nodes[1], context, evaluate)
    if is_error(minute):
        return minute
    second = _evaluate_number(argument_nodes[2], context, evaluate)
    if is_error(second):
        return second
    total_seconds = hour * 3600 + minute * 60 + second
    return (total_seconds % 86400) / 86400.0


@register_function("YEAR", 1, 1)
def year_function(argument_nodes, context, evaluate):
    value = _as_datetime(argument_nodes[0], context, evaluate)
    if is_error(value):
        return value
    return float(value.year)


@register_function("MONTH", 1, 1)
def month_function(argument_nodes, context, evaluate):
    value = _as_datetime(argument_nodes[0], context, evaluate)
    if is_error(value):
        return value
    return float(value.month)


@register_function("DAY", 1, 1)
def day_function(argument_nodes, context, evaluate):
    value = _as_datetime(argument_nodes[0], context, evaluate)
    if is_error(value):
        return value
    return float(value.day)


@register_function("HOUR", 1, 1)
def hour_function(argument_nodes, context, evaluate):
    value = _as_datetime(argument_nodes[0], context, evaluate)
    if is_error(value):
        return value
    return float(value.hour)


@register_function("MINUTE", 1, 1)
def minute_function(argument_nodes, context, evaluate):
    value = _as_datetime(argument_nodes[0], context, evaluate)
    if is_error(value):
        return value
    return float(value.minute)


@register_function("SECOND", 1, 1)
def second_function(argument_nodes, context, evaluate):
    value = _as_datetime(argument_nodes[0], context, evaluate)
    if is_error(value):
        return value
    return float(value.second)


@register_function("NOW", 0, 0)
def now_function(argument_nodes, context, evaluate):
    return context.now


@register_function("TODAY", 0, 0)
def today_function(argument_nodes, context, evaluate):
    return context.now.date()


#: WEEKDAY "return type" argument: 1 = Sunday..Saturday as 1..7 (default),
#: 2 = Monday..Sunday as 1..7. Anything else is unsupported (documented,
#: not full Excel parity — see the sprint's function-coverage note).
_SUNDAY_START = 1
_MONDAY_START = 2


@register_function("WEEKDAY", 1, 2)
def weekday_function(argument_nodes, context, evaluate):
    value = _as_datetime(argument_nodes[0], context, evaluate)
    if is_error(value):
        return value
    return_type = _SUNDAY_START
    if len(argument_nodes) == 2:
        return_type_value = _evaluate_number(argument_nodes[1], context, evaluate)
        if is_error(return_type_value):
            return return_type_value
        return_type = int(return_type_value)
    python_weekday = value.weekday()  # Monday == 0 .. Sunday == 6
    if return_type == _MONDAY_START:
        return float(python_weekday + 1)
    if return_type == _SUNDAY_START:
        return float((python_weekday + 1) % 7 + 1)
    return ErrorValue(ErrorCode.NUMBER)
