"""Text functions: ``CONCAT``, ``LEFT``, ``RIGHT``, ``MID``, ``TRIM``,
``UPPER``, ``LOWER``, ``SUBSTITUTE``.

PURE — see ``office/sheet/__init__.py``.
"""
from __future__ import annotations

import re

from ..values import ErrorCode, ErrorValue, coerce_to_number, coerce_to_text, is_error
from ._registry import register_function

_INTERNAL_WHITESPACE_PATTERN = re.compile(r"\s+")


def _evaluate_text(node, context, evaluate):
    value = evaluate(node, context)
    if is_error(value):
        return value
    return coerce_to_text(value)


def _evaluate_count(node, context, evaluate):
    value = evaluate(node, context)
    if is_error(value):
        return value
    return coerce_to_number(value)


@register_function("CONCAT", 1, None)
def concat_function(argument_nodes, context, evaluate):
    parts = []
    for node in argument_nodes:
        text = _evaluate_text(node, context, evaluate)
        if is_error(text):
            return text
        parts.append(text)
    return "".join(parts)


@register_function("LEFT", 1, 2)
def left_function(argument_nodes, context, evaluate):
    text = _evaluate_text(argument_nodes[0], context, evaluate)
    if is_error(text):
        return text
    count = 1
    if len(argument_nodes) == 2:
        count_value = _evaluate_count(argument_nodes[1], context, evaluate)
        if is_error(count_value):
            return count_value
        count = int(count_value)
    if count < 0:
        return ErrorValue(ErrorCode.VALUE)
    return text[:count]


@register_function("RIGHT", 1, 2)
def right_function(argument_nodes, context, evaluate):
    text = _evaluate_text(argument_nodes[0], context, evaluate)
    if is_error(text):
        return text
    count = 1
    if len(argument_nodes) == 2:
        count_value = _evaluate_count(argument_nodes[1], context, evaluate)
        if is_error(count_value):
            return count_value
        count = int(count_value)
    if count < 0:
        return ErrorValue(ErrorCode.VALUE)
    if count == 0:
        return ""
    return text[-count:]


@register_function("MID", 3, 3)
def mid_function(argument_nodes, context, evaluate):
    text = _evaluate_text(argument_nodes[0], context, evaluate)
    if is_error(text):
        return text
    start_value = _evaluate_count(argument_nodes[1], context, evaluate)
    if is_error(start_value):
        return start_value
    length_value = _evaluate_count(argument_nodes[2], context, evaluate)
    if is_error(length_value):
        return length_value
    start = int(start_value)
    length = int(length_value)
    if start < 1 or length < 0:
        return ErrorValue(ErrorCode.VALUE)
    return text[start - 1 : start - 1 + length]


@register_function("TRIM", 1, 1)
def trim_function(argument_nodes, context, evaluate):
    text = _evaluate_text(argument_nodes[0], context, evaluate)
    if is_error(text):
        return text
    return _INTERNAL_WHITESPACE_PATTERN.sub(" ", text.strip())


@register_function("UPPER", 1, 1)
def upper_function(argument_nodes, context, evaluate):
    text = _evaluate_text(argument_nodes[0], context, evaluate)
    if is_error(text):
        return text
    return text.upper()


@register_function("LOWER", 1, 1)
def lower_function(argument_nodes, context, evaluate):
    text = _evaluate_text(argument_nodes[0], context, evaluate)
    if is_error(text):
        return text
    return text.lower()


@register_function("SUBSTITUTE", 3, 4)
def substitute_function(argument_nodes, context, evaluate):
    text = _evaluate_text(argument_nodes[0], context, evaluate)
    if is_error(text):
        return text
    old_text = _evaluate_text(argument_nodes[1], context, evaluate)
    if is_error(old_text):
        return old_text
    new_text = _evaluate_text(argument_nodes[2], context, evaluate)
    if is_error(new_text):
        return new_text
    if old_text == "":
        return text

    if len(argument_nodes) == 3:
        return text.replace(old_text, new_text)

    instance_value = _evaluate_count(argument_nodes[3], context, evaluate)
    if is_error(instance_value):
        return instance_value
    instance_number = int(instance_value)
    if instance_number < 1:
        return ErrorValue(ErrorCode.VALUE)

    segments = text.split(old_text)
    if instance_number >= len(segments):
        return text
    before = old_text.join(segments[:instance_number])
    after = old_text.join(segments[instance_number:])
    return before + new_text + after
