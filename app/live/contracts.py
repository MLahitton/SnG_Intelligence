import re
from typing import Annotated, Literal, Protocol

from pydantic import Field, field_validator

from app.agent.contracts import ChatRequest, StrictModel
from app.live.config import MAX_SDP_BYTES

OpaqueId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


def valid_sdp(value: str) -> str:
    lines = value.splitlines()
    if (
        len(value.encode("utf-8")) > MAX_SDP_BYTES
        or not lines
        or lines[0] != "v=0"
        or any(re.fullmatch(r"[a-z]=[^\x00-\x1f\x7f]*", line) is None for line in lines)
        or not all(
            any(line.startswith(prefix) for line in lines)
            for prefix in (
                "o=",
                "s=",
                "t=",
                "m=audio ",
                "m=application ",
                "a=ice-ufrag:",
                "a=ice-pwd:",
                "a=fingerprint:",
            )
        )
    ):
        raise ValueError("Invalid WebRTC audio/data SDP")
    return value


class LiveSessionRequest(StrictModel):
    sdp: str = Field(min_length=1, max_length=MAX_SDP_BYTES, repr=False)

    _sdp = field_validator("sdp")(valid_sdp)


class LiveSessionResponse(LiveSessionRequest):
    session_id: OpaqueId = Field(alias="sessionId")


class LiveCloseRequest(StrictModel):
    session_id: OpaqueId = Field(alias="sessionId")


class LiveDelegateRequest(LiveCloseRequest):
    call_id: OpaqueId = Field(alias="callId")
    question: str = Field(min_length=1, max_length=4000, repr=False)

    @field_validator("question")
    @classmethod
    def non_blank(cls, value: str) -> str:
        return ChatRequest.non_blank(value)


class LiveCloseResponse(StrictModel):
    closed: Literal[True] = True


class LiveSessionProvider(Protocol):
    async def create(self, sdp: str) -> LiveSessionResponse: ...
