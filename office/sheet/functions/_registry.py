"""The function registry mechanics: :class:`EvaluationContext`, the
``@register_function`` decorator and the argument-flattening helper every
family module builds on.

PURE — see ``office/sheet/__init__.py``.

Split out of ``functions/__init__.py`` so the family modules
(``aggregate.py``, ``logical.py``, …) can import from here without a
circular import through the package's own ``__init__.py``, and so
``__init__.py`` stays a plain, top-of-file re-export list.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional, Protocol

from ..cell import CellAddress, resolve_address
from ..parser import CellReferenceNode, Node, RangeReferenceNode
from ..values import CellValue


class WorkbookLike(Protocol):
    """The narrow slice of ``engine.Workbook`` a function may read — an
    Interface-Segregation seam that keeps this package independent of
    ``engine.py`` (which imports THIS package to resolve function calls)."""

    def get_cached_value(self, address: CellAddress) -> CellValue:
        ...


@dataclass
class EvaluationContext:
    """Everything a function implementation may read while evaluating one
    cell: the workbook (to look up other cells), the sheet an unqualified
    reference resolves against, the values already computed earlier in this
    recalculation pass, and the injected "now" — date functions read this,
    never the wall clock (determinism, requirement #5)."""

    workbook: WorkbookLike
    current_sheet: str
    computed_values: Dict[CellAddress, CellValue]
    now: datetime.datetime

    def get_cell_value(self, address: CellAddress) -> CellValue:
        resolved = resolve_address(address, self.current_sheet)
        if resolved in self.computed_values:
            return self.computed_values[resolved]
        return self.workbook.get_cached_value(resolved)


#: The interpreter's own node evaluator, passed into every function
#: implementation so functions that need lazy/short-circuit evaluation
#: (``IF``, ``IFS``) can choose which argument ASTs to evaluate, and in
#: which order, instead of every argument being evaluated eagerly upfront.
Evaluate = Callable[[Node, EvaluationContext], CellValue]


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    minimum_arguments: int
    maximum_arguments: Optional[int]
    implementation: Callable[[List[Node], EvaluationContext, Evaluate], CellValue]


FUNCTION_REGISTRY: Dict[str, FunctionSpec] = {}


def register_function(
    name: str, minimum_arguments: int, maximum_arguments: Optional[int] = None
):
    """Decorator: register ``func`` as the implementation of spreadsheet
    function ``name`` — arity is enforced by the caller (``engine.py``)
    before ``func`` ever runs, so implementations may assume their argument
    count is already valid."""

    def decorator(func):
        FUNCTION_REGISTRY[name.upper()] = FunctionSpec(
            name=name.upper(),
            minimum_arguments=minimum_arguments,
            maximum_arguments=maximum_arguments,
            implementation=func,
        )
        return func

    return decorator


def get_function(name: str) -> Optional[FunctionSpec]:
    return FUNCTION_REGISTRY.get(name.upper())


@dataclass(frozen=True)
class ArgumentValue:
    """One value pulled from a function's argument list, tagged with
    whether it came from a cell/range reference. ``SUM``/``AVERAGE``/
    ``COUNT`` treat the two sources differently — a text cell in a summed
    range is silently ignored, but a text literal passed directly
    (``SUM("5", 1)``) is coerced — so that distinction is captured once
    here rather than re-derived in every aggregate function (DRY)."""

    value: CellValue
    from_cell_reference: bool


def iterate_argument_values(
    argument_nodes: List[Node], context: EvaluationContext, evaluate: Evaluate
) -> Iterator[ArgumentValue]:
    """Flatten a function's argument list into individual values, expanding
    any range reference into one value per cell."""
    for node in argument_nodes:
        if isinstance(node, RangeReferenceNode):
            for address in node.cell_range.addresses():
                yield ArgumentValue(
                    context.get_cell_value(address), from_cell_reference=True
                )
        elif isinstance(node, CellReferenceNode):
            yield ArgumentValue(
                context.get_cell_value(node.address), from_cell_reference=True
            )
        else:
            yield ArgumentValue(evaluate(node, context), from_cell_reference=False)


def addresses_of_reference_node(node: Node) -> List[CellAddress]:
    """The cell addresses a range or single-cell reference node covers, or
    an empty list for any other node type. Shared by ``aggregate.py``
    (``SUMIF``/``COUNTIF``) and ``lookup.py`` (``VLOOKUP``/``MATCH``/…),
    which both need "the addresses this argument's reference covers" rather
    than the evaluated value (DRY)."""
    if isinstance(node, RangeReferenceNode):
        return list(node.cell_range.addresses())
    if isinstance(node, CellReferenceNode):
        return [node.address]
    return []
