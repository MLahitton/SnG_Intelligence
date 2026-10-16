import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import openai
import pytest
from conftest import PROJECT_ID, TOKEN
from fastapi.testclient import TestClient
from test_agent import FakeModel, call, final

from app.agent.errors import ProviderConfigurationError, ProviderRateLimited, ProviderTimeout
from app.api import voice as voice_api
from app.api.dependencies import get_agent_model
from app.api.voice import get_speaker, get_transcriber
from app.main import create_app
from app.voice import openai_audio
from app.voice.config import MAX_AUDIO_BYTES, MAX_SPEECH_CHARACTERS, MAX_TRANSCRIPT_CHARACTERS
from app.voice.errors import AudioInvalid

AUTH = {"Authorization": f"Bearer {TOKEN}"}
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00audio-test-content"
FORMATS = [
    ("webm", "audio/webm", b"\x1a\x45\xdf\xa3webm-test-content"),
    ("wav", "audio/wav", b"RIFF\x24\x00\x00\x00WAVEfmt wav-test-content"),
    ("mp3", "audio/mpeg", MP3),
    ("m4a", "audio/mp4", b"\x00\x00\x00\x18ftypM4A m4a-test-content"),
    ("ogg", "audio/ogg", b"OggSogg-test-content"),
]
ENDPOINTS = [
    "/api/intelligence/voice/transcribe",
    "/api/intelligence/voice",
    "/api/intelligence/voice/speech",
]


class FakeSpeechToText:
    def __init__(self, result="¿Cómo va SG001?", error=None):
        self.result = result
        self.error = error
        self.inputs = []

    async def transcribe(self, audio):
        self.inputs.append(audio)
        if self.error:
            raise self.error
        return self.result


class FakeTextToSpeech:
    def __init__(self, result=MP3, error=None):
        self.result = result
        self.error = error
        self.inputs = []
        self.instructions = []

    async def synthesize(self, text, *, instructions):
        self.inputs.append(text)
        self.instructions.append(instructions)
        if self.error:
            raise self.error
        return self.result


def voice_app(settings, transport, *, stt=None, tts=None, model=None):
    stt = stt or FakeSpeechToText()
    tts = tts or FakeTextToSpeech()
    model = model or FakeModel(final())
    app = create_app(settings, transport=transport)
    app.dependency_overrides[get_transcriber] = lambda: stt
    app.dependency_overrides[get_speaker] = lambda: tts
    app.dependency_overrides[get_agent_model] = lambda: model
    return app, stt, tts, model


def send(api, endpoint, headers=AUTH):
    if endpoint.endswith("/speech"):
        return api.post(endpoint, headers=headers, json={"text": "Respuesta de Gerencia."})
    return api.post(endpoint, headers=headers, files={"file": ("test.mp3", MP3, "audio/mpeg")})


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    "management,headers,status", [(True, AUTH, 200), (True, {}, 401), (False, AUTH, 403)]
)
def test_voice_endpoints_require_existing_management_guard(
    settings, mock_backend, payloads, endpoint, management, headers, status
):
    payloads["/api/auth/me"]["isManagement"] = management
    transport, calls = mock_backend
    app, stt, tts, model = voice_app(settings, transport)
    with TestClient(app) as api:
        response = send(api, endpoint, headers)
    assert response.status_code == status
    if status != 200:
        assert not stt.inputs and not tts.inputs and not model.inputs
    if status == 401:
        assert not calls
    assert TOKEN not in str(response.content) + repr(response.headers)


@pytest.mark.parametrize("extension,mime,content", FORMATS)
def test_valid_audio_formats_and_transcript_only(settings, mock_backend, extension, mime, content):
    transport, _ = mock_backend
    app, stt, tts, model = voice_app(settings, transport)
    with TestClient(app) as api:
        response = api.post(
            ENDPOINTS[0], headers=AUTH, files={"file": (f"{TOKEN}.{extension}", content, mime)}
        )
    assert response.status_code == 200
    assert response.json() == {"transcript": "¿Cómo va SG001?"}
    assert stt.inputs[0].content == content
    assert TOKEN not in repr(stt.inputs)
    assert not tts.inputs and not model.inputs


