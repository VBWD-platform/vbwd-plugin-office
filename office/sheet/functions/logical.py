"""Logical functions: ``IF``, ``IFS``, ``AND``, ``OR``, ``NOT``.

PURE — see ``office/sheet/__init__.py``.

``IF``/``IFS`` evaluate only the branch actually taken — this is real
spreadsheet behaviour (a formula like ``IF(B2=0, 0, A2/B2)`` must not blow
up on the untaken ``A2/B2``), and it is why function implementations
receive the argument ASTs plus the interpreter's ``evaluate`` rather than
pre-evaluated values.
"""
from __future__ import annotations

from ..values import ErrorCode, ErrorValue, coerce_to_boolean, is_error
from ._registry import iterate_argument_values, register_function


@register_function("IF", 2, 3)
def if_function(argument_nodes, context, evaluate):
    condition_value = evaluate(argument_nodes[0], context)
    if is_error(condition_value):
        return condition_value
    condition = coerce_to_boolean(condition_value)
    if is_error(condition):
        return condition
    if condition:
        return evaluate(argument_nodes[1], context)
    if len(argument_nodes) == 3:
        return evaluate(argument_nodes[2], context)
    return False


@register_function("IFS", 2, None)
def ifs_function(argument_nodes, context, evaluate):
    if len(argument_nodes) % 2 != 0:
        return ErrorValue(ErrorCode.VALUE)
    for condition_node, value_node in zip(argument_nodes[0::2], argument_nodes[1::2]):
        condition_value = evaluate(condition_node, context)
        if is_error(condition_value):
            return condition_value
        condition = coerce_to_boolean(condition_value)
        if is_error(condition):
            return condition
        if condition:
            return evaluate(value_node, context)
    return ErrorValue(ErrorCode.NOT_AVAILABLE)


@register_function("AND", 1, None)
def and_function(argument_nodes, context, evaluate):
    result = True
    for argument in iterate_argument_values(argument_nodes, context, evaluate):
        if is_error(argument.value):
            return argument.value
        boolean_value = coerce_to_boolean(argument.value)
        if is_error(boolean_value):
            return boolean_value
        result = result and boolean_value
    return result


@register_function("OR", 1, None)
def or_function(argument_nodes, context, evaluate):
    result = False
    for argument in iterate_argument_values(argument_nodes, context, evaluate):
        if is_error(argument.value):
            return argument.value
        boolean_value = coerce_to_boolean(argument.value)
        if is_error(boolean_value):
            return boolean_value
        result = result or boolean_value
    return result


@register_function("NOT", 1, 1)
def not_function(argument_nodes, context, evaluate):
    value = evaluate(argument_nodes[0], context)
    if is_error(value):
        return value
    boolean_value = coerce_to_boolean(value)
    if is_error(boolean_value):
        return boolean_value
    return not boolean_value
