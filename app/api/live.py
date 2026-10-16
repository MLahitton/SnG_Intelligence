import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from time import monotonic, perf_counter
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.routing import APIRoute
from starlette.types import Message

from app.agent.contracts import ChatResponse
from app.agent.core import Agent
from app.agent.errors import ProviderTimeout
from app.agent.security import CredentialGuard
from app.api.dependencies import get_agent, get_credential_guard, get_management_identity
from app.contracts.auth import CurrentUser
from app.live.config import MAX_BODY_BYTES, LiveSettings
from app.live.contracts import (
    LiveCloseRequest,
    LiveCloseResponse,
    LiveDelegateRequest,
    LiveSessionProvider,
    LiveSessionRequest,
    LiveSessionResponse,
)
from app.live.errors import LiveBodyTooLarge, LiveMediaTypeError
from app.live.provider import configured_live_provider
from app.live.registry import LiveEntry, LiveRegistry


class LiveRoute(APIRoute):
    """Bound JSON at the receive boundary before FastAPI buffers/deserializes it."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                raise LiveMediaTypeError()
            content = bytearray()
            deadline = monotonic() + 10
            while True:
                try:
                    async with asyncio.timeout(max(0, deadline - monotonic())):
                        message = await request.receive()
                except TimeoutError:
                    raise ProviderTimeout() from None
                if message["type"] == "http.request":
                    chunk = message.get("body", b"")
                    if len(content) + len(chunk) > MAX_BODY_BYTES:
                        raise LiveBodyTooLarge()
                    content.extend(chunk)
                    if not message.get("more_body", False):
                        break
                else:
                    break

            delivered = False

            async def receive() -> Message:
                nonlocal delivered
                if delivered:
                    return await request.receive()
                delivered = True
                return {"type": "http.request", "body": bytes(content), "more_body": False}

            return await handler(Request(request.scope, receive=receive))

        return bounded


router = APIRouter(prefix="/api/intelligence/live", tags=["Live"], route_class=LiveRoute)


def get_live_registry(request: Request) -> LiveRegistry:
    return cast(LiveRegistry, request.app.state.live_registry)


def get_live_settings(
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
) -> LiveSettings:
    return LiveSettings.from_env()


async def get_live_provider(
    settings: Annotated[LiveSettings, Depends(get_live_settings)],
) -> AsyncIterator[LiveSessionProvider]:
    async with configured_live_provider(settings) as provider:
        yield provider


@router.post("/session", response_model=LiveSessionResponse, status_code=201)
async def live_session(
    body: LiveSessionRequest,
    request: Request,
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
    settings: Annotated[LiveSettings, Depends(get_live_settings)],
    registry: Annotated[LiveRegistry, Depends(get_live_registry)],
    provider: Annotated[LiveSessionProvider, Depends(get_live_provider)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> LiveSessionResponse:
    guard.check(body.sdp)
    guard.check(identity.subject)
    # Reserve before awaiting network I/O so concurrent creates cannot exceed the cap.
    with registry.reserve(identity.subject):
        result = await provider.create(body.sdp)
        guard.check(result.model_dump_json())
        registry.add(result.session_id, identity.subject, settings.lifetime_seconds)
    request.state.live_event = "created"
    return result


async def get_delegate_access(
    body: LiveDelegateRequest,
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
    registry: Annotated[LiveRegistry, Depends(get_live_registry)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> LiveEntry:
    guard.check(body.model_dump_json())
    return registry.owned(body.session_id, identity.subject)


def get_live_agent(
    access: Annotated[LiveEntry, Depends(get_delegate_access)],
    agent: Annotated[Agent, Depends(get_agent)],
) -> Agent:
    return agent


@router.post("/delegate", response_model=ChatResponse)
async def live_delegate(
    body: LiveDelegateRequest,
    request: Request,
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
    registry: Annotated[LiveRegistry, Depends(get_live_registry)],
    agent: Annotated[Agent, Depends(get_live_agent)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> ChatResponse:
    with registry.delegation(body.session_id, identity.subject, body.call_id) as entry:
        start = perf_counter()
        request.state.live_event = "delegated"
        request.state.live_delegations_count = len(entry.calls)
        try:
            result = await agent.run(body.question)
            # Closing, expiry or ownership changes while Agent runs invalidate delivery.
            registry.owned(body.session_id, identity.subject)
            guard.check(result.model_dump_json())
            return result
        finally:
            request.state.agent_duration_ms = round((perf_counter() - start) * 1000, 2)


@router.post("/close", response_model=LiveCloseResponse)
async def live_close(
    body: LiveCloseRequest,
    request: Request,
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
    registry: Annotated[LiveRegistry, Depends(get_live_registry)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> LiveCloseResponse:
    guard.check(body.model_dump_json())
    entry = registry.close(body.session_id, identity.subject)
    request.state.live_event = "closed"
    request.state.live_duration_seconds = registry.duration(entry)
    request.state.live_delegations_count = len(entry.calls)
    # Local revocation only. WebRTC owner sends session.close on its data channel.
    return LiveCloseResponse()