@pytest.mark.parametrize(
    "filename,mime,data,status",
    [
        ("test.mp3", "text/plain", MP3, 415),
        ("test.exe", "audio/mpeg", MP3, 415),
        ("test.mp3", "audio/wav", MP3, 415),
        ("test.mp3", "audio/mpeg", b"", 422),
        ("test.wav", "audio/wav", MP3, 422),
        ("test.webm", "audio/webm", b"plain text", 422),
        ("test.mp3", "audio/mpeg", MP3 + b"x" * MAX_AUDIO_BYTES, 413),
    ],
    ids=["mime", "extension", "mime-mismatch", "empty", "signature", "invalid-webm", "oversize"],
)
def test_invalid_upload_fails_before_provider(settings, mock_backend, filename, mime, data, status):
    transport, _ = mock_backend
    app, stt, _, model = voice_app(settings, transport)
    with TestClient(app) as api:
        response = api.post(ENDPOINTS[1], headers=AUTH, files={"file": (filename, data, mime)})
    assert response.status_code == status
    assert not stt.inputs and not model.inputs


@pytest.mark.parametrize(
    "transcript,status,code",
    [
        ("", 422, "empty_transcription"),
        (" \t\n", 422, "empty_transcription"),
        ("x" * (MAX_TRANSCRIPT_CHARACTERS + 1), 422, "transcript_too_long"),
        (None, 502, "invalid_audio_response"),
        (TOKEN, 422, "sensitive_content_rejected"),
    ],
)
def test_unusable_transcript_never_reaches_agent(settings, mock_backend, transcript, status, code):
    transport, _ = mock_backend
    app, stt, _, model = voice_app(settings, transport, stt=FakeSpeechToText(transcript))
    with TestClient(app) as api:
        response = send(api, ENDPOINTS[1])
    assert response.status_code == status
    assert response.json()["error"] == code
    assert stt.inputs and not model.inputs
    assert TOKEN not in response.text


def test_voice_and_text_use_same_agent_tools_and_grounding(settings, mock_backend):
    transport, calls = mock_backend
    answer = "Proyecto: preparar cotización, sin iniciar."
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        final(answer, [f"project:{PROJECT_ID}"]),
        call("get_project_details", {"project_id": PROJECT_ID}),
        final(answer, [f"project:{PROJECT_ID}"]),
    )
    app, stt, tts, _ = voice_app(settings, transport, model=model)
    with TestClient(app) as api:
        voice = send(api, ENDPOINTS[1])
        text = api.post("/api/intelligence/chat", headers=AUTH, json={"message": stt.result})
    assert voice.status_code == text.status_code == 200
    assert voice.json() == {"transcript": stt.result, **text.json()}
    assert voice.json()["answer"] == answer
    assert voice.json()["references"][0]["projectId"] == PROJECT_ID
    assert model.inputs[0][0][0].content == stt.result
    assert model.inputs[2][0][0].content == stt.result
    assert not tts.inputs
    assert all(c["method"] == "GET" for c in calls)


def test_invented_voice_reference_rejected(settings, mock_backend):
    transport, _ = mock_backend
    app, _, tts, _ = voice_app(
        settings, transport, model=FakeModel(final(keys=[f"project:{PROJECT_ID}"]))
    )
    with TestClient(app) as api:
        response = send(api, ENDPOINTS[1])
    assert response.status_code == 502
    assert response.json()["error"] == "ungrounded_reference"
    assert not tts.inputs


@pytest.mark.parametrize(
    "stage,error,status",
    [
        ("stt", ProviderTimeout(), 504),
        ("stt", AudioInvalid(), 422),
        ("stt", ProviderRateLimited(), 503),
        ("agent", ProviderConfigurationError(), 503),
        ("tts", ProviderTimeout(), 504),
        ("tts", ProviderRateLimited(), 503),
    ],
)
def test_stage_failures_stop_pipeline_safely(settings, mock_backend, stage, error, status):
    transport, _ = mock_backend
    app, stt, tts, model = voice_app(
        settings,
        transport,
        stt=FakeSpeechToText(error=error if stage == "stt" else None),
        tts=FakeTextToSpeech(error=error if stage == "tts" else None),
        model=FakeModel(error) if stage == "agent" else None,
    )
    with TestClient(app) as api:
        response = send(api, ENDPOINTS[2] if stage == "tts" else ENDPOINTS[1])
    assert response.status_code == status
    assert response.json()["error"] == error.code
    if stage == "stt":
        assert not model.inputs
    if stage != "tts":
        assert not tts.inputs
    assert "www-authenticate" not in response.headers


