import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.conversation.compaction import Compactor, MemoryCompaction
from app.conversation.context import (
    ConversationContext,
    ConversationTurn,
    check_context_text,
)
from app.conversation.openai_memory import compact_memory
from app.conversation.repository import (
    ConversationBusy as ConversationBusy,
)
from app.conversation.repository import (
    ConversationCapacity as ConversationCapacity,
)
from app.conversation.repository import (
    ConversationEntry as ConversationEntry,
)
from app.conversation.repository import (
    ConversationForbidden as ConversationForbidden,
)
from app.conversation.repository import (
    ConversationRepository,
)
from app.conversation.repository import (
    ConversationUnavailable as ConversationUnavailable,
)

MAX_RUNTIME_CONVERSATIONS = 128


@dataclass(slots=True, repr=False)
class _Runtime:
    subject: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    operations: set[asyncio.Task[object]] = field(default_factory=set)
    owner: asyncio.Task[object] | None = None
    version: int = 0
    closing: bool = False


class ConversationStore:
    """Durable history via repository; process-local locks and Live bindings only."""

    def __init__(
        self, repository: ConversationRepository, compactor: Compactor | None = None
    ) -> None:
        self.repository = repository
        self._memory = MemoryCompaction(repository, compactor or compact_memory)
        self._runtime: dict[str, _Runtime] = {}
        self._live: dict[str, str] = {}
        self.close_live: Callable[[str], Awaitable[None]] | None = None
        self._stopped = False

    def _running(self) -> None:
        if self._stopped:
            raise ConversationUnavailable()

    async def create(self, subject: str) -> ConversationEntry:
        self._running()
        entry = await self.repository.create(subject)
        for key, runtime in list(self._runtime.items()):
            if runtime.subject == subject and key != entry.conversation_id:
                await asyncio.shield(self._release(key))
        logging.getLogger("sng_intelligence").info("conversation_created")
        return entry

    async def resume(self, subject: str) -> ConversationEntry:
        self._running()
        entry = await self.repository.get_current(subject)
        logging.getLogger("sng_intelligence").info("conversation_resumed")
        return entry

    async def owned(self, key: str, subject: str) -> ConversationEntry:
        self._running()
        entry = await self.repository.get(key, subject)
        if entry.closed_at is not None:
            raise ConversationUnavailable()
        return entry

    def _acquire(self, key: str, subject: str) -> _Runtime:
        runtime = self._runtime.get(key)
        if runtime is None:
            if len(self._runtime) >= MAX_RUNTIME_CONVERSATIONS:
                raise ConversationCapacity()
            runtime = _Runtime(subject)
            self._runtime[key] = runtime
        if runtime.closing:
            raise ConversationUnavailable()
        if runtime.subject != subject:
            raise ConversationForbidden()
        return runtime

    def _drop_idle(self, key: str, runtime: _Runtime) -> None:
        if not runtime.operations and key not in self._live.values():
            if self._runtime.get(key) is runtime:
                self._runtime.pop(key, None)

    @asynccontextmanager
    async def operation(
        self, key: str, subject: str, *, live: bool = False
    ) -> AsyncIterator[ConversationContext]:
        await self.owned(key, subject)
        runtime = self._acquire(key, subject)
        if len(runtime.operations) >= 8:
            raise ConversationBusy()
        task = asyncio.current_task()
        assert task is not None
        runtime.operations.add(task)
        try:
            async with runtime.lock:
                entry = await self.owned(key, subject)
                if runtime.closing:
                    raise ConversationUnavailable()
                if not live and key in self._live.values():
                    raise ConversationBusy()
                runtime.owner = task
                runtime.version = entry.version
                context = await self._memory.context(key, subject, entry.version)
                yield context
                try:
                    await self.owned(key, subject)
                except ConversationUnavailable:
                    # Work may already have committed. Never invite the frontend's stale-ID retry.
                    raise ConversationBusy() from None
        finally:
            if runtime.owner is task:
                runtime.owner = None
            runtime.operations.discard(task)
            self._drop_idle(key, runtime)

    async def append(self, key: str, subject: str, turn: ConversationTurn) -> None:
        await self.owned(key, subject)
        runtime = self._runtime.get(key)
        if runtime is None or runtime.closing or runtime.owner is not asyncio.current_task():
            raise ConversationBusy()
        for text in (turn.user_content, turn.assistant_content, turn.references):
            check_context_text(text)
        runtime.version = await self.repository.append_turn(key, subject, turn, runtime.version)
        logging.getLogger("sng_intelligence").info(
            "conversation_turn_added modality=%s count=%d", turn.modality, runtime.version
        )

    async def bind_live(self, session_id: str, key: str, subject: str) -> None:
        await self.owned(key, subject)
        self._acquire(key, subject)
        if key in self._live.values():
            raise ConversationBusy()
        self._live[session_id] = key

    def conversation_for_live(self, session_id: str) -> str | None:
        return self._live.get(session_id)

    def detach_live(self, session_id: str) -> None:
        key = self._live.pop(session_id, None)
        if key is not None and (runtime := self._runtime.get(key)) is not None:
            self._drop_idle(key, runtime)

    async def _release(self, key: str) -> None:
        runtime = self._runtime.get(key)
        if runtime is None:
            return
        runtime.closing = True
        tasks = [t for t in runtime.operations if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        sessions = [s for s, conversation in self._live.items() if conversation == key]
        if self.close_live is not None:
            await asyncio.gather(
                *(self.close_live(session) for session in sessions), return_exceptions=True
            )
        for session in sessions:
            self.detach_live(session)
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._runtime.get(key) is runtime:
            self._runtime.pop(key, None)

    async def close(self, key: str, subject: str) -> None:
        self._running()
        await self.repository.close(key, subject)
        await asyncio.shield(self._release(key))
        logging.getLogger("sng_intelligence").info("conversation_closed")

    async def shutdown(self) -> None:
        self._stopped = True
        for key in list(self._runtime):
            await self._release(key)
        # Durable records are untouched. The lifespan owns pool shutdown.
