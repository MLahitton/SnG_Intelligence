"""Private, bounded credential lease. SecretStr masks repr; it is NOT encryption."""

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Never

from pydantic import SecretStr

from app.agent.security import CredentialGuard
from app.live.errors import LiveSessionUnavailable


@dataclass(slots=True, repr=False)
class _Credential:
    token: SecretStr
    subject: str
    expires_at: float

    def __reduce__(self) -> Never:
        raise TypeError("Live credentials cannot be serialized")


class LiveCredentialContext:
    __slots__ = ("__items", "_clock")

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self.__items: dict[str, _Credential] = {}
        self._clock = clock

    def capture(self, session_id: str, token: SecretStr, subject: str, expires_at: float) -> None:
        if session_id in self.__items or expires_at <= self._clock():
            raise LiveSessionUnavailable()
        self.__items[session_id] = _Credential(token, subject, min(expires_at, self._clock() + 600))

    def _get(self, session_id: str) -> _Credential:
        value = self.__items.get(session_id)
        if value is None or self._clock() >= value.expires_at:
            self.revoke(session_id)
            raise LiveSessionUnavailable()
        return value

    def check(self, session_id: str, text: str) -> None:
        CredentialGuard(self._get(session_id).token).check(text)

    def revoke(self, session_id: str) -> None:
        self.__items.pop(session_id, None)

    def clear(self) -> None:
        self.__items.clear()

    def __reduce__(self) -> Never:
        raise TypeError("Live credentials cannot be serialized")
