import asyncio
import json
import os
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import truststore
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from app.agent.errors import ProviderConfigurationError, ProviderRateLimited, ProviderTimeout
from app.agent.security import CredentialGuard
from app.conversation.context import CONTEXT_RULE, ConversationContext
from app.core.errors import IntelligenceError
from app.live.config import MAX_PROVIDER_BYTES, LiveSettings
from app.live.contracts import LiveSessionResponse, OpaqueId
from app.live.errors import LiveNotConfigured, LiveProviderError
from app.live.prompt import DELEGATION_INSTRUCTIONS, LIVE_INSTRUCTIONS, consultation_tool
from app.live.sideband_transport import OpenAISidebandConnector, SidebandTransport

CREATE_URL = "https://api.openai.com/v1/live/sessions"


def session_config(settings: LiveSettings) -> dict[str, object]:
    return {
        "model": settings.model,
        "instructions": LIVE_INSTRUCTIONS + "\n" + CONTEXT_RULE,
        "audio": {"output": {"voice": settings.voice}},
        "store": False,
        # Frontend has lifecycle/UI only; private sideband owns tools and continuation.
        "client": {
            "data_channel": {
                "allowed_client_events": [
                    "session.close",
                ],
                "allowed_server_events": [
                    {"type": kind}
                    for kind in (
                        "session.started",
                        "session.closed",
                        "error",
                        "session.input_transcript.delta",
                        "session.output_transcript.delta",
                    )
                ]
                + [
                    {"type": "response.event", "response_event": kind}
                    for kind in (
                        "response.created",
                        "response.completed",
                        "response.failed",
                        "response.incomplete",
                    )
                ],
            }
        },
        "delegation": {
            "type": "responses",
            "responses": {
                "model": settings.delegation_model,
                "instructions": DELEGATION_INSTRUCTIONS + "\n" + CONTEXT_RULE,
                "tools": [consultation_tool()],
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "max_output_tokens": settings.delegation_max_output_tokens,
                "service_tier": settings.service_tier,
                **(
                    {"reasoning": {"effort": settings.delegation_effort}}
                    if settings.delegation_effort is not None
                    else {}
                ),
            },
        },
    }


class _Session(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    id: OpaqueId


class _Transport(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    type: str
    sdp: str


class _Created(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    session: _Session
    transport: _Transport


class OpenAILiveProvider:
    """Closed REST adapter until the installed SDK exposes the Live API.

    Accepts SDP only; no credentials, URLs, tools or session overrides from callers.
    """

    def __init__(self, http: httpx.AsyncClient, key: SecretStr, settings: LiveSettings) -> None:
        self._http = http
        self._key = key
        self._settings = settings

    async def attach(self, session_id: str) -> SidebandTransport:
        return await OpenAISidebandConnector(self._key, self._settings.timeout_seconds).attach(
            session_id
        )

    async def create(
        self, sdp: str, context: ConversationContext | None = None
    ) -> LiveSessionResponse:
        session = session_config(self._settings)
        if context is not None and context.turns:
            session["input"] = context.live_input()
        payload = {
            "session": session,
            "transport": {"type": "webrtc", "sdp": sdp},
        }
        guard = CredentialGuard(self._key)
        guard.check(json.dumps(payload, ensure_ascii=False))
        error: IntelligenceError = LiveProviderError()
        try:
            async with (
                asyncio.timeout(self._settings.timeout_seconds),
                self._http.stream(
                    "POST",
                    CREATE_URL,
                    headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
                    json=payload,
                    follow_redirects=False,
                    timeout=self._settings.timeout_seconds,
                ) as response,
            ):
                # Strip credentials from request objects before parsing errors or JSON.
                response.request.headers.pop("Authorization", None)
                if response.status_code in {400, 401, 403, 404, 422}:
                    raise ProviderConfigurationError()
                if response.status_code == 429:
                    raise ProviderRateLimited()
                if response.status_code != 201:
                    raise LiveProviderError()
                if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                    raise LiveProviderError()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_PROVIDER_BYTES:
                        raise LiveProviderError()
                    content.extend(chunk)
                parsed = _Created.model_validate_json(bytes(content))
                if parsed.transport.type != "webrtc":
                    raise LiveProviderError()
                result = LiveSessionResponse(sessionId=parsed.session.id, sdp=parsed.transport.sdp)
                guard.check(result.model_dump_json())
                return result
        except httpx.TimeoutException as exc:
            exc.request.headers.pop("Authorization", None)
            error = ProviderTimeout()
        except TimeoutError:
            error = ProviderTimeout()
        except httpx.RequestError as exc:
            exc.request.headers.pop("Authorization", None)
        except (ValueError, ValidationError):
            pass
        raise error


@asynccontextmanager
async def configured_live_provider(settings: LiveSettings) -> AsyncIterator[OpenAILiveProvider]:
    key = SecretStr(os.environ.get("OPENAI_API_KEY", ""))
    if not key.get_secret_value().strip():
        raise LiveNotConfigured()
    tls = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    async with httpx.AsyncClient(
        verify=tls, trust_env=False, follow_redirects=False, timeout=settings.timeout_seconds
    ) as http:
        yield OpenAILiveProvider(http, key, settings)
