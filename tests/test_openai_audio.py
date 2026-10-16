import json
import ssl
from contextlib import asynccontextmanager

import httpx
import openai
import pytest
import truststore
from conftest import TOKEN
from pydantic import SecretStr
from test_voice import MP3

from app.agent.errors import (
    ProviderConfigurationError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.clients.openai import openai_client
from app.voice import openai_audio
from app.voice.config import SPEECH_INSTRUCTIONS, TRANSCRIPTION_PROMPT_ES, VoiceSettings
from app.voice.contracts import AudioInput
from app.voice.errors import AudioInvalid, AudioTooLarge, InvalidAudioResponse, VoiceNotConfigured
from app.voice.openai_audio import (
    OpenAISpeech,
    OpenAITranscription,
    configured_speech,
    configured_transcription,
)
from app.voice.style import speech_instructions

pytestmark = pytest.mark.anyio
KEY = "fake-openai-audio-key"


async def invoke(handler, stage="stt", *, model=None, language="es"):
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        openai.AsyncOpenAI(
            api_key=KEY,
            base_url="https://api.openai.com/v1",
            http_client=http,
            max_retries=0,
            timeout=60,
        ) as sdk,
    ):
        if stage == "stt":
            return await OpenAITranscription(
                sdk, model or "configured-transcription-model", language=language
            ).transcribe(AudioInput(MP3, "mp3", "audio/mpeg"))
        return await OpenAISpeech(sdk, model or "configured-speech-model", "coral").synthesize(
            "Preparar cotización para Peña."
        )


async def test_real_sdk_transcription_wire_format_no_credentials_or_translation():
    def provider(request):
        assert request.url.path == "/v1/audio/transcriptions"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert KEY.encode() not in request.content and TOKEN.encode() not in request.content
        assert b'filename="audio.mp3"' in request.content
        assert b"configured-transcription-model" in request.content
        assert b"audio/mpeg" in request.content and MP3 in request.content
        assert b'name="language"\r\n\r\nes\r\n' in request.content
        assert b'name="prompt"' not in request.content
        assert b'name="stream"' not in request.content
        assert request.extensions["timeout"]["read"] == 60
        return httpx.Response(
            200, json={"text": "¿Cómo va Peña SG001?", "usage": {"type": "duration", "seconds": 1}}
        )

    assert await invoke(provider) == "¿Cómo va Peña SG001?"


@pytest.mark.parametrize(
    "model,language,has_prompt",
    [
        ("gpt-4o-mini-transcribe", "es", True),
        ("gpt-4o-mini-transcribe-2025-12-15", "es", True),
        ("gpt-4o-transcribe", "es", True),
        ("gpt-4o-mini-transcribe", "en", False),
        ("gpt-4o-transcribe-diarize", "es", False),
        ("configured-transcription-model", "es", False),
    ],
)
async def test_transcription_language_and_supported_lexical_hint(model, language, has_prompt):
    transcript = "¿Qué proyectos tenemos activos en este momento?"

    def provider(request):
        assert request.url.path == "/v1/audio/transcriptions"
        assert f'name="language"\r\n\r\n{language}\r\n'.encode() in request.content
        assert (b'name="prompt"' in request.content) is has_prompt
        if has_prompt:
            assert TRANSCRIPTION_PROMPT_ES.encode() in request.content
        assert TOKEN not in str(request.headers)
        assert TOKEN.encode() not in request.content and KEY.encode() not in request.content
        return httpx.Response(200, json={"text": transcript})

    assert await invoke(provider, model=model, language=language) == transcript


@pytest.mark.parametrize("configured,expected", [(None, "es"), ("es", "es"), (" EN ", "en")])
async def test_language_configuration_reaches_sdk(monkeypatch, configured, expected):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("SNG_INTELLIGENCE_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
    if configured is None:
        monkeypatch.delenv("SNG_INTELLIGENCE_TRANSCRIPTION_LANGUAGE", raising=False)
    else:
        monkeypatch.setenv("SNG_INTELLIGENCE_TRANSCRIPTION_LANGUAGE", configured)
    assert VoiceSettings.from_env().transcription_language == expected
    calls = []

    def provider(request):
        assert f'name="language"\r\n\r\n{expected}\r\n'.encode() in request.content
        calls.append(request.url.path)
        return httpx.Response(200, json={"text": "¿Cómo va Smoke Test Proyecto?"})

    @asynccontextmanager
    async def factory(key, timeout_seconds):
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http,
            openai.AsyncOpenAI(api_key=key.get_secret_value(), http_client=http) as sdk,
        ):
            yield sdk

    monkeypatch.setattr(openai_audio, "openai_client", factory)
    async with configured_transcription() as stt:
        assert await stt.transcribe(AudioInput(MP3, "mp3", "audio/mpeg")) == (
            "¿Cómo va Smoke Test Proyecto?"
        )
    assert calls == ["/v1/audio/transcriptions"]


