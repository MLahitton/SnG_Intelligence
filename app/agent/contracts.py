from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value


class ReferenceBase(StrictModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    project_id: UUID
    label: str


class ProjectReference(ReferenceBase):
    type: Literal["project"] = "project"
    sg_code: str | None


class TaskReference(ReferenceBase):
    type: Literal["task"] = "task"
    task_id: UUID


Reference = Annotated[ProjectReference | TaskReference, Field(discriminator="type")]


class ChatResponse(StrictModel):
    answer: str
    references: list[Reference]


class ModelAnswer(StrictModel):
    answer: str = Field(min_length=1, max_length=12000)
    reference_keys: list[str] = Field(max_length=50)


class NoArguments(StrictModel):
    pass


class ProjectArguments(StrictModel):
    project_id: UUID


class SearchArguments(StrictModel):
    query: str = Field(min_length=1, max_length=200)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    arguments: type[StrictModel]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelTurn:
    answer: ModelAnswer | None = None
    calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    calls: tuple[ToolCall, ...] = ()
    call_id: str = ""


class AgentModel(Protocol):
    async def respond(
        self, messages: tuple[ModelMessage, ...], tools: tuple[ToolDefinition, ...]
    ) -> ModelTurn: ...
