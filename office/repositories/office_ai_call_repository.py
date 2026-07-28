"""``OfficeAiCallRepository`` — data access for the S147-3 AI-call audit
trail; also the read side of the per-user monthly budget check
(``count_since``)."""
from typing import List

from plugins.office.office.models.office_ai_call import OfficeAiCall


class OfficeAiCallRepository:
    def __init__(self, session) -> None:
        self.session = session

    def add(self, call: OfficeAiCall) -> OfficeAiCall:
        self.session.add(call)
        self.session.flush()
        return call

    def count_since(self, user_id, since_datetime) -> int:
        """Number of calls (success + error alike) this user has made at or
        after ``since_datetime`` — the budget check's read (S147-3 control:
        a budget-exhausted request is never recorded, so this count only
        ever reflects calls that actually reached the provider)."""
        return (
            self.session.query(OfficeAiCall)
            .filter(
                OfficeAiCall.user_id == user_id,
                OfficeAiCall.created_at >= since_datetime,
            )
            .count()
        )

    def find_by_node_id(self, node_id) -> List[OfficeAiCall]:
        return (
            self.session.query(OfficeAiCall)
            .filter(OfficeAiCall.node_id == node_id)
            .order_by(OfficeAiCall.created_at.desc())
            .all()
        )
