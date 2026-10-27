import json
from uuid import uuid4

import httpx
import pytest
from conftest import PROJECT_ID, TOKEN
from conversation_repository import MemoryRepository
from pydantic import SecretStr
from test_agent import FakeModel, call, final
from test_conversation import turn
from test_live import KEY, LIVE_SETTINGS, SDP, SESSION
from test_live_provider import created
from test_memory import REFERENCE, REFS, FakeCompactor, draft, seed
from test_quotations import quotation_path, quotations  # noqa: F401 - shared fixture
from test_sideband import begin, complete, function, setup_live, until

from app.agent.core import Agent
from app.agent.errors import InvalidModelResponse, InvalidToolCall
from app.agent.security import CredentialGuard
from app.agent.toolbox import ToolBox
from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.conversation.context import ConversationContext
from app.conversation.memory import assemble_memory
from app.conversation.store import ConversationStore
from app.live.provider import OpenAILiveProvider
from app.tools import ReadTools

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "requested,error_type",
    [
        (final("Sigue en tiempo."), InvalidModelResponse),
        (call("get_commercial_quotation", {"project_id": PROJECT_ID}), InvalidToolCall),
        (call("get_budget_quotation", {"project_id": PROJECT_ID}), InvalidToolCall),
    ],
)
async def test_memory_only_cannot_supply_current_facts_or_authorize_quotation(
    mock_backend, requested, error_type
):
    memory = assemble_memory(
        draft(summary="Casa Ximena se discutió anteriormente como en tiempo."), (REFERENCE,)
    )
    guard = CredentialGuard(SecretStr(TOKEN))
    model = FakeModel(requested)
    async with create_http_client("https://control.test", 7, mock_backend[0]) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        try:
            with pytest.raises(error_type):
                await Agent(model, ToolBox(ReadTools(client), guard), guard, {}).run(
                    "¿Sigue en tiempo?", ConversationContext(memory=memory)
                )
        finally:
            client.clear_credentials()
    assert not mock_backend[1]
    assert memory.summary in repr(model.inputs[0][0])


@pytest.mark.usefixtures("quotations")
async def test_live_long_history_restart_memory_and_fresh_quotation(mock_backend):
    repo = MemoryRepository()
    key = await seed(repo, 35, first=turn("Proyecto base", "Usemos Casa Ximena", REFS))
    original = repo.turns[key]
    first = ConversationStore(repo, FakeCompactor())
    async with first.operation(key, "manager"):
        pass
    await first.shutdown()
    store = ConversationStore(repo, FakeCompactor())
    assert (await store.resume("manager")).conversation_id == key
    async with store.operation(key, "manager") as context:
        assert context.memory.historical_referents == (REFERENCE,)
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        return httpx.Response(201, json=created())

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        await OpenAILiveProvider(http, SecretStr(KEY), LIVE_SETTINGS).create(SDP, context)
    assert requests[0]["session"]["input"] == context.live_input()
    assert "Casa Ximena" in str(requests[0]["session"]["input"])
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "two"),
        final("La cotización confirmada registra vidrio Templado.", [f"project:{PROJECT_ID}"]),
    )
    transport, backend_calls = mock_backend
    async with setup_live(transport, model) as (manager, socket, vault, registry, _):
        manager._conversations = store
        await store.bind_live(SESSION, key, "manager")
        begin(socket)
        socket.incoming.put_nowait(
            function(question="Volvamos al proyecto base. ¿Qué vidrio usa actualmente?")
        )
        complete(socket)
        await until(lambda: any(e["type"] == "response.create" for e in socket.sent))
        assert not socket.closed and registry.owned(SESSION, "manager")
        assert vault._get(SESSION).subject == "manager"
        assert "Long-term conversation memory" in repr(model.inputs[0][0])
        assert "Casa Ximena" in repr(model.inputs[0][0])
        assert [r["path"] for r in backend_calls] == [
            "/api/auth/me",
            f"/api/projects/{PROJECT_ID}",
            f"/api/projects/{PROJECT_ID}/tasks",
            quotation_path("commercial"),
        ]
        assert repo.turns[key][:35] == original and len(repo.turns[key]) == 36
        assert json.loads(repo.turns[key][-1].references)[0]["projectId"] == PROJECT_ID
        assert "Templado" in repo.turns[key][-1].assistant_content
    await store.shutdown()


async def test_live_memory_only_sent_as_same_data():
    context = ConversationContext(memory=assemble_memory(draft(), (REFERENCE,)))

    def respond(request):
        assert json.loads(request.content)["session"]["input"] == context.live_input()
        return httpx.Response(201, json=created())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await OpenAILiveProvider(http, SecretStr(KEY), LIVE_SETTINGS).create(SDP, context)
