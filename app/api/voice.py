import asyncio
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.agent.core import Agent
from app.agent.errors import ProviderTimeout
from app.agent.security import CredentialGuard
from app.api.dependencies import get_agent, get_credential_guard, get_management_client
from app.voice.config import (
    MAX_SPEECH_BYTES,
    MAX_TRANSCRIPT_CHARACTERS,
    SPEECH_TIMEOUT_SECONDS,
    TRANSCRIPTION_TIMEOUT_SECONDS,
)
from app.voice.contracts import (
    AudioInput,
    SpeechRequest,
    SpeechToText,
    TextToSpeech,
    TranscriptionResponse,
    VoiceResponse,
)
from app.voice.errors import EmptyTranscription, InvalidAudioResponse, TranscriptTooLong
from app.voice.openai_audio import configured_speech, configured_transcription
from app.voice.style import speech_instructions
from app.voice.upload import has_audio_signature, read_audio

router = APIRouter(prefix="/api/intelligence", tags=["Voice"])
_AUDIO_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "additionalProperties": False,
                    "properties": {"file": {"type": "string", "format": "binary"}},
                }
            }
        },
    }
}


async def get_transcriber() -> AsyncIterator[SpeechToText]:
    async with configured_transcription() as provider:
        yield provider


async def get_speaker() -> AsyncIterator[TextToSpeech]:
    async with configured_speech() as provider:
        yield provider


async def get_audio(
    request: Request,
    management: Annotated[object, Depends(get_management_client)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> AudioInput:
    audio = await read_audio(request)
    guard.check_bytes(audio.content)
    return audio


async def get_transcript(
    request: Request,
    audio: Annotated[AudioInput, Depends(get_audio)],
    provider: Annotated[SpeechToText, Depends(get_transcriber)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> str:
    start = perf_counter()
    request.state.provider_stage = "transcription"
    try:
        async with asyncio.timeout(TRANSCRIPTION_TIMEOUT_SECONDS):
            transcript = await provider.transcribe(audio)
        if not isinstance(transcript, str):
            raise InvalidAudioResponse()
        guard.check(transcript)
        if not transcript.strip():
            raise EmptyTranscription()
        if len(transcript) > MAX_TRANSCRIPT_CHARACTERS:
            raise TranscriptTooLong()
        return transcript
    except TimeoutError:
        pass
    finally:
        request.state.transcription_duration_ms = round((perf_counter() - start) * 1000, 2)
    raise ProviderTimeout()


@router.post("/voice/transcribe", response_model=TranscriptionResponse, openapi_extra=_AUDIO_BODY)
async def transcribe(transcript: Annotated[str, Depends(get_transcript)]) -> TranscriptionResponse:
    return TranscriptionResponse(transcript=transcript)


@router.post("/voice", response_model=VoiceResponse, openapi_extra=_AUDIO_BODY)
async def voice_query(
    request: Request,
    transcript: Annotated[str, Depends(get_transcript)],
    agent: Annotated[Agent, Depends(get_agent)],
) -> VoiceResponse:
    request.state.provider_stage = "agent"
    start = perf_counter()
    try:
        result = await agent.run(transcript)
        return VoiceResponse(transcript=transcript, **result.model_dump())
    finally:
        request.state.agent_duration_ms = round((perf_counter() - start) * 1000, 2)


@router.post(
    "/voice/speech",
    response_class=Response,
    responses={
        200: {"content": {"audio/mpeg": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def speech(
    body: SpeechRequest,
    request: Request,
    management: Annotated[object, Depends(get_management_client)],
    provider: Annotated[TextToSpeech, Depends(get_speaker)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> Response:
    guard.check(body.text)
    request.state.provider_stage = "speech"
    start = perf_counter()
    try:
        async with asyncio.timeout(SPEECH_TIMEOUT_SECONDS):
            audio = await provider.synthesize(
                body.text, instructions=speech_instructions(body.response_type)
            )
        if (
            not isinstance(audio, bytes)
            or len(audio) > MAX_SPEECH_BYTES
            or not has_audio_signature(audio, "mp3")
        ):
            raise InvalidAudioResponse()
        guard.check_bytes(audio)
        return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
    except TimeoutError:
        pass
    finally:
        request.state.speech_duration_ms = round((perf_counter() - start) * 1000, 2)
    raise ProviderTimeout()
