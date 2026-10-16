from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request

from app.agent.contracts import AgentModel
from app.agent.openai_model import configured_model
from app.auth.management_guard import extract_bearer, require_management
from app.clients.sng_control import SnGControlClient
from app.tools import ReadTools


async def get_control_client(request: Request) -> AsyncIterator[SnGControlClient]:
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


async def get_management_client(
    client: Annotated[SnGControlClient, Depends(get_control_client)],
) -> SnGControlClient:
    await require_management(client)
    return client


async def get_agent_model(
    client: Annotated[SnGControlClient, Depends(get_management_client)],
) -> AsyncIterator[AgentModel]:
    async with configured_model() as model:
        yield model