def test_speech_returns_inline_bytes_without_json_or_cache(settings, mock_backend):
    transport, _ = mock_backend
    app, stt, tts, model = voice_app(settings, transport)
    with TestClient(app) as api:
        response = send(api, ENDPOINTS[2])
    assert response.status_code == 200
    assert response.content == MP3
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "no-store"
    assert "content-disposition" not in response.headers
    assert tts.inputs == ["Respuesta de Gerencia."]
    assert not stt.inputs and not model.inputs


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": ""},
        {"text": " \n"},
        {"text": 12},
        {"text": "a" * (MAX_SPEECH_CHARACTERS + 1)},
        {"text": "ok", "voice": "custom"},
        {"text": TOKEN},
    ],
)
def test_invalid_speech_input_never_reaches_provider(settings, mock_backend, body):
    transport, _ = mock_backend
    app, _, tts, _ = voice_app(settings, transport)
    with TestClient(app) as api:
        response = api.post(ENDPOINTS[2], headers=AUTH, json=body)
    assert response.status_code == 422
    assert not tts.inputs
    assert TOKEN not in response.text


@pytest.mark.parametrize("audio", [b"", b"not audio", None, b"ID3" + TOKEN.encode()])
def test_invalid_tts_output_is_safe_json_before_audio_response(settings, mock_backend, audio):
    transport, _ = mock_backend
    app, _, _, _ = voice_app(settings, transport, tts=FakeTextToSpeech(result=audio))
    with TestClient(app) as api:
        response = send(api, ENDPOINTS[2])
    assert response.status_code in (422, 502)
    assert response.headers["content-type"] == "application/json"
    assert TOKEN not in response.text


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_missing_configuration_keeps_health_available(
    settings, mock_backend, monkeypatch, endpoint
):
    for name in (
        "OPENAI_API_KEY",
        "SNG_INTELLIGENCE_TRANSCRIPTION_MODEL",
        "SNG_INTELLIGENCE_SPEECH_MODEL",
        "SNG_INTELLIGENCE_VOICE",
    ):
        monkeypatch.delenv(name, raising=False)
    transport, _ = mock_backend
    with TestClient(create_app(settings, transport=transport)) as api:
        assert api.get("/health").status_code == 200
        response = send(api, endpoint)
    assert response.status_code == 503
    assert response.json()["error"] == "voice_not_configured"


