from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class BackendContract(BaseModel):
    """Wire DTOs only: no business rules, persistence or unknown-field forwarding."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="ignore",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
    )
