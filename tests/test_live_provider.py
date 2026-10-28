import asyncio
import json
import ssl
from dataclasses import replace

import httpx
import pytest
from conftest import TOKEN
from pydantic import SecretStr
from test_live import KEY, LIVE_SETTINGS, SDP, SESSION

from app.agent.errors import (
    ProviderConfigurationError,
    ProviderRateLimited,
    ProviderTimeout,
    SensitiveContent,
)
from app.live.config import MAX_PROVIDER_BYTES, LiveSettings
from app.live.errors import LiveNotConfigured, LiveProviderError
from app.live.provider import CREATE_URL, OpenAILiveProvider, configured_live_provider


def created():
    return {"session": {"id": SESSION}, "transport": {"type": "webrtc", "sdp": SDP}}


@pytest.mark.anyio
async def test_real_http_contract_config_and_credential_isolation():
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == CREATE_URL
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert TOKEN not in str(request.headers)
        assert KEY not in request.content.decode() and TOKEN not in request.content.decode()
        body = json.loads(request.content)
        assert body["transport"] == {"type": "webrtc", "sdp": SDP}
        session = body["session"]
        assert session["model"] == "custom-live-model"
        assert session["audio"] == {"output": {"voice": "cedar"}}
        assert session["store"] is False
        assert session["delegation"]["type"] == "responses"
        delegation = session["delegation"]["responses"]
        assert delegation["model"] == "custom-delegation-model"
        assert delegation["parallel_tool_calls"] is False
        assert delegation["tool_choice"] == "required"
        assert len(delegation["tools"]) == 1
        tool = delegation["tools"][0]
        assert tool["name"] == "consult_sng_intelligence" and tool["strict"] is True
        assert tool["parameters"]["additionalProperties"] is False
        assert tool["parameters"]["required"] == ["question"]
        assert set(tool["parameters"]["properties"]) == {"question"}
        assert "session.update" not in session["client"]["data_channel"]["allowed_client_events"]
        assert session["client"]["data_channel"]["allowed_client_events"] == ["session.close"]
        selectors = session["client"]["data_channel"]["allowed_server_events"]
        assert {"type": "session.input_transcript.delta"} in selectors
        assert {
            "type": "response.event",
            "response_event": "response.output_item.done",
        } not in selectors
        # Drop provider-only fields, including any accidental echoed credential.
        return httpx.Response(201, json={**created(), "internal_secret": KEY})

    settings = replace(
        LIVE_SETTINGS,
        model="custom-live-model",
        delegation_model="custom-delegation-model",
        voice="cedar",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await OpenAILiveProvider(http, SecretStr(KEY), settings).create(SDP)
    assert result.model_dump(by_alias=True) == {"sessionId": SESSION, "sdp": SDP}
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize(
    "status,error",
    [
        (400, ProviderConfigurationError),
        (401, ProviderConfigurationError),
        (403, ProviderConfigurationError),
        (404, ProviderConfigurationError),
        (422, ProviderConfigurationError),
        (429, ProviderRateLimited),
        (500, LiveProviderError),
        (503, LiveProviderError),
        (302, LiveProviderError),
        (200, LiveProviderError),
    ],
)
@pytest.mark.anyio
async def test_safe_status_errors_no_redirects(status, error):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, text=KEY + TOKEN, headers={"location": "https://untrusted.test/"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(error) as caught:
            await OpenAILiveProvider(http, SecretStr(KEY), LIVE_SETTINGS).create(SDP)
    assert KEY not in str(caught.value) and TOKEN not in str(caught.value)
    assert len(calls) == 1 and "authorization" not in calls[0].headers


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"session": {"id": "https://evil.test"}, "transport": {"type": "webrtc", "sdp": SDP}},
        {"session": {"id": SESSION}, "transport": {"type": "sip", "sdp": SDP}},
        {"session": {"id": SESSION}, "transport": {"type": "webrtc", "sdp": "invalid"}},
    ],
    ids=["missing", "invalid-id", "wrong-transport", "invalid-sdp"],
)
@pytest.mark.anyio
async def test_malformed_provider_contract(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(201, json=body))
    ) as http:
        with pytest.raises(LiveProviderError):
            await OpenAILiveProvider(http, SecretStr(KEY), LIVE_SETTINGS).create(SDP)


