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


class DocumentReference(ReferenceBase):
    type: Literal["document"] = "document"
    document_id: UUID


Reference = Annotated[
    ProjectReference | TaskReference | DocumentReference, Field(discriminator="type")
]


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


AnswerMode = Literal[
    "factual", "analysis", "recommendation", "scenario", "brainstorm", "conversation"
]


class ModelAnswer(StrictModel):
    model_config = ConfigDict(populate_by_name=True)
    answer_mode: AnswerMode = Field(alias="answerMode")
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


class DocumentSelection(StrictModel):
    project_id: UUID
    scope: Literal["current", "initial_handoff", "selected", "all_versions"]
    document_ids: list[UUID] = Field(max_length=4)
    filename: str | None = Field(max_length=250)

    @model_validator(mode="after")
    def valid_selector(self) -> "DocumentSelection":
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("Duplicate document selection")
        if self.scope == "selected":
            if bool(self.document_ids) == bool(self.filename):
                raise ValueError("Select IDs or an exact filename")
        elif self.document_ids or self.filename is not None:
            raise ValueError("Scope selects official metadata only")
        return self


class DocumentArguments(StrictModel):
    selections: list[DocumentSelection] = Field(min_length=1, max_length=4)
    question: str = Field(min_length=1, max_length=2000)
    compare_versions: bool

    @model_validator(mode="after")
    def distinct_projects(self) -> "DocumentArguments":
        if len({s.project_id for s in self.selections}) != len(self.selections):
            raise ValueError("Select each project once")
        if not self.question.strip():
            raise ValueError("Blank analysis question")
        return self


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
