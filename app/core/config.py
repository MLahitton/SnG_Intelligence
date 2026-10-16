import os
from math import isfinite
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class ConfigurationError(RuntimeError):
    """Safe startup error; never includes environment values."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    sng_control_base_url: str
    sng_control_timeout_seconds: float = 30

    @field_validator("sng_control_base_url")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or "\\" in value
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
        ):
            raise ValueError("Expected an HTTP(S) origin without credentials, path or query")
        _ = parsed.port  # Reject malformed ports.
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("HTTPS is required outside loopback development")
        return value.rstrip("/")

    @field_validator("sng_control_timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if not isfinite(value) or value <= 0:
            raise ValueError("Timeout must be finite and positive")
        return value

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            return cls.model_validate(
                {
                    "sng_control_base_url": os.environ.get("SNG_CONTROL_BASE_URL"),
                    "sng_control_timeout_seconds": os.environ.get(
                        "SNG_CONTROL_TIMEOUT_SECONDS", "30"
                    ),
                }
            )
        except ValidationError:
            pass
        raise ConfigurationError(
            "Invalid configuration: check SNG_CONTROL_BASE_URL and "
            "SNG_CONTROL_TIMEOUT_SECONDS (positive finite seconds)."
        )
