"""Live-only model options. Normal chat retains its existing provider defaults."""

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import SecretStr

from app.agent.errors import AgentUnavailable, ProviderConfigurationError
from app.agent.openai_model import OpenAIModel
from app.clients.openai import openai_client

Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


def reasoning_effort(model: str, value: str) -> Effort | None:
    if not value:
        return None  # Explicit omission for models without reasoning support.
    # Closed compatibility table; unknown models must omit reasoning until evaluated.
    # Shared subset of current Live schema and SDK/model support, not a model selector.
    if model in {"gpt-6-luna", "gpt-6-sol", "gpt-6-astra"}:
        supported = {"low", "medium", "high", "xhigh"}
        if model != "gpt-6-astra":
            supported.add("none")
    else:
        supported = set()
    if value not in supported:
        raise ProviderConfigurationError()
    return cast(Effort, value)


def output_limit(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if 256 <= value <= maximum:
            return value
    except ValueError:
        pass
    raise ProviderConfigurationError()


@dataclass(frozen=True)
class LiveAgentSettings:
    model: str
    effort: Effort | None
    max_output_tokens: int

    @classmethod
    def from_env(cls) -> "LiveAgentSettings":
        model = (
            os.environ.get("SNG_INTELLIGENCE_LIVE_AGENT_MODEL", "").strip()
            or os.environ.get("SNG_INTELLIGENCE_MODEL", "").strip()
        )
        if not model:
            raise AgentUnavailable()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", model):
            raise ProviderConfigurationError()
        effort = reasoning_effort(
            model, os.environ.get("SNG_INTELLIGENCE_LIVE_AGENT_REASONING_EFFORT", "low").strip()
        )
        limit = output_limit("SNG_INTELLIGENCE_LIVE_AGENT_MAX_OUTPUT_TOKENS", 800, 3000)
        return cls(model, effort, limit)


@asynccontextmanager
async def configured_live_agent_model() -> AsyncIterator[OpenAIModel]:
    settings = LiveAgentSettings.from_env()
    key = SecretStr(os.environ.get("OPENAI_API_KEY", ""))
    if not key.get_secret_value().strip():
        raise AgentUnavailable()
    async with openai_client(key, request_timeout_seconds=30) as client:
        yield OpenAIModel(
            client,
            settings.model,
            reasoning_effort=settings.effort,
            max_output_tokens=settings.max_output_tokens,
            concise=True,
        )
