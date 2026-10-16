from datetime import datetime
from uuid import UUID

from app.contracts.base import BackendContract


class DocumentPerson(BackendContract):
    email: str | None
    name: str | None


class Document(BackendContract):
    id: UUID
    original_file_name: str
    content_type: str | None
    size_bytes: int
    uploaded_at: datetime
    uploaded_by: DocumentPerson
    upload_batch_id: UUID
    is_current: bool
    replaced_at: datetime | None
    replaced_by_document_id: UUID | None
    in_initial_handoff: bool


class InitialHandoff(BackendContract):
    id: UUID
    sent_at: datetime
    sent_by: DocumentPerson
    document_ids: list[UUID]


class ProjectDocuments(BackendContract):
    documents: list[Document]
    initial_handoff: InitialHandoff | None
    can_modify: bool
    can_remove: bool
    can_send_to_budgets: bool
