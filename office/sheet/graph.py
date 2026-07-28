"""Dependency graph over cell addresses: precedent/dependent edges parsed
from formula text, topological recalculation order, and cycle detection.

PURE — see ``office/sheet/__init__.py``.

Recalculation walks only the *dirty sub-graph* — ``changed_cells`` plus every
cell that transitively depends on one of them — never the whole workbook
(requirement #6 in the sprint doc). Cycles are detected within that
sub-graph and terminate cleanly: every cell in a cycle (and everything
downstream of it) is assigned ``#CYCLE!`` instead of being evaluated.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, FrozenSet, Iterable, Iterator, List, Set, Tuple

from .cell import CellAddress, resolve_address
from .parser import (
    BinaryOpNode,
    CellReferenceNode,
    FormulaSyntaxError,
    FunctionCallNode,
    Node,
    RangeReferenceNode,
    UnaryOpNode,
    parse_formula,
)


class DependencyGraph:
    """A directed graph: an edge ``precedent -> dependent`` means the
    dependent's formula reads the precedent, so the precedent must be
    evaluated first."""

    def __init__(self) -> None:
        self._precedents: Dict[CellAddress, Set[CellAddress]] = {}
        self._dependents: Dict[CellAddress, Set[CellAddress]] = {}

    def add_node(self, address: CellAddress) -> None:
        self._precedents.setdefault(address, set())
        self._dependents.setdefault(address, set())

    def add_precedent(self, dependent: CellAddress, precedent: CellAddress) -> None:
        self.add_node(dependent)
        self.add_node(precedent)
        self._precedents[dependent].add(precedent)
        self._dependents[precedent].add(dependent)

    def precedents_of(self, address: CellAddress) -> FrozenSet[CellAddress]:
        return frozenset(self._precedents.get(address, ()))

    def dependents_of(self, address: CellAddress) -> FrozenSet[CellAddress]:
        return frozenset(self._dependents.get(address, ()))

    def nodes(self) -> FrozenSet[CellAddress]:
        return frozenset(self._precedents.keys())


def iter_referenced_addresses(node: Node) -> Iterator[CellAddress]:
    """Walk an AST and yield every cell address it reads — a single-cell
    reference yields one address, a range reference yields every cell in
    the range."""
    if isinstance(node, CellReferenceNode):
        yield node.address
    elif isinstance(node, RangeReferenceNode):
        yield from node.cell_range.addresses()
    elif isinstance(node, UnaryOpNode):
        yield from iter_referenced_addresses(node.operand)
    elif isinstance(node, BinaryOpNode):
        yield from iter_referenced_addresses(node.left)
        yield from iter_referenced_addresses(node.right)
    elif isinstance(node, FunctionCallNode):
        for argument in node.arguments:
            yield from iter_referenced_addresses(argument)


def build_dependency_graph(formulas: Dict[CellAddress, str]) -> DependencyGraph:
    """Build the full precedent/dependent graph from every formula cell in
    the workbook. A malformed formula contributes no edges — it has no
    precedents the graph can name — and surfaces ``#VALUE!`` at evaluation
    time instead (see ``engine.py``)."""
    graph = DependencyGraph()
    for address, formula_text in formulas.items():
        graph.add_node(address)
        try:
            formula_ast = parse_formula(formula_text, default_sheet=address.sheet)
        except FormulaSyntaxError:
            continue
        for reference in iter_referenced_addresses(formula_ast):
            precedent = resolve_address(reference, address.sheet)
            graph.add_precedent(address, precedent)
    return graph


def compute_dirty_closure(
    graph: DependencyGraph, changed_cells: Iterable[CellAddress]
) -> Set[CellAddress]:
    """``changed_cells`` plus every cell that transitively depends on one of
    them — the only cells a recalculation pass needs to touch."""
    dirty: Set[CellAddress] = set(changed_cells)
    frontier: List[CellAddress] = list(dirty)
    while frontier:
        current = frontier.pop()
        for dependent in graph.dependents_of(current):
            if dependent not in dirty:
                dirty.add(dependent)
                frontier.append(dependent)
    return dirty


def _sort_key(address: CellAddress) -> Tuple[str, int, int]:
    return (address.sheet or "", address.row, address.column)


def topological_order(
    graph: DependencyGraph, nodes: Iterable[CellAddress]
) -> Tuple[List[CellAddress], Set[CellAddress]]:
    """Kahn's algorithm restricted to the induced subgraph of ``nodes``.

    Returns ``(ordered, cyclic)``: ``ordered`` is a valid evaluation order
    for the acyclic portion; ``cyclic`` is every node Kahn's algorithm could
    never dequeue — a cell genuinely on a cycle, or one that only depends on
    cycle cells within this node set. Ties are broken in a fixed,
    deterministic order (top-to-bottom, left-to-right) so recalculation
    never depends on Python's set-iteration order — required by the
    determinism guarantee (requirement #5).
    """
    node_set = set(nodes)
    in_degree: Dict[CellAddress, int] = {}
    for node in node_set:
        in_degree[node] = sum(
            1 for precedent in graph.precedents_of(node) if precedent in node_set
        )

    queue = deque(sorted((n for n in node_set if in_degree[n] == 0), key=_sort_key))
    ordered: List[CellAddress] = []
    while queue:
        current = queue.popleft()
        ordered.append(current)
        newly_ready = []
        for dependent in graph.dependents_of(current):
            if dependent not in node_set:
                continue
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                newly_ready.append(dependent)
        for node in sorted(newly_ready, key=_sort_key):
            queue.append(node)

    cyclic = node_set - set(ordered)
    return ordered, cyclic
