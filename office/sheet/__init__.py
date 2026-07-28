"""``office.sheet`` — the pure spreadsheet formula engine (S147-4).

**This package is PURE.** No module under ``office/sheet/`` (including
``functions/``) may import ``vbwd``, ``flask``, ``sqlalchemy``, or anything
else from the ``office`` plugin outside this package. No module may read the
wall clock (``datetime.now``/``utcnow``) or a global random source. This is
enforced by a dedicated oracle test —
``plugins/office/tests/unit/sheet/test_sheet_engine_purity.py`` — modelled on
the BDV engine's ``plugins/bdv/tests/unit/test_core_purity.py``.

Why purity is a product requirement, not a style preference:

* **Determinism.** A spreadsheet that recalculates differently on two boxes
  is broken in a way users will not report — they will just leave. Date/time
  functions therefore take an injected ``now`` (see
  ``functions.EvaluationContext.now``) rather than reading the clock
  themselves.
* **Zero-infra testability.** Every module here is testable with plain
  ``pytest``, no Flask app, no database, no fixtures beyond Python objects.
* **A ship-anywhere seam.** Nothing here is coupled to how a formula got to
  the server or how its result gets persisted — the same engine could run
  standalone, or move client-side, without a rewrite.

Module layout:

* ``cell.py``      — A1 references, ranges, absolute/relative markers, R1C1
                      conversion, and the address-transform rules for
                      copy/paste and row/column insert/delete.
* ``lexer.py``      — formula text -> token stream.
* ``parser.py``     — token stream -> AST (a Pratt-style, precedence
                      climbing parser). **No ``eval`` anywhere.**
* ``functions/``    — the function registry: one module per function
                      family, each function a single ``@register_function``
                      call (the OCP seam — adding a function never touches
                      ``engine.py``).
* ``graph.py``      — the dependency graph, dirty-subgraph computation,
                      topological recalculation order, and cycle detection.
* ``values.py``     — the value lattice (``number | text | boolean | date |
                      error | blank``) and its coercion rules.
* ``engine.py``     — the AST interpreter and ``recalculate(workbook,
                      changed_cells, now)``, the orchestration entry point.

A raised exception anywhere during formula evaluation is a bug in this
engine, never a user's formula — errors are values in the lattice
(``#DIV/0!``, ``#REF!``, ``#VALUE!``, ``#NAME?``, ``#N/A``, ``#CYCLE!``),
and they propagate as values, not exceptions.
"""
