"""OfficeAiCall — audit row for one AI-helper invocation (S147-3).

"Every call recorded" from the sprint's control table, verbatim: which user,
which capability, an approximate token count, and the CONNECTION SLUG
actually used (never a connection id — the slug is the stable, portable
address; see ``ai_capabilities.py``). ``tokens`` is an approximation
(``len(text) // 4``-shaped heuristic computed in ``OfficeAiService``) because
the core LLM adapters (``vbwd/llm/adapter.py``) do not return provider usage
counts today — documented here rather than silently presented as exact.

A budget-exhausted request is NEVER recorded (no provider call was made, so
there is nothing to audit). A request that reaches the provider and fails is
recorded with ``status='error'`` and ``tokens=NULL`` — it still consumed a
slot of the caller's monthly budget, which is deliberate: a
capability that reliably errors must not become a way to bypass the budget
by design.
"""
from uuid import uuid4

from sqlalchemy.dialects.postgresql import UUID

from vbwd.extensions import db
from vbwd.utils.datetime_utils import utcnow

STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
ALLOWED_STATUSES = (STATUS_SUCCESS, STATUS_ERROR)

CAPABILITY_MAX_LENGTH = 32
CONNECTION_SLUG_MAX_LENGTH = 128
STATUS_MAX_LENGTH = 16


class OfficeAiCall(db.Model):  # type: ignore[name-defined]
    """One recorded AI-helper call against one Doc node."""

    __tablename__ = "office_ai_call"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("office_node.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    capability = db.Column(db.String(CAPABILITY_MAX_LENGTH), nullable=False)
    connection_slug = db.Column(db.String(CONNECTION_SLUG_MAX_LENGTH), nullable=False)
    tokens = db.Column(db.Integer, nullable=True)
    status = db.Column(
        db.String(STATUS_MAX_LENGTH), nullable=False, default=STATUS_SUCCESS
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "node_id": str(self.node_id),
            "user_id": str(self.user_id),
            "capability": self.capability,
            "connection_slug": self.connection_slug,
            "tokens": self.tokens,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<OfficeAiCall(node_id='{self.node_id}', capability='{self.capability}')>"
        )
