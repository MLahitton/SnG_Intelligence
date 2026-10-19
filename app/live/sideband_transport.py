import asyncio
import json
import logging
import ssl
from typing import Protocol

import truststore
from pydantic import SecretStr, TypeAdapter
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

from app.agent.errors import ProviderConfigurationError, ProviderRateLimited, ProviderTimeout
from app.agent.security import CredentialGuard
from app.core.errors import IntelligenceError
from app.live.contracts import OpaqueId
from app.live.errors import LiveAttachError, LiveDisconnected
from app.live.events import MAX_EVENT_BYTES


class SidebandTransport(Protocol):
    async def receive(self) -> str | bytes: ...
    async def send(self, event: dict[str, object]) -> None: ...
    async def close(self) -> None: ...


class SidebandConnector(Protocol):
    async def attach(self, session_id: str) -> SidebandTransport: ...


class _NoRedirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        return exc  # Never forward project authentication to another host.


class _Socket:
    def __init__(self, socket: ClientConnection, key: SecretStr) -> None:
        self._socket = socket
        self._guard = CredentialGuard(key)

    async def receive(self) -> str | bytes:
        try:
            return await self._socket.recv()
        except (WebSocketException, OSError):
            pass
        raise LiveDisconnected()

    async def send(self, event: dict[str, object]) -> None:
        text = json.dumps(event, ensure_ascii=False)
        self._guard.check(text)
        try:
            async with asyncio.timeout(5):
                await self._socket.send(text)
                return
        except (WebSocketException, OSError, TimeoutError):
            pass
        raise LiveDisconnected()

    async def close(self) -> None:
        try:
            async with asyncio.timeout(3):
                await self._socket.close()
        except (WebSocketException, OSError, TimeoutError):
            self._socket.transport.abort()


class OpenAISidebandConnector:
    def __init__(self, key: SecretStr, timeout: int = 30) -> None:
        self._key = key
        self._timeout = timeout

    async def attach(self, session_id: str) -> SidebandTransport:
        identifier = TypeAdapter(OpaqueId).validate_python(session_id)
        # Dedicated disabled logger: websocket debug logging includes headers and frames.
        logger = logging.Logger("sng_live_transport")
        logger.disabled = True
        error: IntelligenceError = LiveAttachError()
        try:
            socket = await _NoRedirectConnect(
                f"wss://api.openai.com/v1/live/sessions/{identifier}/attach",
                additional_headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
                ssl=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
                proxy=None,
                open_timeout=self._timeout,
                close_timeout=2,
                max_size=MAX_EVENT_BYTES,
                max_queue=8,
                compression=None,
                logger=logger,
            )
            if socket.request is not None:
                del socket.request.headers["Authorization"]
            return _Socket(socket, self._key)
        except InvalidStatus as exc:
            if exc.response.status_code in {400, 401, 403, 404, 422}:
                error = ProviderConfigurationError()
            elif exc.response.status_code == 429:
                error = ProviderRateLimited()
        except TimeoutError:
            error = ProviderTimeout()
        except (WebSocketException, OSError):
            pass
        raise error  # No raw exception context, headers or provider response.
