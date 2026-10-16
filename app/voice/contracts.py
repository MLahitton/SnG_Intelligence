from dataclasses import dataclass, field
from typing import Protocol

from pydantic import Field, field_validator

from app.agent.contracts import ChatResponse, StrictModel
from app.contracts.presentation import ResponseType
from app.voice.config import MAX_SPEECH_CHARACTERS, MAX_TRANSCRIPT_CHARACTERS


@dataclass(frozen=True)
class AudioInput:
    content: bytes = field(repr=False)
    extension: str
    content_type: str


class SpeechToText(Protocol):
    async def transcribe(self, audio: AudioInput) -> str: ...


class TextToSpeech(Protocol):
    async def synthesize(self, text: str, *, instructions: str) -> bytes: ...


class SpeechRequest(StrictModel):
    text: str = Field(min_length=1, max_length=MAX_SPEECH_CHARACTERS)
    response_type: ResponseType = Field(default="direct", alias="responseType")

    @field_validator("text")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text must not be blank")
        return value


class TranscriptionResponse(StrictModel):
    transcript: str = Field(min_length=1, max_length=MAX_TRANSCRIPT_CHARACTERS)


class VoiceResponse(ChatResponse):
    transcript: str = Field(min_length=1, max_length=MAX_TRANSCRIPT_CHARACTERS)
