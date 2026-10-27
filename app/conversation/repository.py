from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.conversation.context import ConversationTurn
from app.core.errors import IntelligenceError


class ConversationUnavailable(IntelligenceError):
    status_code = 404
    code = "conversation_unavailable"


class ConversationForbidden(IntelligenceError):
    status_code = 403
    code = "conversation_forbidden"


class ConversationBusy(IntelligenceError):
    status_code = 409
    code = "conversation_busy"


class ConversationCapacity(IntelligenceError):
    status_code = 429
    code = "conversation_capacity"


class ConversationStorageUnavailable(IntelligenceError):
    status_code = 503
    code = "conversation_storage_unavailable"


@dataclass(frozen=True, slots=True, repr=False)
class ConversationEntry:
    conversation_id: str
    subject: str
    created_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None = None
    version: int = 0


class ConversationRepository(Protocol):
    async def create(self, subject: str) -> ConversationEntry: ...
    async def get(self, key: str, subject: str) -> ConversationEntry: ...
    async def get_current(self, subject: str) -> ConversationEntry: ...
    async def append_turn(
        self, key: str, subject: str, turn: ConversationTurn, expected_version: int
    ) -> int: ...
    async def close(self, key: str, subject: str) -> None: ...
    async def list_turns(
        self, key: str, subject: str, *, limit: int | None = None
    ) -> tuple[ConversationTurn, ...]: ...
    async def ready(self) -> None: ...
