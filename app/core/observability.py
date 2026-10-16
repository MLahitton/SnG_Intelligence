import json
import logging
from time import perf_counter
from uuid import UUID, uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("sng_intelligence")


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).getlist("x-request-id")
        request_id = uuid4()
        if len(supplied) == 1:
            try:
                request_id = UUID(supplied[0])
            except ValueError:
                pass
        scope.setdefault("state", {})["request_id"] = request_id
        start = perf_counter()
        status = 500

        async def correlated_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = str(request_id)
                headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, correlated_send)
        finally:
            route = scope.get("route")
            logger.info(
                json.dumps(
                    {
                        "request_id": str(request_id),
                        "operation": getattr(route, "name", "unmatched"),
                        "duration_ms": round((perf_counter() - start) * 1000, 2),
                        "status": status,
                        "error_type": scope["state"].get("error_type"),
                        **{
                            key: scope["state"][key]
                            for key in (
                                "audio_size_bytes",
                                "provider_stage",
                                "transcription_duration_ms",
                                "agent_duration_ms",
                                "speech_duration_ms",
                                "live_event",
                                "live_duration_seconds",
                                "live_delegations_count",
                            )
                            if key in scope["state"]
                        },
                        **scope["state"].get("agent_metrics", {}),
                    }
                )
            )
