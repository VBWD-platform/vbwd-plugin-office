"""Test fixtures for the office (VBWD Office) plugin tests.

Mirrors the pattern from ``plugins/bdv/tests/conftest.py`` — session-scoped
Flask app bound to a ``<dbname>_test`` database, function-scoped ``db``
fixture that isolates each test in a rolled-back transaction.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

os.environ["FLASK_ENV"] = "testing"
os.environ["TESTING"] = "true"


def _test_db_url() -> str:
    base = os.getenv("DATABASE_URL", "postgresql://vbwd:vbwd@postgres:5432/vbwd")
    prefix, _, dbname = base.rpartition("/")
    dbname = dbname.split("?")[0]
    return f"{prefix}/{dbname}_test"


def _ensure_test_db(url: str) -> None:
    from sqlalchemy import create_engine, text

    main_url = url.rsplit("/", 1)[0] + "/postgres"
    dbname = url.rsplit("/", 1)[1].split("?")[0]
    engine = create_engine(main_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        engine.dispose()


def _ensure_office_enabled(flask_app) -> None:
    """Enable ``office`` so ``on_enable`` runs and registers the DI provider.

    A fresh per-plugin CI clone has no ``plugins.json``, so the plugin is
    discovered-but-not-enabled and its registrations never fire. Idempotent —
    a no-op when the plugin is already enabled (e.g. local dev state via the
    shared manifest).
    """
    from vbwd.plugins.base import PluginStatus

    manager = getattr(flask_app, "plugin_manager", None)
    if manager is None:
        return
    with flask_app.app_context():
        plugin = manager.get_plugin("office")
        if plugin is None or plugin.status == PluginStatus.ENABLED:
            return
        try:
            manager.enable_plugin("office")
        except ValueError:
            if plugin.status == PluginStatus.INITIALIZED:
                plugin.enable()


@pytest.fixture(scope="session")
def app():
    from vbwd.app import create_app

    url = _test_db_url()
    _ensure_test_db(url)
    test_config = {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": url,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "RATELIMIT_ENABLED": False,
        "RATELIMIT_STORAGE_URL": "memory://",
    }
    flask_app = create_app(test_config)

    with flask_app.app_context():
        from vbwd.extensions import db as _db
        from vbwd.testing.integration_db import ensure_schema_and_baseline

        ensure_schema_and_baseline(_db)

    _ensure_office_enabled(flask_app)

    yield flask_app

    with flask_app.app_context():
        from vbwd.extensions import db as _db

        _db.engine.dispose()


@pytest.fixture
def db(app):
    """Isolate each test in a rolled-back transaction (self-cleaning, no wipe).

    The schema + baseline reference rows are built once in the ``app``
    fixture; each test runs inside a transaction that is rolled back at
    teardown, so nothing it writes persists.
    """
    from vbwd.extensions import db as _db

    with app.app_context():
        from vbwd.testing.integration_db import rollback_isolation

        with rollback_isolation(_db):
            yield _db


@pytest.fixture
def client(app):
    return app.test_client()
