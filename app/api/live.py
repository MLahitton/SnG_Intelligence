import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from time import monotonic
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.routing import APIRoute
from starlette.types import Message

from app.agent.errors import ProviderTimeout
from app.agent.security import CredentialGuard
from app.api.dependencies import get_credential_guard, get_management_identity
from app.auth.management_guard import extract_bearer
from app.contracts.auth import CurrentUser
from app.live.config import MAX_BODY_BYTES, LiveSettings
from app.live.contracts import (
    LiveCloseRequest,
    LiveCloseResponse,
    LiveSessionProvider,
    LiveSessionRequest,
    LiveSessionResponse,
)
from app.live.credentials import LiveCredentialContext
from app.live.errors import LiveBodyTooLarge, LiveMediaTypeError
from app.live.provider import configured_live_provider
from app.live.registry import LiveRegistry
from app.live.sideband import LiveSidebandManager


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
        credentials = cast(LiveCredentialContext, request.app.state.live_credentials)
        manager = cast(LiveSidebandManager, request.app.state.live_sidebands)
        try:
            credentials.capture(
                result.session_id,
                extract_bearer(request.headers.getlist("authorization")),
                identity.subject,
                registry.owned(result.session_id, identity.subject).expires_at,
            )
            await manager.attach(
                result.session_id, identity.subject, provider, str(request.state.request_id)
            )
        except BaseException:
            credentials.revoke(result.session_id)
            registry.discard(result.session_id)
            raise
    request.state.live_event = "created"
    return result


@router.post("/close", response_model=LiveCloseResponse)
async def live_close(
    body: LiveCloseRequest,
    request: Request,
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
    registry: Annotated[LiveRegistry, Depends(get_live_registry)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> LiveCloseResponse:
    guard.check(body.model_dump_json())
    manager = cast(LiveSidebandManager, request.app.state.live_sidebands)
    await manager.close(body.session_id, identity.subject)
    request.state.live_event = "closed"
    return LiveCloseResponse()
