import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic

from app.conversation.context import ConversationContext, ConversationTurn, check_context_text
from app.core.errors import IntelligenceError

MAX_CONVERSATIONS = 128
MAX_PER_SUBJECT = 3
MAX_TURNS = 16
MAX_CHARACTERS = 16_000


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


@dataclass(slots=True, repr=False)
class ConversationEntry:
    conversation_id: str
    subject: str
    created_at: datetime
    last_activity_at: datetime
    expires_at: datetime
    turns: tuple[ConversationTurn, ...] = ()


@dataclass(slots=True, repr=False)
class _Runtime:
    deadline: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    operations: set[asyncio.Task[object]] = field(default_factory=set)
    expiry: asyncio.Task[None] | None = None


class ConversationStore:
    """One event loop, fixed TTL, bounded entries and per-conversation serialization.

    Runtime locks/tasks and Live bindings are separate from conversational data.
    No credential access and no business/project resolution belongs here.
    """

    def __init__(self, ttl: int = 1800) -> None:
        if not 1 <= ttl <= 3600:
            raise ValueError("Invalid conversation lifetime")
        self._ttl = ttl
        self._entries: dict[str, ConversationEntry] = {}
        self._runtime: dict[str, _Runtime] = {}
        self._live: dict[str, str] = {}
        self._closed: dict[str, str] = {}
        self._cleanup: set[asyncio.Task[None]] = set()
        self.close_live: Callable[[str], Awaitable[None]] | None = None
        self._stopped = False

    def create(self, subject: str) -> ConversationEntry:
        self.sweep()
        if self._stopped:
            raise ConversationUnavailable()
        if (
            len(self._entries) + len(self._cleanup) >= MAX_CONVERSATIONS
            or sum(entry.subject == subject for entry in self._entries.values()) >= MAX_PER_SUBJECT
        ):
            raise ConversationCapacity()
        now = datetime.now(UTC)
        key = secrets.token_urlsafe(32)
        entry = ConversationEntry(key, subject, now, now, now + timedelta(seconds=self._ttl))
        runtime = _Runtime(monotonic() + self._ttl)
        self._entries[key], self._runtime[key] = entry, runtime
        runtime.expiry = asyncio.create_task(self._expire(key, runtime))
        logging.getLogger("sng_intelligence").info("conversation_created")
        return entry

    def owned(self, key: str, subject: str) -> ConversationEntry:
        self.sweep()
        entry = self._entries.get(key)
        if entry is None:
            raise ConversationUnavailable()
        if entry.subject != subject:
            raise ConversationForbidden()
        return entry

    def sweep(self) -> None:
        for key, runtime in list(self._runtime.items()):
            if runtime.deadline <= monotonic():
                self._invalidate(key)

    async def _expire(self, key: str, runtime: _Runtime) -> None:
        await asyncio.sleep(max(0, runtime.deadline - monotonic()))
        if key in self._entries:
            logging.getLogger("sng_intelligence").info("conversation_expired")
            self._invalidate(key)

    @asynccontextmanager
    async def operation(
        self, key: str, subject: str, *, live: bool = False
    ) -> AsyncIterator[ConversationContext]:
        self.owned(key, subject)
        runtime = self._runtime[key]
        if len(runtime.operations) >= 8:
            raise ConversationBusy()
        task = asyncio.current_task()
        assert task is not None
        runtime.operations.add(task)
        try:
            async with runtime.lock:
                entry = self.owned(key, subject)
                if not live and key in self._live.values():
                    raise ConversationBusy()
                yield ConversationContext(entry.turns)
                self.owned(key, subject)
        finally:
            runtime.operations.discard(task)

    def append(self, key: str, subject: str, turn: ConversationTurn) -> None:
        entry = self.owned(key, subject)
        runtime = self._runtime[key]
        if not runtime.lock.locked() or asyncio.current_task() not in runtime.operations:
            raise ConversationBusy()
        for text in (turn.user_content, turn.assistant_content, turn.references):
            check_context_text(text)
        turns = (*entry.turns, turn)[-MAX_TURNS:]
        while turns and ConversationContext(turns).characters > MAX_CHARACTERS:
            turns = turns[1:]  # Drop whole turns, including an oversized single turn.
        entry.turns = turns
        entry.last_activity_at = datetime.now(UTC)
        logging.getLogger("sng_intelligence").info(
            "conversation_turn_added modality=%s count=%d", turn.modality, len(turns)
        )

    def bind_live(self, session_id: str, key: str, subject: str) -> None:
        self.owned(key, subject)
        if key in self._live.values():
            raise ConversationBusy()
        self._live[session_id] = key

    def conversation_for_live(self, session_id: str) -> str | None:
        return self._live.get(session_id)

    def detach_live(self, session_id: str) -> None:
        self._live.pop(session_id, None)

    def _invalidate(self, key: str) -> asyncio.Task[None]:
        entry = self._entries.pop(key)
        runtime = self._runtime.pop(key)
        entry.turns = ()
        self._closed[key] = entry.subject
        if len(self._closed) > MAX_CONVERSATIONS:
            del self._closed[next(iter(self._closed))]
        for task in (*runtime.operations, runtime.expiry):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        sessions = [session for session, conversation in self._live.items() if conversation == key]
        # Keep bindings until sideband cleanup, so no operation can fall back to stateless.
        task = asyncio.create_task(self._finish_close(sessions, runtime))
        self._cleanup.add(task)
        task.add_done_callback(self._cleanup.discard)
        return task

    async def _finish_close(self, sessions: list[str], runtime: _Runtime) -> None:
        if self.close_live is not None:
            await asyncio.gather(*(self.close_live(session) for session in sessions))
        for session in sessions:
            self.detach_live(session)
        if runtime.operations:
            await asyncio.gather(*runtime.operations, return_exceptions=True)
        logging.getLogger("sng_intelligence").info("conversation_closed")

    async def close(self, key: str, subject: str) -> None:
        self.sweep()
        if key not in self._entries:
            if key not in self._closed:
                raise ConversationUnavailable()
            if self._closed[key] != subject:
                raise ConversationForbidden()
            return
        self.owned(key, subject)
        await asyncio.shield(self._invalidate(key))

    async def shutdown(self) -> None:
        self._stopped = True
        for key in list(self._entries):
            self._invalidate(key)
        if self._cleanup:
            await asyncio.gather(*self._cleanup)
        self._closed.clear()
