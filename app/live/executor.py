import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import uuid4

import httpx
from pydantic import SecretStr

from app.agent.contracts import AgentModel, ChatResponse
from app.agent.core import Agent
from app.agent.security import CredentialGuard
from app.agent.toolbox import ToolBox
from app.clients.sng_control import SnGControlClient
from app.live.credentials import LiveCredentialContext
from app.live.model import configured_live_agent_model
from app.tools import ReadTools


class LiveAgentExecutor:
    """Only this boundary obtains Google credentials; never passed to a provider."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        credentials: LiveCredentialContext,
        model_factory: Callable[
            [], AbstractAsyncContextManager[AgentModel]
        ] = configured_live_agent_model,
    ) -> None:
        self._http = http
        self._credentials = credentials
        self._model_factory = model_factory

    async def run(self, session_id: str, question: str) -> ChatResponse:
        lease = self._credentials._get(session_id)
        client = SnGControlClient(self._http, lease.token, uuid4())
        guard = CredentialGuard(lease.token, SecretStr(os.environ.get("OPENAI_API_KEY", "")))
        try:
            guard.check(question)
            async with ReadTools._authorized_run(client, lease.subject) as tools:
                self._credentials._get(session_id)
                async with self._model_factory() as model:
                    self._credentials._get(session_id)
                    result = await Agent(model, ToolBox(tools, guard), guard, {}).run(question)
                    self._credentials._get(session_id)
                    guard.check(result.model_dump_json())
                    return result
        finally:
            client.clear_credentials()
