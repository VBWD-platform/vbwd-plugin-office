"""Grant ``office.use`` to the fe-user access levels that should reach the app.

Why this exists: ``office.use`` gates ``GET /api/v1/office/*`` player routes
(``@require_user_permission``) once S147-1 adds them, but core's shipped
default access levels are a **core** data file and must not name a plugin's
vocabulary. So the plugin grants its own permission, additively, from its own
seeder (mirrors ``plugins/bdv/bdv/services/access_seeder.py``).

Additive and idempotent: it only ever ADDS ``office.use`` to an existing
level, never removes anything and never creates a level. An operator who wants
to gate VBWD Office behind a paid tier simply removes it from ``logged-in``.
"""
from typing import List, Tuple

#: Levels that can reach VBWD Office out of the box. Anyone logged in — the
#: permission exists so an operator CAN gate it, not because it is gated.
DEFAULT_USABLE_LEVELS: Tuple[str, ...] = (
    "logged-in",
    "subscribed-basic",
    "subscribed-pro",
)

USE_PERMISSION = "office.use"


def grant_use_permission(
    session, levels: Tuple[str, ...] = DEFAULT_USABLE_LEVELS
) -> List[str]:
    """Ensure ``office.use`` exists and is attached to the given access levels.

    Returns the slugs actually modified (empty on a re-run — it is idempotent).
    """
    from vbwd.models.role import Permission
    from vbwd.models.user_access_level import AccessLevel

    permission = (
        session.query(Permission).filter(Permission.name == USE_PERMISSION).first()
    )
    if permission is None:
        permission = Permission(
            name=USE_PERMISSION,
            description="Use VBWD Office (Space, Docs, Spreadsheets)",
            resource="office",
            action="use",
        )
        session.add(permission)
        session.flush()

    changed: List[str] = []
    for slug in levels:
        level = session.query(AccessLevel).filter(AccessLevel.slug == slug).first()
        if level is None:
            continue
        if any(p.name == USE_PERMISSION for p in level.permissions):
            continue
        level.permissions.append(permission)
        changed.append(slug)

    session.flush()
    return changed
