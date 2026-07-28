"""VBWD Office seed — grants ``office.use`` and seeds one demo vault entry.

CREATE-ONLY and idempotent:

* re-running never overwrites an operator's runtime access-level grants
  (``grant_use_permission`` only ever adds);
* the demo vault (one "Welcome" folder + one text file) is seeded for the
  platform's standard demo user (``test@example.com``, the same account every
  other plugin's ``populate_db.py`` looks for) THROUGH
  ``OfficeDocumentService`` — never a raw ``INSERT`` — and only when that
  user exists AND no folder of that name is present yet at their vault root,
  so a second run is a no-op.
"""
from vbwd.extensions import db

DEMO_USER_EMAIL = "test@example.com"
DEMO_FOLDER_NAME = "Welcome"
DEMO_DOCUMENT_NAME = "getting-started.txt"
DEMO_DOCUMENT_CONTENT = (
    b"Welcome to VBWD Space.\n\n"
    b"This is your private vault: upload files, keep versions, and share "
    b"them (sharing arrives in a later slice).\n"
)


def populate(app=None):
    from plugins.office.office.services.access_seeder import grant_use_permission

    granted = grant_use_permission(db.session)
    db.session.commit()
    if granted:
        print(f"[office] granted office.use to: {', '.join(granted)}")
    else:
        print("[office] office.use already granted to every default level")

    _seed_demo_vault()


def _seed_demo_vault() -> None:
    """Seed one folder + one text file for the demo user, through
    ``OfficeDocumentService`` (never a raw INSERT). Skipped entirely when the
    demo user is absent (this seeder can run before/without the core demo-user
    seeder) or when the folder already exists (idempotent, create-only)."""
    from vbwd.models.user import User

    demo_user = db.session.query(User).filter_by(email=DEMO_USER_EMAIL).first()
    if demo_user is None:
        print(f"[office] no demo user '{DEMO_USER_EMAIL}' — skipping vault seed")
        return

    from plugins.office.office.repositories.office_node_repository import (
        OfficeNodeRepository,
    )

    existing_root_children = OfficeNodeRepository(db.session).find_children(
        demo_user.id, None
    )
    if any(node.name == DEMO_FOLDER_NAME for node in existing_root_children):
        print("[office] demo vault already seeded")
        return

    from plugins.office import build_document_service

    service = build_document_service()
    folder = service.create_folder(demo_user.id, DEMO_FOLDER_NAME, None)
    service.upload_document(
        demo_user.id, DEMO_DOCUMENT_NAME, folder.id, DEMO_DOCUMENT_CONTENT
    )
    db.session.commit()
    print(f"[office] seeded demo vault: {DEMO_FOLDER_NAME}/{DEMO_DOCUMENT_NAME}")


if __name__ == "__main__":
    from vbwd.app import create_app

    app = create_app()
    with app.app_context():
        populate(app)
