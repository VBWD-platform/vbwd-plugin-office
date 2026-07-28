"""Dependency graph: recalculation visits precedents before dependents, and
touching one cell only recalculates its transitive dependents — not the
whole workbook."""
from plugins.office.office.sheet import engine
from plugins.office.office.sheet.cell import parse_cell_reference
from plugins.office.office.sheet.graph import (
    build_dependency_graph,
    compute_dirty_closure,
    topological_order,
)

from ._helpers import DEFAULT_NOW, SHEET_NAME


def _address(reference_text):
    return parse_cell_reference(reference_text, default_sheet=SHEET_NAME)


def _chain_workbook():
    """A1 = 1 ; B1 = A1 + 1 ; C1 = B1 + 1 — a straight-line dependency chain."""
    workbook = engine.Workbook()
    workbook.add_sheet(SHEET_NAME)
    workbook.set_literal(_address("A1"), 1.0)
    workbook.set_formula(_address("B1"), "=A1+1")
    workbook.set_formula(_address("C1"), "=B1+1")
    return workbook


def test_recalculation_order_respects_precedents():
    workbook = _chain_workbook()
    formulas = dict(workbook.iter_formula_addresses())
    graph = build_dependency_graph(formulas)

    dirty = compute_dirty_closure(graph, [_address("A1")])
    ordered, cyclic = topological_order(graph, dirty)

    assert cyclic == set()
    assert ordered.index(_address("B1")) < ordered.index(_address("C1"))


def test_editing_a_leaf_only_recalculates_its_dependents():
    workbook = _chain_workbook()
    # An unrelated cell that nothing in the chain reads.
    workbook.set_literal(_address("D1"), 100.0)

    results = engine.recalculate(workbook, [_address("A1")], now=DEFAULT_NOW)

    assert set(results.keys()) == {_address("A1"), _address("B1"), _address("C1")}
    assert results[_address("B1")] == 2.0
    assert results[_address("C1")] == 3.0


def test_recalculation_returns_only_the_dirty_cells_not_the_whole_sheet():
    workbook = _chain_workbook()
    workbook.set_formula(_address("E1"), "=99")  # untouched by the A1 edit

    results = engine.recalculate(workbook, [_address("A1")], now=DEFAULT_NOW)

    assert _address("E1") not in results


def test_changing_a_middle_cell_recalculates_only_downstream_of_it():
    workbook = _chain_workbook()

    results = engine.recalculate(workbook, [_address("B1")], now=DEFAULT_NOW)

    # A1 itself is a precedent of B1, not a dependent — it must not be
    # re-touched by an edit to B1.
    assert _address("A1") not in results
    assert set(results.keys()) == {_address("B1"), _address("C1")}
