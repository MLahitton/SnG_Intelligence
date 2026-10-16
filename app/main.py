import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.agent.contracts import ChatRequest, ChatResponse
from app.agent.core import Agent
from app.api.dependencies import get_agent, get_control_client
from app.api.voice import router as voice_router
from app.auth.management_guard import require_management
from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.core.config import Settings
from app.core.errors import IntelligenceError
from app.core.observability import CorrelationMiddleware


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config = settings if settings is not None else Settings.from_env()
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logging.getLogger("sng_intelligence").setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        # Multipart debug messages include parser events; do not log upload contents.
        logging.getLogger("python_multipart").setLevel(logging.CRITICAL)
        async with create_http_client(
            config.sng_control_base_url, config.sng_control_timeout_seconds, transport
        ) as http:
            application.state.control_http = http
            try:
                yield
            finally:
                del application.state.control_http

    application = FastAPI(
        title="SnG Intelligence",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        swagger_ui_oauth2_redirect_url=None,
        debug=False,
    )
    application.add_middleware(CorrelationMiddleware)
    application.include_router(voice_router)

    @application.exception_handler(IntelligenceError)
    async def safe_error(request: Request, exc: IntelligenceError) -> JSONResponse:
        request.state.error_type = exc.code
        return JSONResponse(
            {"error": exc.code, "requestId": str(request.state.request_id)},
            status_code=exc.status_code,
            headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
        )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.exception_handler(RequestValidationError)
    async def safe_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI defaults echo invalid inputs; request bodies can contain secrets.
        request.state.error_type = "invalid_request"
        return JSONResponse(
            {"error": "invalid_request", "requestId": str(request.state.request_id)},
            status_code=422,
        )

    @application.post("/api/intelligence/chat", response_model=ChatResponse)
    async def chat(
        body: ChatRequest,
        agent: Annotated[Agent, Depends(get_agent)],
    ) -> ChatResponse:
        return await agent.run(body.message)

    @application.get("/api/intelligence/session")
    async def session(
        client: Annotated[SnGControlClient, Depends(get_control_client)],
    ) -> dict[str, bool]:
        await require_management(client)
        return {"authenticated": True, "isManagement": True}

    return application


app = create_app()
