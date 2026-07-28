"""The function registry — the OCP seam for spreadsheet functions.

PURE — see ``office/sheet/__init__.py``.

Adding a function is ONE ``@register_function(name, minimum_arguments,
maximum_arguments)`` decorator on a plain implementation in the relevant
family module below. Nothing in ``engine.py`` or ``parser.py`` needs to
change — that is what makes this an Open/Closed seam rather than a growing
``if name == ...`` ladder.

Registry mechanics (``EvaluationContext``, ``register_function``, …) live in
``_registry.py``; this module only re-exports them and imports the family
modules for their registration side effect.
"""
from __future__ import annotations

from . import (
    aggregate,
    datetime_functions,
    logical,
    lookup,
    math_functions,
    text_functions,
)
from ._registry import (
    FUNCTION_REGISTRY,
    ArgumentValue,
    Evaluate,
    EvaluationContext,
    FunctionSpec,
    WorkbookLike,
    get_function,
    iterate_argument_values,
    register_function,
)

__all__ = [
    "EvaluationContext",
    "Evaluate",
    "FunctionSpec",
    "FUNCTION_REGISTRY",
    "register_function",
    "get_function",
    "ArgumentValue",
    "iterate_argument_values",
    "WorkbookLike",
    "aggregate",
    "datetime_functions",
    "logical",
    "lookup",
    "math_functions",
    "text_functions",
]
