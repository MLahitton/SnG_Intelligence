import asyncio
import secrets
from dataclasses import replace
from datetime import UTC, datetime

from app.conversation.repository import (
    ConversationBusy,
    ConversationEntry,
    ConversationForbidden,
    ConversationUnavailable,
    StoredConversationMemory,
    StoredConversationTurn,
)


class MemoryRepository:
    """Offline durable-state stand-in shared by successive application runtimes."""

    def __init__(self):
        self.entries = {}
        self.turns = {}
        self.memories = {}
        self.lock = asyncio.Lock()

    async def ready(self):
        pass

    async def get(self, key, subject):
        entry = self.entries.get(key)
        if entry is None:
            raise ConversationUnavailable()
        if entry.subject != subject:
            raise ConversationForbidden()
        return entry

    async def _current(self, subject, replace_current):
        async with self.lock:
            current = next(
                (e for e in self.entries.values() if e.subject == subject and e.closed_at is None),
                None,
            )
            if current and not replace_current:
                return current
            if current:
                self.entries[current.conversation_id] = replace(
                    current, closed_at=datetime.now(UTC)
                )
            now = datetime.now(UTC)
            entry = ConversationEntry(secrets.token_urlsafe(32), subject, now, now)
            self.entries[entry.conversation_id] = entry
            self.turns[entry.conversation_id] = ()
            return entry

    async def create(self, subject):
        return await self._current(subject, True)

    async def get_current(self, subject):
        return await self._current(subject, False)

    async def append_turn(self, key, subject, turn, expected_version):
        async with self.lock:
            entry = await self.get(key, subject)
            if entry.closed_at is not None:
                raise ConversationUnavailable()
            if entry.version != expected_version:
                raise ConversationBusy()
            self.entries[key] = replace(
                entry, version=entry.version + 1, last_activity_at=datetime.now(UTC)
            )
            self.turns[key] += (turn,)
            return entry.version + 1

    async def close(self, key, subject):
        async with self.lock:
            entry = await self.get(key, subject)
            self.entries[key] = replace(entry, closed_at=entry.closed_at or datetime.now(UTC))

    async def list_turns(self, key, subject, *, limit=None):
        await self.get(key, subject)
        turns = self.turns[key]
        return turns if limit is None else turns[-limit:]

    async def get_memory(self, key, subject):
        await self.get(key, subject)
        return self.memories.get(key)

    async def save_memory(self, key, subject, memory, compacted_through, expected_revision):
        async with self.lock:
            entry = await self.get(key, subject)
            if entry.closed_at is not None:
                raise ConversationUnavailable()
            previous = self.memories.get(key)
            if (previous.revision if previous else 0) != expected_revision or not (
                previous.compacted_through if previous else 0
            ) < compacted_through <= entry.version:
                raise ConversationBusy()
            stored = StoredConversationMemory(compacted_through, expected_revision + 1, memory)
            self.memories[key] = stored
            return stored

    async def list_turn_records(self, key, subject, *, after, through, limit, latest=False):
        await self.get(key, subject)
        records = tuple(
            StoredConversationTurn(i, turn)
            for i, turn in enumerate(self.turns[key], 1)
            if after < i <= through
        )
        return records[-limit:] if latest else records[:limit]
