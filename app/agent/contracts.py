from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.contracts.presentation import Presentation, ResponseType


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


ConversationId = Annotated[str, Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")]


class ChatRequest(StrictModel):
    conversation_id: ConversationId | None = Field(default=None, alias="conversationId")
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
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    response_type: ResponseType
    answer: str
    spoken_answer: str
    references: list[Reference]
    presentation: Presentation

    @model_validator(mode="after")
    def matching_presentation(self) -> "ChatResponse":
        if self.presentation.kind != self.response_type:
            raise ValueError("Presentation must match response type")
        return self


class ModelAnswer(StrictModel):
    model_config = ConfigDict(populate_by_name=True)
    response_type: ResponseType = Field(alias="responseType")
    answer: str = Field(min_length=1, max_length=12000)
    spoken_answer: str = Field(min_length=1, max_length=12000, alias="spokenAnswer")
    reference_keys: list[str] = Field(max_length=50)
    presentation_keys: list[str] = Field(max_length=50)


class NoArguments(StrictModel):
    pass


class ProjectArguments(StrictModel):
    project_id: UUID


class SearchArguments(StrictModel):
    query: str = Field(min_length=1, max_length=200)


class ComparisonArguments(StrictModel):
    project_ids: list[UUID] = Field(min_length=2, max_length=4)
    include_documents: bool
    include_activity: bool

    @field_validator("project_ids")
    @classmethod
    def distinct_projects(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Comparison requires distinct projects")
        return value


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
    role: Literal["user", "assistant", "tool", "developer"]
    content: str = ""
    calls: tuple[ToolCall, ...] = ()
    call_id: str = ""


class AgentModel(Protocol):
    async def respond(
        self, messages: tuple[ModelMessage, ...], tools: tuple[ToolDefinition, ...]
    ) -> ModelTurn: ...
