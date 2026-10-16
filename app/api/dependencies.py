import os
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from app.agent.contracts import AgentModel
from app.agent.core import Agent
from app.agent.openai_model import configured_model
from app.agent.security import CredentialGuard
from app.agent.toolbox import ToolBox
from app.auth.management_guard import extract_bearer, require_management
from app.clients.sng_control import SnGControlClient
from app.contracts.auth import CurrentUser
from app.tools import ReadTools


async def get_control_client(
    request: Request,
    bearer_docs: Annotated[
        HTTPAuthorizationCredentials | None, Security(HTTPBearer(auto_error=False))
    ],
) -> AsyncIterator[SnGControlClient]:
    # HTTPBearer documents the scheme; the existing strict guard remains authoritative.
    token = extract_bearer(request.headers.getlist("authorization"))
    client = SnGControlClient(request.app.state.control_http, token, request.state.request_id)
    del token
    try:
        yield client
    finally:
        client.clear_credentials()


def get_read_tools(
    client: Annotated[SnGControlClient, Depends(get_control_client)],
) -> ReadTools:
    return ReadTools(client)


async def get_management_identity(
    client: Annotated[SnGControlClient, Depends(get_control_client)],
) -> CurrentUser:
    return await require_management(client)


def get_management_client(
    client: Annotated[SnGControlClient, Depends(get_control_client)],
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
) -> SnGControlClient:
    return client


async def get_agent_model(
    client: Annotated[SnGControlClient, Depends(get_management_client)],
) -> AsyncIterator[AgentModel]:
    async with configured_model() as model:
        yield model


def get_credential_guard(request: Request) -> CredentialGuard:
    return CredentialGuard(
        extract_bearer(request.headers.getlist("authorization")),
        SecretStr(os.environ.get("OPENAI_API_KEY", "")),
    )


def get_agent(
    request: Request,
    client: Annotated[SnGControlClient, Depends(get_management_client)],
    model: Annotated[AgentModel, Depends(get_agent_model)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> Agent:
    metrics: dict[str, object] = {}
    request.state.agent_metrics = metrics
    return Agent(model, ToolBox(ReadTools(client), guard), guard, metrics)
