import os
import re
from dataclasses import dataclass

from app.agent.errors import ProviderConfigurationError
from app.live.errors import LiveNotConfigured
from app.live.model import Effort, output_limit, reasoning_effort

MAX_SDP_BYTES = 32 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_PROVIDER_BYTES = 128 * 1024
MAX_SESSIONS = 128
MAX_SESSIONS_PER_SUBJECT = 2
MAX_DELEGATIONS = 64
# Conservative local authorization window, not a provider session-duration setting.
MAX_LIFETIME_SECONDS = 600
BUILTIN_LIVE_VOICES = frozenset(
    "alloy ash ballad beacon bossa brise cedar cinder coral delta echo flitz gleam "
    "harema juni marin meridian nira noeul nuri quartz ripple sage shida shimmer "
    "sillage stone tempo verse vesper willow".split()
)


@dataclass(frozen=True)
class LiveSettings:
    model: str
    delegation_model: str
    voice: str
    timeout_seconds: int = 30
    lifetime_seconds: int = MAX_LIFETIME_SECONDS
    delegation_effort: Effort | None = "low"
    service_tier: str = "auto"
    delegation_max_output_tokens: int = 1024

    @classmethod
    def from_env(cls) -> "LiveSettings":
        values = [
            os.environ.get(name, "").strip()
            for name in (
                "SNG_INTELLIGENCE_LIVE_MODEL",
                "SNG_INTELLIGENCE_LIVE_DELEGATION_MODEL",
                "SNG_INTELLIGENCE_LIVE_VOICE",
            )
        ]
        if not all(values):
            raise LiveNotConfigured()
        if (
            any(re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", v) is None for v in values[:2])
            or values[2] not in BUILTIN_LIVE_VOICES
        ):
            raise ProviderConfigurationError()
        try:
            effort = reasoning_effort(
                values[1],
                os.environ.get("SNG_INTELLIGENCE_LIVE_DELEGATION_REASONING_EFFORT", "low").strip(),
            )
            tier = os.environ.get("SNG_INTELLIGENCE_LIVE_SERVICE_TIER", "auto").strip()
            if tier not in {"auto", "default", "priority"}:
                raise ProviderConfigurationError()
            limit = output_limit("SNG_INTELLIGENCE_LIVE_DELEGATION_MAX_OUTPUT_TOKENS", 1024, 4096)
            timeout = int(os.environ.get("SNG_INTELLIGENCE_LIVE_TIMEOUT_SECONDS", "30"))
            lifetime = int(os.environ.get("SNG_INTELLIGENCE_LIVE_LIFETIME_SECONDS", "600"))
            if 1 <= timeout <= 60 and 1 <= lifetime <= MAX_LIFETIME_SECONDS:
                return cls(values[0], values[1], values[2], timeout, lifetime, effort, tier, limit)
        except ValueError:
            pass
        raise ProviderConfigurationError()
