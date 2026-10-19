import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent.errors import ProviderConfigurationError

MAX_DOCUMENTS_PER_ANALYSIS = 4
# Decimal bytes, strictly below the provider's 50 MB individual-file ceiling.
MAX_PROVIDER_BYTES = 49_000_000


class DocumentSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    max_file_bytes: int = Field(default=20_000_000, gt=0, le=MAX_PROVIDER_BYTES)
    max_total_bytes: int = Field(default=30_000_000, gt=0, le=MAX_PROVIDER_BYTES)
    pdf_detail: Literal["low", "auto", "high"] = "auto"
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "omit"] = "low"
    max_output_tokens: int = Field(default=1600, ge=256, le=4000)

    @model_validator(mode="after")
    def consistent_limits(self) -> "DocumentSettings":
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("Invalid document limits")
        return self

    @classmethod
    def from_env(cls) -> "DocumentSettings":
        prefix = "SNG_INTELLIGENCE_DOCUMENT_"
        try:
            return cls.model_validate(
                {
                    "model": os.environ.get(prefix + "MODEL", "").strip()
                    or os.environ.get("SNG_INTELLIGENCE_MODEL", "").strip(),
                    "max_file_bytes": os.environ.get(prefix + "MAX_FILE_BYTES", "20000000"),
                    "max_total_bytes": os.environ.get(prefix + "MAX_TOTAL_BYTES", "30000000"),
                    "pdf_detail": os.environ.get(prefix + "PDF_DETAIL", "auto"),
                    "reasoning_effort": os.environ.get(prefix + "REASONING_EFFORT", "low"),
                    "max_output_tokens": os.environ.get(prefix + "MAX_OUTPUT_TOKENS", "1600"),
                }
            )
        except ValidationError:
            pass
        raise ProviderConfigurationError()
