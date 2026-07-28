"""``populate_db`` grants ``office.use`` additively and idempotently (S147-00),
and (S147-1) seeds one demo folder + text file for the platform's standard
demo user through ``OfficeDocumentService`` — create-only, never a raw INSERT.
"""
from uuid import uuid4

from vbwd.models.role import Permission
from vbwd.models.user import User
from vbwd.models.user_access_level import AccessLevel
from vbwd.services.rbac_seeder import seed_default_rbac

from plugins.office import populate_db
from plugins.office.office.models.office_node import (
    NODE_KIND_DOCUMENT,
    NODE_KIND_FOLDER,
)
from plugins.office.office.repositories.office_node_repository import (
    OfficeNodeRepository,
)


def _seed_core_access_levels(db):
    """The plugin's seeder only ever ADDS to an existing access level (it
    never creates one) — the levels themselves are core baseline data the
    shared test schema does not seed by default. Seed them via the real core
    seeder (never a raw INSERT) so this test exercises the plugin's own
    additive path against realistic data."""
    seed_default_rbac(db.session)
    db.session.commit()


def test_seed_grants_office_use_to_logged_in_and_is_idempotent(db, app):
    _seed_core_access_levels(db)

    populate_db.populate(app)
    populate_db.populate(app)  # a second run must not duplicate anything

    permissions = db.session.query(Permission).filter_by(name="office.use").all()
    assert len(permissions) == 1

    logged_in = db.session.query(AccessLevel).filter_by(slug="logged-in").first()
    assert logged_in is not None
    assert any(p.name == "office.use" for p in logged_in.permissions)


def test_seed_never_removes_an_operator_grant_on_re_run(db, app):
    _seed_core_access_levels(db)
    populate_db.populate(app)

    logged_in = db.session.query(AccessLevel).filter_by(slug="logged-in").first()
    grants_before = {p.name for p in logged_in.permissions}

    populate_db.populate(app)

    logged_in_after = db.session.query(AccessLevel).filter_by(slug="logged-in").first()
    grants_after = {p.name for p in logged_in_after.permissions}
    assert grants_before <= grants_after


def _create_demo_user(db):
    user = User(id=uuid4(), email=populate_db.DEMO_USER_EMAIL, password_hash="x")
    db.session.add(user)
    db.session.commit()
    return user


def test_seed_creates_a_demo_folder_and_file_and_is_idempotent(db, app):
    _seed_core_access_levels(db)
    demo_user = _create_demo_user(db)

    populate_db.populate(app)
    populate_db.populate(app)  # a second run must not duplicate the vault

    node_repository = OfficeNodeRepository(db.session)
    root_children = node_repository.find_children(demo_user.id, None)
    folders = [n for n in root_children if n.kind == NODE_KIND_FOLDER]
    assert [f.name for f in folders] == [populate_db.DEMO_FOLDER_NAME]

    folder_children = node_repository.find_children(demo_user.id, folders[0].id)
    documents = [n for n in folder_children if n.kind == NODE_KIND_DOCUMENT]
    assert [d.name for d in documents] == [populate_db.DEMO_DOCUMENT_NAME]


def test_seed_skips_the_vault_when_the_demo_user_is_absent(db, app):
    _seed_core_access_levels(db)

    populate_db.populate(app)  # no demo user in this test's transaction

    permissions = db.session.query(Permission).filter_by(name="office.use").all()
    assert len(permissions) == 1  # the permission grant still ran