@pytest.mark.parametrize("stage", ["stt", "tts"])
def test_stage_wall_timeout_cancels_work(settings, mock_backend, monkeypatch, stage):
    finished = []

    class SlowProvider:
        async def transcribe(self, _, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(True)

        synthesize = transcribe

    monkeypatch.setattr(voice_api, "TRANSCRIPTION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(voice_api, "SPEECH_TIMEOUT_SECONDS", 0.01)
    transport, _ = mock_backend
    app, _, _, model = voice_app(
        settings,
        transport,
        stt=SlowProvider() if stage == "stt" else None,
        tts=SlowProvider() if stage == "tts" else None,
    )
    with TestClient(app) as api:
        response = send(api, ENDPOINTS[1] if stage == "stt" else ENDPOINTS[2])
    assert response.status_code == 504
    assert finished and not model.inputs


def test_voice_logs_only_metadata(settings, mock_backend, monkeypatch, caplog):
    key = "test-voice-secret-key"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    caplog.set_level("DEBUG")
    transport, _ = mock_backend
    app, _, _, _ = voice_app(settings, transport, model=FakeModel(final("PRIVATE ANSWER")))
    with TestClient(app) as api:
        assert send(api, ENDPOINTS[1]).status_code == 200
        assert send(api, ENDPOINTS[2]).status_code == 200
        for endpoint in (ENDPOINTS[0], ENDPOINTS[2]):
            if endpoint.endswith("speech"):
                response = api.post(endpoint, headers=AUTH, json={"text": key})
            else:
                response = api.post(
                    endpoint,
                    headers=AUTH,
                    files={"file": ("audio.mp3", MP3 + key.encode(), "audio/mpeg")},
                )
            assert response.status_code == 422
            assert key not in response.text
    for secret in (
        TOKEN,
        key,
        "PRIVATE ANSWER",
        "¿Cómo va SG001?",
        "audio-test-content",
        "Respuesta de Gerencia.",
    ):
        assert secret not in caplog.text
    events = [json.loads(r.message) for r in caplog.records if r.name == "sng_intelligence"]
    voice = next(e for e in events if e.get("agent_duration_ms") is not None)
    assert voice["audio_size_bytes"] == len(MP3)
    assert voice["transcription_duration_ms"] >= 0 and voice["tool_calls"] == 0


def test_openapi_voice_contracts_and_docs(settings, mock_backend):
    transport, _ = mock_backend
    with TestClient(create_app(settings, transport=transport)) as api:
        assert api.get("/docs").status_code == 200
        schema = api.get("/openapi.json").json()
    for endpoint in ENDPOINTS[:2]:
        operation = schema["paths"][endpoint]["post"]
        body = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
        assert body["properties"]["file"]["format"] == "binary"
        assert body["required"] == ["file"]
        assert operation["security"] == [{"HTTPBearer": []}]
    speech = schema["paths"][ENDPOINTS[2]]["post"]
    assert "application/json" in speech["requestBody"]["content"]
    assert set(speech["responses"]["200"]["content"]) == {"audio/mpeg"}


@pytest.mark.parametrize("endpoint", ENDPOINTS[:2])
@pytest.mark.parametrize("language", ["", " ", "es-CO", "spanish", "e1", TOKEN])
def test_invalid_stt_language_is_safe_before_provider(
    settings, mock_backend, monkeypatch, caplog, endpoint, language
):
    monkeypatch.setenv("SNG_INTELLIGENCE_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
    monkeypatch.setenv("SNG_INTELLIGENCE_TRANSCRIPTION_LANGUAGE", language)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid language must fail before creating an SDK client")

    monkeypatch.setattr(openai_audio, "openai_client", forbidden)
    transport, _ = mock_backend
    with TestClient(create_app(settings, transport=transport)) as api:
        assert api.get("/health").status_code == 200
        response = send(api, endpoint)
    assert response.status_code == 503
    assert response.json()["error"] == "provider_configuration_error"
    assert set(response.json()) == {"error", "requestId"}
    assert TOKEN not in response.text + caplog.text


@pytest.mark.parametrize("endpoint", ENDPOINTS[:2])
@pytest.mark.parametrize(
    "transcript",
    ["¿Qué proyectos tenemos activos en este momento?", "¿Cómo va Smoke Test Proyecto?"],
)
def test_spanish_sdk_transcript_preserved_through_http_and_agent(
    settings, mock_backend, monkeypatch, caplog, endpoint, transcript
):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-stt-provider-key")
    monkeypatch.setenv("SNG_INTELLIGENCE_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
    monkeypatch.setenv("SNG_INTELLIGENCE_TRANSCRIPTION_LANGUAGE", "es")
    calls = []

    def provider(request):
        assert request.url.path == "/v1/audio/transcriptions"
        assert b'name="language"\r\n\r\nes\r\n' in request.content
        assert TOKEN not in str(request.headers)
        assert TOKEN.encode() not in request.content
        assert b"fake-stt-provider-key" not in request.content
        calls.append(request.url.path)
        return httpx.Response(200, json={"text": transcript})

    @asynccontextmanager
    async def factory(key, timeout_seconds):
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http,
            openai.AsyncOpenAI(api_key=key.get_secret_value(), http_client=http) as sdk,
        ):
            yield sdk

    monkeypatch.setattr(openai_audio, "openai_client", factory)
    transport, _ = mock_backend
    app = create_app(settings, transport=transport)
    answer = "No dispongo de esa información."
    model = FakeModel(final(answer))
    app.dependency_overrides[get_agent_model] = lambda: model
    with TestClient(app) as api:
        response = send(api, endpoint)
    assert response.status_code == 200
    assert response.json()["transcript"] == transcript
    if endpoint.endswith("/transcribe"):
        assert response.json() == {"transcript": transcript}
        assert not model.inputs
    else:
        assert model.inputs[0][0][0].content == transcript
        assert response.json()["answer"] == answer
    assert calls == ["/v1/audio/transcriptions"]
    assert TOKEN not in response.text + caplog.text
    assert transcript not in caplog.text