@pytest.mark.parametrize(
    "model,instructions",
    [
        ("configured-speech-model", speech_instructions("direct")),
        ("tts-1", None),
        ("tts-1-hd", None),
    ],
)
async def test_real_sdk_speech_config_and_bytes(model, instructions):
    def provider(request):
        assert request.url.path == "/v1/audio/speech"
        assert KEY.encode() not in request.content and TOKEN.encode() not in request.content
        body = json.loads(request.content)
        assert body["model"] == model and body["voice"] == "coral"
        assert body["response_format"] == "mp3"
        assert body["input"] == "Preparar cotización para Peña."
        assert body.get("instructions") == instructions
        assert "stream_format" not in body
        return httpx.Response(200, content=MP3, headers={"Content-Type": "audio/mpeg"})

    assert await invoke(provider, "tts", model=model) == MP3


@pytest.mark.parametrize("stage", ["stt", "tts"])
@pytest.mark.parametrize(
    "status,error",
    [
        (400, ProviderConfigurationError),
        (401, ProviderConfigurationError),
        (403, ProviderConfigurationError),
        (404, ProviderConfigurationError),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (503, ProviderUnavailable),
        (302, ProviderUnavailable),
    ],
)
async def test_provider_http_failures_safe_without_retries(stage, status, error, caplog):
    calls = []

    def provider(request):
        calls.append(True)
        return httpx.Response(
            status,
            json={"error": {"message": KEY + TOKEN, "type": "error"}},
            headers={"Location": "https://evil.test"},
        )

    with pytest.raises(error) as caught:
        await invoke(provider, stage)
    assert calls == [True]
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert KEY not in str(caught.value) + caplog.text
    assert TOKEN not in str(caught.value) + caplog.text


@pytest.mark.parametrize(
    "status,param,error",
    [
        (400, "file", AudioInvalid),
        (413, None, AudioTooLarge),
        (415, None, AudioInvalid),
        (422, None, AudioInvalid),
        (400, "model", ProviderConfigurationError),
    ],
)
async def test_transcription_input_errors_use_safe_provider_fields(status, param, error):
    with pytest.raises(error):
        await invoke(
            lambda _: httpx.Response(
                status, json={"error": {"message": KEY + TOKEN, "param": param}}
            )
        )


@pytest.mark.parametrize("stage", ["stt", "tts"])
@pytest.mark.parametrize(
    "failure,error",
    [(httpx.ReadTimeout, ProviderTimeout), (httpx.ConnectError, ProviderUnavailable)],
)
async def test_audio_network_errors_are_safe(stage, failure, error):
    def provider(request):
        raise failure(KEY + TOKEN, request=request)

    with pytest.raises(error) as caught:
        await invoke(provider, stage)
    assert caught.value.__context__ is None
    assert KEY not in str(caught.value) and TOKEN not in str(caught.value)


@pytest.mark.parametrize("body", [{}, {"text": None}, {"text": 12}, {"text": [KEY + TOKEN]}])
async def test_malformed_transcription(body):
    with pytest.raises(InvalidAudioResponse):
        await invoke(lambda _: httpx.Response(200, json=body))


@pytest.mark.parametrize(
    "content,mime", [(b"", "audio/mpeg"), (b"not audio", "audio/mpeg"), (MP3, "text/html")]
)
async def test_malformed_speech_output(content, mime):
    with pytest.raises(InvalidAudioResponse):
        await invoke(
            lambda _: httpx.Response(200, content=content, headers={"Content-Type": mime}), "tts"
        )


async def test_speech_output_limit_and_response_closed(monkeypatch):
    monkeypatch.setattr(openai_audio, "MAX_SPEECH_BYTES", 4)
    closed = []

    class AudioStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield MP3

        async def aclose(self):
            closed.append(True)

    with pytest.raises(InvalidAudioResponse):
        await invoke(
            lambda _: httpx.Response(
                200, stream=AudioStream(), headers={"Content-Type": "audio/mpeg"}
            ),
            "tts",
        )
    assert closed


