import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import openai
from pydantic import SecretStr

from app.agent.errors import (
    ProviderConfigurationError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.clients.openai import openai_client
from app.core.errors import IntelligenceError
from app.voice.config import (
    BUILTIN_VOICES,
    MAX_SPEECH_BYTES,
    SPEECH_TIMEOUT_SECONDS,
    TRANSCRIPTION_TIMEOUT_SECONDS,
    VoiceSettings,
)
from app.voice.contracts import AudioInput
from app.voice.errors import AudioInvalid, AudioTooLarge, InvalidAudioResponse, VoiceNotConfigured
from app.voice.style import speech_instructions
from app.voice.upload import has_audio_signature


def _provider_error(exc: Exception, *, transcription: bool) -> IntelligenceError:
    if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException)):
        return ProviderTimeout()
    if isinstance(exc, openai.RateLimitError):
        return ProviderRateLimited()
    if isinstance(
        exc, (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError)
    ):
        return ProviderConfigurationError()
    if isinstance(exc, openai.BadRequestError):
        # Only classify structured provider fields; never forward or log its message.
        if transcription and exc.param == "file":
            return AudioInvalid()
        return ProviderConfigurationError()
    if isinstance(exc, openai.APIStatusError):
        if transcription and exc.status_code == 413:
            return AudioTooLarge()
        if transcription and exc.status_code in (415, 422):
            return AudioInvalid()
        return ProviderUnavailable()
    if isinstance(exc, (openai.APIConnectionError, httpx.RequestError)):
        return ProviderUnavailable()
    return InvalidAudioResponse()


class OpenAITranscription:
    def __init__(self, client: openai.AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def transcribe(self, audio: AudioInput) -> str:
        try:
            response = await self._client.audio.transcriptions.create(
                model=self._model,
                file=(f"audio.{audio.extension}", audio.content, audio.content_type),
                response_format="json",
                # Preserve the spoken language; no translation endpoint or forced language.
            )
            if not isinstance(response.text, str):
                raise ValueError("Unexpected transcription")
            return response.text
        except (
            openai.OpenAIError,
            httpx.RequestError,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            error = _provider_error(exc, transcription=True)
        raise error


class OpenAISpeech:
    def __init__(self, client: openai.AsyncOpenAI, model: str, voice: str) -> None:
        self._client = client
        self._model = model
        self._voice = voice

    async def synthesize(
        self, text: str, *, instructions: str = speech_instructions("direct")
    ) -> bytes:
        try:
            # Incremental HTTP reading only to cap memory; the API response is fully buffered.
            async with self._client.audio.speech.with_streaming_response.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format="mp3",
                instructions=(
                    openai.omit if self._model in {"tts-1", "tts-1-hd"} else instructions
                ),
            ) as response:
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
                    raise ValueError("Unexpected speech content type")
                content = bytearray()
                async for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if len(content) + len(chunk) > MAX_SPEECH_BYTES:
                        raise ValueError("Speech output exceeds limit")
                    content.extend(chunk)
                audio = bytes(content)
                if not has_audio_signature(audio, "mp3"):
                    raise ValueError("Missing or invalid speech audio")
                return audio
        except (
            openai.OpenAIError,
            httpx.RequestError,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            error = _provider_error(exc, transcription=False)
        raise error


def _provider_key() -> SecretStr:
    key = SecretStr(os.environ.get("OPENAI_API_KEY", ""))
    if not key.get_secret_value().strip():
        raise VoiceNotConfigured()
    return key


@asynccontextmanager
async def configured_transcription() -> AsyncIterator[OpenAITranscription]:
    settings = VoiceSettings.from_env()
    if not settings.transcription_model:
        raise VoiceNotConfigured()
    async with openai_client(_provider_key(), TRANSCRIPTION_TIMEOUT_SECONDS) as client:
        yield OpenAITranscription(client, settings.transcription_model)


@asynccontextmanager
async def configured_speech() -> AsyncIterator[OpenAISpeech]:
    settings = VoiceSettings.from_env()
    if not settings.speech_model or not settings.voice:
        raise VoiceNotConfigured()
    if settings.voice not in BUILTIN_VOICES:
        raise ProviderConfigurationError()
    async with openai_client(_provider_key(), SPEECH_TIMEOUT_SECONDS) as client:
        yield OpenAISpeech(client, settings.speech_model, settings.voice)