@pytest.mark.parametrize(
    "content,media",
    [
        (b"not-json", "application/json"),
        (b"{}", "text/html"),
        (b"x" * (MAX_PROVIDER_BYTES + 1), "application/json"),
    ],
    ids=["invalid-json", "wrong-mime", "oversized"],
)
@pytest.mark.anyio
async def test_bounded_provider_response(content, media):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(201, content=content, headers={"content-type": media})
        )
    ) as http:
        with pytest.raises(LiveProviderError):
            await OpenAILiveProvider(http, SecretStr(KEY), LIVE_SETTINGS).create(SDP)


@pytest.mark.parametrize(
    "exception,error",
    [
        (httpx.ReadTimeout, ProviderTimeout),
        (httpx.ConnectError, LiveProviderError),
    ],
)
@pytest.mark.anyio
async def test_network_error_is_safe(exception, error):
    def respond(request):
        raise exception(KEY + TOKEN, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(error) as caught:
            await OpenAILiveProvider(http, SecretStr(KEY), LIVE_SETTINGS).create(SDP)
    assert caught.value.__context__ is None
    assert KEY not in str(caught.value) and TOKEN not in str(caught.value)


@pytest.mark.anyio
async def test_total_timeout_and_cancellation():
    async def respond(request):
        await asyncio.sleep(60)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = OpenAILiveProvider(
            http, SecretStr(KEY), replace(LIVE_SETTINGS, timeout_seconds=1)
        )
        with pytest.raises(ProviderTimeout):
            await provider.create(SDP)
        task = asyncio.create_task(provider.create(SDP))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_secret_in_projected_provider_answer_is_blocked():
    body = created()
    body["transport"]["sdp"] += f"a=private:{KEY}\r\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(201, json=body))
    ) as http:
        with pytest.raises(SensitiveContent):
            await OpenAILiveProvider(http, SecretStr(KEY), LIVE_SETTINGS).create(SDP)


def configure(monkeypatch):
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_MODEL", "gpt-live-1")
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_DELEGATION_MODEL", "gpt-6-luna")
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_VOICE", "marin")
    monkeypatch.delenv("SNG_INTELLIGENCE_LIVE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("SNG_INTELLIGENCE_LIVE_LIFETIME_SECONDS", raising=False)


def test_configured_values_and_defaults(monkeypatch):
    configure(monkeypatch)
    assert LiveSettings.from_env() == LIVE_SETTINGS
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_VOICE", "cedar")
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_LIFETIME_SECONDS", "300")
    assert LiveSettings.from_env() == replace(
        LIVE_SETTINGS, voice="cedar", timeout_seconds=12, lifetime_seconds=300
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODEL", "https://evil.test"),
        ("DELEGATION_MODEL", "x\n" + TOKEN),
        ("VOICE", "custom-voice"),
        ("TIMEOUT_SECONDS", "0"),
        ("TIMEOUT_SECONDS", "61"),
        ("TIMEOUT_SECONDS", "NaN"),
        ("LIFETIME_SECONDS", "0"),
        ("LIFETIME_SECONDS", "601"),
    ],
)
def test_invalid_configuration_is_safe(monkeypatch, name, value):
    configure(monkeypatch)
    monkeypatch.setenv("SNG_INTELLIGENCE_LIVE_" + name, value)
    with pytest.raises(ProviderConfigurationError) as caught:
        LiveSettings.from_env()
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("name", ["MODEL", "DELEGATION_MODEL", "VOICE"])
def test_missing_configuration(monkeypatch, name):
    configure(monkeypatch)
    monkeypatch.delenv("SNG_INTELLIGENCE_LIVE_" + name)
    with pytest.raises(LiveNotConfigured):
        LiveSettings.from_env()


@pytest.mark.anyio
async def test_factory_tls_no_redirects_and_client_closed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    real_client = httpx.AsyncClient
    clients = []

    def client(**kwargs):
        tls = kwargs["verify"]
        assert tls.verify_mode == ssl.CERT_REQUIRED and tls.check_hostname
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 30
        result = real_client(**kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr("app.live.provider.httpx.AsyncClient", client)
    async with configured_live_provider(LIVE_SETTINGS):
        assert not clients[0].is_closed
    assert clients[0].is_closed
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(LiveNotConfigured):
        async with configured_live_provider(LIVE_SETTINGS):
            pytest.fail("must not create unconfigured provider")