@pytest.mark.parametrize("missing", ["OPENAI_API_KEY", "SNG_INTELLIGENCE_TRANSCRIPTION_MODEL"])
async def test_missing_transcription_config(monkeypatch, missing):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("SNG_INTELLIGENCE_TRANSCRIPTION_MODEL", "configured-transcription")
    monkeypatch.delenv(missing)
    with pytest.raises(VoiceNotConfigured):
        async with configured_transcription():
            pytest.fail("Must not configure provider")


@pytest.mark.parametrize(
    "missing", ["OPENAI_API_KEY", "SNG_INTELLIGENCE_SPEECH_MODEL", "SNG_INTELLIGENCE_VOICE"]
)
async def test_missing_speech_config(monkeypatch, missing):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("SNG_INTELLIGENCE_SPEECH_MODEL", "configured-speech")
    monkeypatch.setenv("SNG_INTELLIGENCE_VOICE", "coral")
    monkeypatch.delenv(missing)
    with pytest.raises(VoiceNotConfigured):
        async with configured_speech():
            pytest.fail("Must not configure provider")


async def test_custom_voice_forbidden(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("SNG_INTELLIGENCE_SPEECH_MODEL", "configured-speech")
    monkeypatch.setenv("SNG_INTELLIGENCE_VOICE", "voice_custom_id")
    with pytest.raises(ProviderConfigurationError):
        async with configured_speech():
            pytest.fail("Custom voices are outside Voice 1A")


async def test_config_independent_endpoints_and_native_tls(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("SNG_INTELLIGENCE_TRANSCRIPTION_MODEL", "configured-stt")
    monkeypatch.setenv("SNG_INTELLIGENCE_SPEECH_MODEL", "configured-tts")
    monkeypatch.setenv("SNG_INTELLIGENCE_VOICE", "coral")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.test")
    settings = VoiceSettings.from_env()
    assert settings.transcription_model == "configured-stt"
    async with configured_transcription() as stt, configured_speech() as tts:
        for sdk in (stt._client, tts._client):
            assert str(sdk.base_url) == "https://api.openai.com/v1/"
            assert sdk.timeout == 60 and sdk.max_retries == 0
            assert sdk._client.follow_redirects is False
            context = sdk._client._transport._pool._ssl_context
            assert isinstance(context, truststore.SSLContext)
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert stt._model == "configured-stt" and tts._model == "configured-tts"
        assert tts._voice == "coral"
    assert stt._client.is_closed() and tts._client.is_closed()


async def test_sdk_debug_logs_suppressed_by_shared_factory(monkeypatch, caplog):
    import logging

    caplog.set_level("DEBUG")
    async with openai_client(SecretStr(KEY), 60):
        logging.getLogger("openai._base_client").debug(KEY + TOKEN)
    assert KEY not in caplog.text and TOKEN not in caplog.text


def test_colombian_vocal_delivery_never_authorizes_word_or_fact_changes():
    for policy in (
        "español colombiano natural",
        "cálida y cercana",
        "pequeñas variaciones de ritmo",
        "entonación orgánica",
        "énfasis sutil",
        "cadencia relajada",
        "GPS",
        "narración institucional",
        "matiz paisa suave",
        "sin forzar",
        "Pronuncia exactamente las palabras",
        "No interpretes hechos",
    ):
        assert policy in SPEECH_INSTRUCTIONS


@pytest.mark.parametrize("voice", ["marin", "coral", "nova"])
def test_provisional_selection_does_not_override_voice_configuration(monkeypatch, voice):
    monkeypatch.setenv("SNG_INTELLIGENCE_VOICE", voice)
    assert VoiceSettings.from_env().voice == voice
    monkeypatch.delenv("SNG_INTELLIGENCE_VOICE")
    assert VoiceSettings.from_env().voice == ""


@pytest.mark.parametrize("kind", ["direct", "management_brief", "clarification"])
async def test_sdk_receives_selected_style_with_unchanged_text(kind):
    instructions = speech_instructions(kind)

    def provider(request):
        body = json.loads(request.content)
        assert body["instructions"] == instructions
        assert body["input"] == "Casa Ximena tiene 2 tareas."
        assert KEY.encode() not in request.content and TOKEN.encode() not in request.content
        return httpx.Response(200, content=MP3, headers={"Content-Type": "audio/mpeg"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http,
        openai.AsyncOpenAI(api_key=KEY, http_client=http, max_retries=0) as sdk,
    ):
        assert (
            await OpenAISpeech(sdk, "configured-model", "marin").synthesize(
                "Casa Ximena tiene 2 tareas.", instructions=instructions
            )
            == MP3
        )
