"""The purity oracle.

``office/sheet/`` (and ``office/sheet/functions/``) must import nothing from
``vbwd``, Flask, the ORM, the rest of the ``office`` plugin, the wall clock,
or a global RNG. This is not style policing: determinism is what makes
recalculation reproducible across boxes, and it is what lets this engine be
tested with zero infrastructure. If this test goes red, the product claim
goes with it. Modelled directly on
``plugins/bdv/tests/unit/test_core_purity.py``.
"""
import ast
import pathlib

import pytest

SHEET_PACKAGE = pathlib.Path(__file__).resolve().parents[3] / "office" / "sheet"

#: Import roots that would destroy determinism or couple the engine to infra.
BANNED_IMPORT_ROOTS = {
    "vbwd",
    "flask",
    "sqlalchemy",
    "alembic",
    "redis",
    "requests",
    "httpx",
    "openai",
    "anthropic",
    "plugins",
}

#: Calls that introduce hidden, non-reproducible state.
BANNED_CALLS = {
    ("random", "random"),
    ("random", "randint"),
    ("random", "choice"),
    ("random", "shuffle"),
    ("random", "seed"),
    ("time", "time"),
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("uuid", "uuid4"),
}


def sheet_modules():
    return sorted(SHEET_PACKAGE.rglob("*.py"))


def test_sheet_package_exists():
    assert SHEET_PACKAGE.is_dir(), "the pure formula engine package must exist"
    assert sheet_modules(), "…and contain modules"


@pytest.mark.parametrize(
    "module", sheet_modules(), ids=lambda p: str(p.relative_to(SHEET_PACKAGE))
)
def test_no_infrastructure_imports(module):
    tree = ast.parse(module.read_text(), filename=str(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert (
                    root not in BANNED_IMPORT_ROOTS
                ), f"{module.name} imports {alias.name!r} — the engine must stay pure"
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside sheet/ is fine
                continue
            root = (node.module or "").split(".")[0]
            assert (
                root not in BANNED_IMPORT_ROOTS
            ), f"{module.name} imports from {node.module!r} — the engine must stay pure"


@pytest.mark.parametrize(
    "module", sheet_modules(), ids=lambda p: str(p.relative_to(SHEET_PACKAGE))
)
def test_no_clock_reads_or_global_randomness(module):
    """Date functions must read ``EvaluationContext.now`` (injected by the
    caller), never the wall clock — and nothing here may draw from a global,
    unseeded RNG. A seeded ``random.Random(seed)`` instance would still be
    reproducible and is not what this check is about; it is about the
    module-level/global functions that no replay could ever reproduce."""
    tree = ast.parse(module.read_text(), filename=str(module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            assert (
                pair not in BANNED_CALLS
            ), f"{module.name} calls {pair[0]}.{pair[1]}() — non-reproducible"


def test_engine_is_importable_and_usable_with_no_flask_app_no_db_nothing():
    """The strongest proof: import and evaluate a formula with no Flask
    app, no database, no fixtures — a plain Python process."""
    import datetime

    from plugins.office.office.sheet import engine
    from plugins.office.office.sheet.cell import parse_cell_reference

    workbook = engine.Workbook()
    workbook.add_sheet("Sheet1")
    address = parse_cell_reference("A1", default_sheet="Sheet1")
    workbook.set_formula(address, "=1+2*3")

    results = engine.recalculate(workbook, [address], now=datetime.datetime(2026, 1, 1))

    assert results[address] == 7.0
