import json
import traceback
from contextlib import asynccontextmanager

import httpx
import openai
import pytest
from conftest import TOKEN
from test_memory import draft
from test_openai_model import KEY, function_call, output_message, reasoning_item, response_body

from app.agent.errors import (
    AgentUnavailable,
    InvalidModelResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    SensitiveContent,
)
from app.conversation import openai_memory
from app.conversation.openai_memory import OpenAIMemoryCompactor

pytestmark = pytest.mark.anyio
PAYLOAD = json.dumps({"previousMemory": None, "nextTurns": [], "allowedHistoricalReferents": []})


async def invoke(handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with openai.AsyncOpenAI(api_key=KEY, http_client=http, max_retries=0) as sdk:
            return await OpenAIMemoryCompactor(sdk, "memory-model").compact(PAYLOAD)


async def test_responses_strict_schema_no_tools_storage_or_auth_content():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert body["model"] == "memory-model" and body["store"] is False
        assert body["input"] == [{"role": "user", "content": PAYLOAD}]
        assert body["instructions"] == openai_memory.COMPACTION_INSTRUCTIONS
        schema = body["text"]["format"]
        assert schema["strict"] and schema["type"] == "json_schema"
        assert schema["schema"]["additionalProperties"] is False
        assert set(schema["schema"]["required"]) == {
            "summary",
            "topics",
            "userDecisions",
            "userAssumptions",
            "openThreads",
            "priorProposals",
        }
        assert (
            not {"tools", "reasoning", "include", "previous_response_id", "conversation"}
            & body.keys()
        )
        assert KEY not in request.content.decode() and TOKEN not in request.content.decode()
        return httpx.Response(
            200,
            json=response_body(
                [reasoning_item(), output_message(draft().model_dump_json(by_alias=True))]
            ),
        )

    assert await invoke(handler) == draft()


@pytest.mark.parametrize(
    "body",
    [
        response_body([]),
        response_body([function_call()]),
        response_body([output_message("not json")]),
        response_body([output_message("{}")]),
        response_body([output_message(draft().model_dump_json()), output_message()]),
        response_body(status="incomplete"),
        response_body([output_message(content=[{"type": "refusal", "refusal": "private"}])]),
        response_body(
            [output_message(json.dumps({**draft().model_dump(), "currentStatus": "overdue"}))]
        ),
        response_body(
            [output_message(json.dumps({**draft().model_dump(), "summary": "x" * 1801}))]
        ),
    ],
)
async def test_malformed_memory_fails_safely(body):
    with pytest.raises(InvalidModelResponse) as error:
        await invoke(lambda request: httpx.Response(200, json=body))
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "status,error_type",
    [(429, ProviderRateLimited), (500, ProviderUnavailable), (503, ProviderUnavailable)],
)
async def test_provider_errors_never_expose_upstream(status, error_type, caplog):
    upstream = "private-upstream"
    with pytest.raises(error_type) as error:
        await invoke(lambda request: httpx.Response(status, json={"error": {"message": upstream}}))
    assert "private-upstream" not in caplog.text + "".join(traceback.format_exception(error.value))


async def test_timeout_and_sensitive_generated_text():
    def timeout(request):
        raise httpx.ReadTimeout("private-upstream", request=request)

    with pytest.raises(ProviderTimeout):
        await invoke(timeout)
    text = json.dumps({**draft().model_dump(), "summary": "Bearer private-token"})
    with pytest.raises(SensitiveContent):
        await invoke(
            lambda request: httpx.Response(200, json=response_body([output_message(text)]))
        )


@pytest.mark.parametrize(
    "configured,expected",
    [(None, "main-model"), ("", "main-model"), ("custom-memory", "custom-memory")],
)
async def test_lazy_configuration_optional_memory_model(monkeypatch, configured, expected):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("SNG_INTELLIGENCE_MODEL", "main-model")
    monkeypatch.delenv("SNG_INTELLIGENCE_MEMORY_MODEL", raising=False)
    if configured is not None:
        monkeypatch.setenv("SNG_INTELLIGENCE_MEMORY_MODEL", configured)
    calls = []

    @asynccontextmanager
    async def client(key, seconds):
        assert key.get_secret_value() == KEY and seconds == 20
        calls.append("client")
        yield object()

    async def compact(self, payload):
        assert self._model == expected and payload == PAYLOAD
        return draft()

    monkeypatch.setattr(openai_memory, "openai_client", client)
    monkeypatch.setattr(OpenAIMemoryCompactor, "compact", compact)
    assert not calls
    assert await openai_memory.compact_memory(PAYLOAD) == draft()
    assert calls == ["client"]
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(AgentUnavailable):
        await openai_memory.compact_memory(PAYLOAD)
    assert calls == ["client"]
