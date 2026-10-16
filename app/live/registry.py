import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic

from app.live.config import MAX_DELEGATIONS, MAX_SESSIONS, MAX_SESSIONS_PER_SUBJECT
from app.live.errors import (
    LiveCallConflict,
    LiveCapacityExceeded,
    LiveProviderError,
    LiveSessionForbidden,
    LiveSessionUnavailable,
)


@dataclass(slots=True, repr=False)
class LiveEntry:
    subject: str
    created_at: float
    expires_at: float
    # Bounded opaque call IDs only: no questions, answers, auth or transcripts.
    calls: set[str] = field(default_factory=set)
    busy: bool = False


class LiveRegistry:
    """Instance-local metadata only. Mutations are synchronous on one ASGI event loop."""

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, LiveEntry] = {}
        self._pending: dict[str, int] = {}

    def cleanup(self) -> None:
        now = self._clock()
        for identifier, entry in list(self._entries.items()):
            if now >= entry.expires_at:
                del self._entries[identifier]

    async def maintain(self) -> None:
        while True:
            await asyncio.sleep(15)
            self.cleanup()

    def clear(self) -> None:
        self._entries.clear()
        self._pending.clear()

    @contextmanager
    def reserve(self, subject: str) -> Iterator[None]:
        self.cleanup()
        pending = sum(self._pending.values())
        owned = sum(entry.subject == subject for entry in self._entries.values())
        if (
            len(self._entries) + pending >= MAX_SESSIONS
            or owned + self._pending.get(subject, 0) >= MAX_SESSIONS_PER_SUBJECT
        ):
            raise LiveCapacityExceeded()
        self._pending[subject] = self._pending.get(subject, 0) + 1
        try:
            yield
        finally:
            remaining = self._pending.get(subject, 1) - 1
            if remaining:
                self._pending[subject] = remaining
            else:
                self._pending.pop(subject, None)

    def add(self, identifier: str, subject: str, lifetime_seconds: int) -> None:
        self.cleanup()
        if identifier in self._entries:
            raise LiveProviderError()
        now = self._clock()
        self._entries[identifier] = LiveEntry(subject, now, now + lifetime_seconds)

    def owned(self, identifier: str, subject: str) -> LiveEntry:
        self.cleanup()
        entry = self._entries.get(identifier)
        if entry is None:
            raise LiveSessionUnavailable()
        if entry.subject != subject:
            raise LiveSessionForbidden()
        return entry

    def close(self, identifier: str, subject: str) -> LiveEntry:
        entry = self.owned(identifier, subject)
        del self._entries[identifier]
        return entry

    def duration(self, entry: LiveEntry) -> float:
        return round(max(0, self._clock() - entry.created_at), 2)

    @contextmanager
    def delegation(self, identifier: str, subject: str, call_id: str) -> Iterator[LiveEntry]:
        entry = self.owned(identifier, subject)
        if entry.busy or call_id in entry.calls:
            raise LiveCallConflict()
        if len(entry.calls) >= MAX_DELEGATIONS:
            raise LiveCapacityExceeded()
        entry.calls.add(call_id)
        entry.busy = True
        try:
            yield entry
        finally:
            entry.busy = False
