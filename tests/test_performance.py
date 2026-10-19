import asyncio
import json
import pickle
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import httpx
import openai
import pytest
from conftest import PROJECT_ID, TOKEN
from pydantic import SecretStr
from test_agent import FakeModel, call, final
from test_live_provider import configure
from test_openai_model import response_body
from test_sideband import begin, complete, function, setup_live, until

from app.agent.contracts import ModelMessage
from app.agent.errors import ProviderConfigurationError
from app.agent.openai_model import OpenAIModel
from app.clients.sng_control import SnGControlClient
from app.core.errors import ManagementRequired
from app.core.latency import LatencyMetrics, latency_scope
from app.live.config import LiveSettings
from app.live.model import LiveAgentSettings, configured_live_agent_model
from app.live.provider import session_config
from app.tools import ReadTools


def test_live_configuration_wire_and_chat_defaults(monkeypatch):
    configure(monkeypatch)
    settings = LiveSettings.from_env()
    wire = session_config(settings)
    responses = wire["delegation"]["responses"]
    assert responses["reasoning"] == {"effort": "low"}
    assert responses["service_tier"] == "auto"
    assert responses["max_output_tokens"] == 1024
    # GPT-Live MediaSessionConfig has no Realtime turn_detection field.
    assert wire["audio"] == {"output": {"voice": "marin"}}
    assert "turn_detection" not in json.dumps(wire)
    monkeypatch.setenv("SNG_INTELLIGENCE_MODEL", "gpt-6-sol")
    assert LiveAgentSettings.from_env() == LiveAgentSettings("gpt-6-sol", "low", 800)
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_AGENT_MODEL", "gpt-6-luna")
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_AGENT_REASONING_EFFORT", "none")
    assert LiveAgentSettings.from_env() == LiveAgentSettings("gpt-6-luna", "none", 800)
    assert responses["model"] == settings.delegation_model
    assert (
        session_config(replace(settings, service_tier="priority"))["delegation"]["responses"][
            "service_tier"
        ]
        == "priority"
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("SERVICE_TIER", "flex"),
        ("DELEGATION_REASONING_EFFORT", "invalid"),
        ("DELEGATION_REASONING_EFFORT", "minimal"),
        ("DELEGATION_MAX_OUTPUT_TOKENS", "255"),
        ("DELEGATION_MAX_OUTPUT_TOKENS", "4097"),
        ("DELEGATION_MAX_OUTPUT_TOKENS", TOKEN),
    ],
)
def test_invalid_delegation_configuration(monkeypatch, name, value):
    configure(monkeypatch)
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_" + name, value)
    with pytest.raises(ProviderConfigurationError) as caught:
        LiveSettings.from_env()
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODEL", "unsupported-model"),
        ("REASONING_EFFORT", "invalid"),
        ("MAX_OUTPUT_TOKENS", "255"),
        ("MAX_OUTPUT_TOKENS", "3001"),
        ("MAX_OUTPUT_TOKENS", "nan"),
    ],
)
def test_invalid_agent_configuration(monkeypatch, name, value):
    monkeypatch.setenv("SNG_INTELLIGENCE_MODEL", "gpt-6-luna")
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_AGENT_" + name, value)
    with pytest.raises(ProviderConfigurationError):
        LiveAgentSettings.from_env()


def test_optional_reasoning_for_other_models(monkeypatch):
    monkeypatch.setenv("SNG_INTELLIGENCE_MODEL", "gpt-4.1")
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_AGENT_REASONING_EFFORT", "")
    assert LiveAgentSettings.from_env().effort is None


@pytest.mark.anyio
async def test_sdk_live_options_and_chat_unchanged(monkeypatch):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        sdk = openai.AsyncOpenAI(api_key="synthetic-key", http_client=http)

        @asynccontextmanager
        async def fake_client(*args, **kwargs):
            yield sdk

        monkeypatch.setattr("app.live.model.openai_client", fake_client)
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
        monkeypatch.setenv("SNG_INTELLIGENCE_MODEL", "gpt-6-sol")
        monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_AGENT_MODEL", "gpt-6-luna")
        async with configured_live_agent_model() as model:
            await model.respond((ModelMessage(role="user", content="Hola"),), ())
        await OpenAIModel(sdk, "normal-model").respond(
            (ModelMessage(role="user", content="Hola"),), ()
        )
    live, chat = requests
    assert live["model"] == "gpt-6-luna" and live["max_output_tokens"] == 800
    assert live["reasoning"] == {"effort": "low"}
    assert chat["model"] == "normal-model" and chat["max_output_tokens"] == 3000
    assert "reasoning" not in chat
    assert live["instructions"].startswith(chat["instructions"])
    assert live["store"] is chat["store"] is False
    assert TOKEN not in json.dumps(requests)


