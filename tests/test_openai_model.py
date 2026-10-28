import json
from uuid import UUID, uuid4

import httpx
import openai
import pytest
from conftest import PROJECT_ID, TOKEN
from pydantic import SecretStr

from app.agent.contracts import ModelMessage, ToolCall
from app.agent.core import Agent
from app.agent.errors import (
    InvalidModelResponse,
    InvalidToolCall,
    ProviderConfigurationError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    UngroundedReference,
)
from app.agent.openai_model import OpenAIModel, configured_model
from app.agent.security import CredentialGuard
from app.agent.toolbox import TOOL_DEFINITIONS, ToolBox
from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.tools import ReadTools

pytestmark = pytest.mark.anyio
KEY = "fake-sdk-api-key"


def output_message(text=None, **overrides):
    if text is None and not overrides.pop("null_text", False):
        text = json.dumps(
            {
                "answerMode": "conversation",
                "responseType": "direct",
                "answer": "Hola",
                "spokenAnswer": "Hola",
                "reference_keys": [],
                "presentation_keys": [],
            }
        )
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
        **overrides,
    }


def function_call(call_id="call1", name="get_management_overview", arguments="{}", **overrides):
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
        **overrides,
    }


def reasoning_item(**overrides):
    return {
        "id": "rs_test",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque-test-reasoning",
        **overrides,
    }


def response_body(output=None, **overrides):
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": "test-model",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [output_message()] if output is None else output,
        **overrides,
    }


async def invoke(handler, messages=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with openai.AsyncOpenAI(
            api_key=KEY, base_url="https://api.openai.com/v1", http_client=http, max_retries=0
        ) as sdk:
            return await OpenAIModel(sdk, "test-configured-model").respond(
                messages or (ModelMessage(role="user", content="Hola"),),
                TOOL_DEFINITIONS,
            )


async def test_sdk_request_structured_output_and_no_credentials_in_content():
    def backend(request):
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert KEY not in request.content.decode()
        assert TOKEN not in request.content.decode()
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert payload["model"] == "test-configured-model"
        assert payload["store"] is False
        assert payload["parallel_tool_calls"] is False
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["text"]["format"]["strict"] is True
        assert payload["text"]["format"]["schema"]["additionalProperties"] is False
        assert payload["max_output_tokens"] == 3000
        assert payload["include"] == ["reasoning.encrypted_content"]
        assert payload["instructions"]
        assert payload["input"] == [{"role": "user", "content": "Hola"}]
        assert "previous_response_id" not in payload
        assert "conversation" not in payload
        assert "messages" not in payload
        assert "response_format" not in payload
        assert len(payload["tools"]) == 14
        assert all(
            t["type"] == "function"
            and t["strict"]
            and t["parameters"]["additionalProperties"] is False
            for t in payload["tools"]
        )
        return httpx.Response(200, json=response_body())

    result = await invoke(backend)
    assert result.answer.answer == "Hola"


async def test_sdk_tool_request_parsing_and_result_round_trip():
    call = ToolCall("call1", "get_management_overview", "{}")
    messages = (
        ModelMessage(role="user", content="Pregunta"),
        ModelMessage(role="assistant", calls=(call,)),
        ModelMessage(role="tool", content='{"complete":true}', call_id="call1"),
    )

    def backend(request):
        body = json.loads(request.content)
        assert body["input"][1] == {
            "type": "function_call",
            "call_id": "call1",
            "name": "get_management_overview",
            "arguments": "{}",
        }
        assert body["input"][2] == {
            "type": "function_call_output",
            "call_id": "call1",
            "output": '{"complete":true}',
        }
        return httpx.Response(
            200,
            json=response_body([function_call("call2", "find_projects", '{"query":"Casa"}')]),
        )

    result = await invoke(backend, messages)
    assert result.calls == (ToolCall("call2", "find_projects", '{"query":"Casa"}'),)
    assert result.answer is None


@pytest.mark.parametrize(
    "status,error_type",
    [
        (400, ProviderConfigurationError),
        (401, ProviderConfigurationError),
        (403, ProviderConfigurationError),
        (404, ProviderConfigurationError),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (503, ProviderUnavailable),
    ],
)
async def test_provider_status_mapping_safe(status, error_type, caplog):
    calls = []

    def backend(request):
        calls.append(True)
        return httpx.Response(status, json={"error": {"message": KEY + TOKEN, "type": "test"}})

    with pytest.raises(error_type) as error:
        await invoke(backend)
    assert len(calls) == 1
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert KEY not in str(error.value) + caplog.text
    assert TOKEN not in str(error.value) + caplog.text


@pytest.mark.parametrize(
    "failure,error_type",
    [(httpx.ReadTimeout, ProviderTimeout), (httpx.ConnectError, ProviderUnavailable)],
)
async def test_sdk_network_errors_safe(failure, error_type):
    def backend(request):
        raise failure(KEY + TOKEN, request=request)

    with pytest.raises(error_type) as error:
        await invoke(backend)
    assert error.value.__context__ is None
    assert KEY not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        {},
        response_body([output_message("not json")]),
        response_body([output_message('{"answer":"ok","reference_keys":[],"projectId":"forged"}')]),
        response_body(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
        response_body(status="failed", error={"code": "server_error", "message": KEY + TOKEN}),
        response_body(status="in_progress"),
        response_body(status="queued"),
        response_body([output_message(None, null_text=True)]),
        {"status": "completed", "output": None},
        response_body([]),
        response_body([reasoning_item()]),
        response_body([output_message(), function_call()]),
        response_body([output_message(), output_message()]),
        response_body([output_message(status="incomplete")]),
        response_body([output_message(role="user")]),
        response_body([output_message(content=[])]),
        response_body([output_message(content=[{"type": "refusal", "refusal": "Refused"}])]),
        response_body([{"type": "web_search_call", "id": "ws_test", "status": "completed"}]),
        response_body([{"type": "unknown"}]),
        response_body([reasoning_item(status="incomplete"), function_call()]),
    ],
)
async def test_malformed_or_incomplete_provider_response(response):
    with pytest.raises(InvalidModelResponse) as error:
        await invoke(lambda _: httpx.Response(200, json=response))
    assert error.value.__context__ is None
    assert KEY not in str(error.value)
    assert TOKEN not in str(error.value)


