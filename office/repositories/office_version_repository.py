"""``OfficeVersionRepository`` — append-only access to version history
(S147-1)."""
from typing import List, Optional

from plugins.office.office.models.office_version import OfficeVersion

FIRST_VERSION_NO = 1


class OfficeVersionRepository:
    def __init__(self, session) -> None:
        self.session = session

    def add(self, version: OfficeVersion) -> OfficeVersion:
        self.session.add(version)
        self.session.flush()
        return version

    def find_by_id(self, version_id) -> Optional[OfficeVersion]:
        return (
            self.session.query(OfficeVersion)
            .filter(OfficeVersion.id == version_id)
            .first()
        )

    def find_by_document_id(self, document_id) -> List[OfficeVersion]:
        return (
            self.session.query(OfficeVersion)
            .filter(OfficeVersion.document_id == document_id)
            .order_by(OfficeVersion.version_no.asc())
            .all()
        )

    def find_by_document_and_version_no(
        self, document_id, version_no: int
    ) -> Optional[OfficeVersion]:
        return (
            self.session.query(OfficeVersion)
            .filter(
                OfficeVersion.document_id == document_id,
                OfficeVersion.version_no == version_no,
            )
            .first()
        )

    def next_version_no(self, document_id) -> int:
        latest = (
            self.session.query(OfficeVersion)
            .filter(OfficeVersion.document_id == document_id)
            .order_by(OfficeVersion.version_no.desc())
            .first()
        )
        return (latest.version_no + 1) if latest else FIRST_VERSION_NO
