"""Cycle detection: a cycle among formulas yields ``#CYCLE!`` for every cell
on it (and everything downstream of it) and recalculation still terminates.
Exercised both with hand-written cases and a property test over random
dependency graphs — ``random`` is used only here, in the TEST file, which
is exempt from the pure package's purity oracle."""
import random

import pytest

from plugins.office.office.sheet import engine
from plugins.office.office.sheet.cell import parse_cell_reference
from plugins.office.office.sheet.graph import DependencyGraph, topological_order
from plugins.office.office.sheet.values import ErrorCode, ErrorValue

from ._helpers import DEFAULT_NOW, SHEET_NAME


def _address(reference_text):
    return parse_cell_reference(reference_text, default_sheet=SHEET_NAME)


def test_direct_self_reference_is_a_cycle_error():
    workbook = engine.Workbook()
    workbook.add_sheet(SHEET_NAME)
    workbook.set_formula(_address("A1"), "=A1+1")

    results = engine.recalculate(workbook, [_address("A1")], now=DEFAULT_NOW)

    assert results[_address("A1")] == ErrorValue(ErrorCode.CYCLE)


def test_two_cell_cycle_marks_both_cells_and_terminates():
    workbook = engine.Workbook()
    workbook.add_sheet(SHEET_NAME)
    workbook.set_formula(_address("A1"), "=B1+1")
    workbook.set_formula(_address("B1"), "=A1+1")

    results = engine.recalculate(workbook, [_address("A1")], now=DEFAULT_NOW)

    assert results[_address("A1")] == ErrorValue(ErrorCode.CYCLE)
    assert results[_address("B1")] == ErrorValue(ErrorCode.CYCLE)


def test_cell_downstream_of_a_cycle_is_also_a_cycle_error():
    workbook = engine.Workbook()
    workbook.add_sheet(SHEET_NAME)
    workbook.set_formula(_address("A1"), "=B1+1")
    workbook.set_formula(_address("B1"), "=A1+1")
    workbook.set_formula(_address("C1"), "=B1+1")  # reads a cyclic cell

    results = engine.recalculate(
        workbook, [_address("A1"), _address("C1")], now=DEFAULT_NOW
    )

    assert results[_address("C1")] == ErrorValue(ErrorCode.CYCLE)


def test_acyclic_neighbour_of_a_cycle_still_recalculates_correctly():
    workbook = engine.Workbook()
    workbook.add_sheet(SHEET_NAME)
    workbook.set_formula(_address("A1"), "=B1+1")
    workbook.set_formula(_address("B1"), "=A1+1")
    workbook.set_literal(_address("D1"), 41.0)
    workbook.set_formula(_address("E1"), "=D1+1")  # not connected to the cycle

    results = engine.recalculate(
        workbook, [_address("A1"), _address("D1")], now=DEFAULT_NOW
    )

    assert results[_address("E1")] == 42.0


def _build_random_dependency_graph(cell_count, edge_count, seed):
    """A random directed graph over ``cell_count`` cell addresses, used only
    to prove ``topological_order`` always terminates and classifies every
    node as either ordered or cyclic — never both, never neither."""
    random_generator = random.Random(seed)
    addresses = [
        parse_cell_reference(f"A{index}", default_sheet=SHEET_NAME)
        for index in range(1, cell_count + 1)
    ]
    graph = DependencyGraph()
    for address in addresses:
        graph.add_node(address)
    for _ in range(edge_count):
        dependent = random_generator.choice(addresses)
        precedent = random_generator.choice(addresses)
        graph.add_precedent(dependent, precedent)
    return graph, addresses


@pytest.mark.parametrize("seed", range(25))
def test_topological_order_always_terminates_on_random_graphs(seed):
    graph, addresses = _build_random_dependency_graph(
        cell_count=12, edge_count=20, seed=seed
    )

    ordered, cyclic = topological_order(graph, addresses)

    # Every node is classified exactly once — the algorithm neither drops a
    # node nor emits it twice, regardless of how many cycles the random
    # graph happens to contain.
    assert set(ordered) | cyclic == set(addresses)
    assert set(ordered).isdisjoint(cyclic)
    assert len(ordered) + len(cyclic) == len(addresses)


@pytest.mark.parametrize("seed", range(10))
def test_random_cyclic_workbook_recalculates_without_raising(seed):
    """The end-to-end property test: build a workbook whose formulas are
    literally the random graph's edges, and prove recalculation always
    terminates and never raises, however tangled the cycles."""
    random_generator = random.Random(seed)
    cell_count = 8
    workbook = engine.Workbook()
    workbook.add_sheet(SHEET_NAME)
    addresses = [_address(f"A{index}") for index in range(1, cell_count + 1)]

    for address in addresses:
        precedent_count = random_generator.randint(0, 2)
        precedents = random_generator.sample(addresses, k=precedent_count)
        formula_body = "+".join(f"A{p.row}" for p in precedents)
        formula = "=" + (f"{formula_body}+1" if precedents else "1")
        workbook.set_formula(address, formula)

    results = engine.recalculate(workbook, addresses, now=DEFAULT_NOW)

    assert set(results.keys()) == set(addresses)
    for value in results.values():
        assert value is not None