async def test_configured_adapter_uses_explicit_origin_no_startup_network(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("SNG_INTELLIGENCE_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.test")
    async with configured_model() as model:
        assert str(model._client.base_url) == "https://api.openai.com/v1/"
        assert model._client.max_retries == 0
        assert model._client.timeout == 30
        assert model._model == "test-model"


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "find_projects", "arguments": None},
        {"name": None, "arguments": "{}"},
        {"name": "", "arguments": "{}"},
        {"call_id": None},
        {"call_id": ""},
        {"call_id": 12},
        {"arguments": ""},
        {"arguments": {}},
        {"status": "incomplete"},
        {"namespace": "unapproved"},
        {"caller": {"type": "program", "caller_id": "program1"}},
    ],
)
async def test_malformed_function_fields_are_safe(fields):
    body = response_body([function_call(**fields)])
    with pytest.raises(InvalidModelResponse):
        await invoke(lambda _: httpx.Response(200, json=body))


async def test_multiple_calls_outputs_and_final_answer_with_reasoning_replay():
    requests = []
    provider_calls = [function_call(), function_call("call2", "list_management_tasks")]

    def backend(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["store"] is False
        assert "previous_response_id" not in body
        assert "conversation" not in body
        if len(requests) == 1:
            return httpx.Response(200, json=response_body([reasoning_item(), *provider_calls]))
        return httpx.Response(200, json=response_body([reasoning_item(), output_message()]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as http:
        async with openai.AsyncOpenAI(api_key=KEY, http_client=http, max_retries=0) as sdk:
            model = OpenAIModel(sdk, "test-model")
            messages = (ModelMessage(role="user", content="Resumen"),)
            turn = await model.respond(messages, TOOL_DEFINITIONS)
            assert len(requests) == 1  # The adapter neither executes tools nor loops.
            assert turn.calls == (
                ToolCall("call1", "get_management_overview", "{}"),
                ToolCall("call2", "list_management_tasks", "{}"),
            )
            messages += (ModelMessage(role="assistant", calls=turn.calls),)
            messages += tuple(
                ModelMessage(role="tool", call_id=call.id, content='{"data":[],"complete":true}')
                for call in turn.calls
            )
            final = await model.respond(messages, TOOL_DEFINITIONS)
            assert final.answer.answer == "Hola"
            assert not final.calls
            wire = requests[1]["input"]
            assert wire[1] == reasoning_item()
            assert [item["id"] for item in wire[2:4]] == ["fc_call1", "fc_call2"]
            assert [item["call_id"] for item in wire[2:]] == ["call1", "call2", "call1", "call2"]
            assert all(item["type"] == "function_call_output" for item in wire[4:])
            assert all(item["output"] == '{"data":[],"complete":true}' for item in wire[4:])


async def test_independent_requests_do_not_replay_provider_context():
    requests = []

    def backend(request):
        requests.append(json.loads(request.content))
        # Identical call IDs deliberately test that a new chat cannot inherit reasoning.
        items = [reasoning_item(), function_call()] if len(requests) == 1 else [function_call()]
        return httpx.Response(200, json=response_body(items))

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as http:
        async with openai.AsyncOpenAI(api_key=KEY, http_client=http, max_retries=0) as sdk:
            model = OpenAIModel(sdk, "test-model")
            await model.respond((ModelMessage(role="user", content="Primera"),), TOOL_DEFINITIONS)
            messages = (ModelMessage(role="user", content="Segunda"),)
            turn = await model.respond(messages, TOOL_DEFINITIONS)
            await model.respond(
                messages
                + (
                    ModelMessage(role="assistant", calls=turn.calls),
                    ModelMessage(role="tool", call_id="call1", content="{}"),
                ),
                TOOL_DEFINITIONS,
            )
    assert requests[1]["input"] == [{"role": "user", "content": "Segunda"}]
    assert all(item.get("type") != "reasoning" for item in requests[2]["input"])


@pytest.mark.parametrize("scenario", ["grounded", "forged_reference", "repaired", "unknown_tool"])
async def test_responses_through_core_preserves_dispatch_and_evidence(mock_backend, scenario):
    transport, backend_requests = mock_backend
    provider_requests = []

    def provider(request):
        assert TOKEN not in request.content.decode()
        assert KEY not in request.content.decode()
        body = json.loads(request.content)
        provider_requests.append(body)
        if len(provider_requests) == 1:
            name = "http_request" if scenario == "unknown_tool" else "get_management_overview"
            return httpx.Response(
                200, json=response_body([reasoning_item(), function_call(name=name)])
            )
        output = next(item for item in body["input"] if item.get("type") == "function_call_output")
        if len(provider_requests) == 3:
            assert body["tools"] == [] and body["tool_choice"] == "none"
            assert body["input"][-1]["role"] == "developer"
            assert body["input"][1] == reasoning_item()
        assert output["type"] == "function_call_output"
        evidence = json.loads(output["output"])
        assert evidence["data"]["totalProjects"] == 19
        assert evidence["complete"] is True
        key = f"project:{PROJECT_ID}"
        assert key in evidence["available_references"]
        if scenario == "forged_reference" or (
            scenario == "repaired" and len(provider_requests) == 2
        ):
            key = "project:40000000-0000-4000-8000-000000000004"
        return httpx.Response(
            200,
            json=response_body(
                [
                    output_message(
                        json.dumps(
                            {
                                "answerMode": "factual",
                                "responseType": "direct",
                                "answer": "Hay 19 proyectos.",
                                "spokenAnswer": "Hay 19 proyectos.",
                                "reference_keys": [key],
                                "presentation_keys": ["metric:total_projects"],
                            }
                        )
                    )
                ]
            ),
        )

    guard = CredentialGuard(SecretStr(TOKEN), SecretStr(KEY))
    metrics = {}
    async with (
        create_http_client("https://control.test", 7, transport) as control_http,
        httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_http,
        openai.AsyncOpenAI(api_key=KEY, http_client=provider_http, max_retries=0) as sdk,
    ):
        client = SnGControlClient(control_http, SecretStr(TOKEN), uuid4())
        agent = Agent(
            OpenAIModel(sdk, "test-model"), ToolBox(ReadTools(client), guard), guard, metrics
        )
        try:
            if scenario in {"grounded", "repaired"}:
                result = await agent.run("Resumen")
                assert result.references[0].project_id == UUID(PROJECT_ID)
                assert result.references[0].label == "Proyecto"
            else:
                error = InvalidToolCall if scenario == "unknown_tool" else UngroundedReference
                with pytest.raises(error):
                    await agent.run("Resumen")
        finally:
            client.clear_credentials()
    if scenario == "unknown_tool":
        assert not backend_requests
        assert len(provider_requests) == 1
    else:
        assert len(provider_requests) == (3 if scenario in {"forged_reference", "repaired"} else 2)
        assert [(r["method"], r["path"]) for r in backend_requests] == [
            ("GET", "/api/auth/me"),
            ("GET", "/api/management/overview"),
        ]
        assert metrics["tool_calls"] == 1


async def test_malformed_fields_do_not_emit_sensitive_serialization_warnings(recwarn, caplog):
    body = response_body([function_call(arguments={"sensitive": KEY + TOKEN})])
    with pytest.raises(InvalidModelResponse) as error:
        await invoke(lambda _: httpx.Response(200, json=body))
    assert not recwarn.list
    assert error.value.__context__ is None
    assert KEY not in caplog.text + str(error.value)
    assert TOKEN not in caplog.text + str(error.value)