@pytest.mark.anyio
async def test_task_bound_authorization_expires_and_next_run_revalidates(mock_backend):
    transport, calls = mock_backend
    async with httpx.AsyncClient(transport=transport, base_url="https://control.test") as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        async with ReadTools._authorized_run(client, "manager") as tools:
            await tools.get_management_overview()
            await tools.list_management_tasks()
            assert sum(c["path"] == "/api/auth/me" for c in calls) == 1
            with pytest.raises(ManagementRequired):
                await asyncio.create_task(tools.get_management_overview())
            with pytest.raises(TypeError):
                pickle.dumps(tools)
        with pytest.raises(ManagementRequired):
            await tools.get_management_overview()
        async with ReadTools._authorized_run(client, "manager") as next_tools:
            await next_tools.get_management_overview()
        assert sum(c["path"] == "/api/auth/me" for c in calls) == 2
        client.clear_credentials()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["success", "failure", "cancel"])
async def test_search_parallel_and_sibling_cleanup(payloads, mode):
    arrived = set()
    both = asyncio.Event()
    released = set()

    async def respond(request):
        path = request.url.path
        if path != "/api/auth/me":
            arrived.add(path)
            if len(arrived) == 2:
                both.set()
            try:
                await both.wait()
                if mode == "cancel" or (mode == "failure" and path == "/api/management/tasks"):
                    await asyncio.Event().wait()
                if mode == "failure":
                    return httpx.Response(503)
            finally:
                released.add(path)
        return httpx.Response(200, json=payloads[path])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://control.test"
    ) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        task = asyncio.create_task(ReadTools(client).find_projects("Casa"))
        await asyncio.wait_for(both.wait(), 1)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif mode == "failure":
            from app.core.errors import BackendResponseError

            with pytest.raises(BackendResponseError):
                await task
        else:
            assert await task == []
        assert released == arrived
        client.clear_credentials()


@pytest.mark.anyio
async def test_correlated_sideband_latency_no_content(mock_backend, caplog):
    caplog.set_level("INFO", logger="sng_intelligence")
    model = FakeModel(call("get_management_overview"), final("PRIVATE BUSINESS ANSWER"))
    async with setup_live(mock_backend[0], model) as (_, socket, *_):
        begin(socket)
        socket.incoming.put_nowait(function("call_1", "PRIVATE QUESTION"))
        complete(socket)
        await until(lambda: any(x["type"] == "response.create" for x in socket.sent))
        from test_sideband import response

        socket.incoming.put_nowait(response("response.created", response={"id": "response_2"}))
        complete(socket, "response_2")
        await until(lambda: any('"operation": "live_turn"' in r.message for r in caplog.records))
    rows = [json.loads(r.message) for r in caplog.records if r.name == "sng_intelligence"]
    turn = next(r for r in rows if r.get("operation") == "live_turn")
    assert turn["agent_model_calls"] == 2 and turn["agent_tool_rounds"] == 1
    assert turn["tool_calls"] == turn["auth_calls"] == 1
    assert turn["management_revalidation_ms"] >= 0 and turn["agent_model_ms"] >= 0
    assert turn["backend_tools_ms"] >= 0 and turn["agent_total_ms"] >= 0
    assert turn["tool_output_to_delegated_response_ms"] >= 0
    assert turn["total_turn_ms"] is None
    for forbidden in [
        TOKEN,
        PROJECT_ID,
        "PRIVATE QUESTION",
        "PRIVATE BUSINESS ANSWER",
        "delegation_1",
    ]:
        assert forbidden not in caplog.text
    backend = next(r for r in rows if r.get("operation") == "management_overview")
    assert set(backend) == {"operation", "duration_ms", "status_category"}


@pytest.mark.anyio
async def test_metric_scope_isolated():
    from app.core.latency import count

    a, b = LatencyMetrics(), LatencyMetrics()

    async def run(metrics):
        with latency_scope(metrics):
            count("tool_calls")
            await asyncio.sleep(0)
            count("tool_calls")

    await asyncio.gather(run(a), run(b))
    count("tool_calls")
    assert a.snapshot() == b.snapshot()
    assert a.snapshot()["tool_calls"] == 2
