import json
import re
from typing import Annotated, Self

from pydantic import ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.agent.contracts import Reference, StrictModel

MAX_MEMORY_CHARACTERS = 5000
MAX_MEMORY_BYTES = 4800  # Leaves room for the static rule in Live's 6000-byte input.
MAX_REFERENTS = 16
MemoryText = Annotated[str, Field(min_length=1, max_length=240)]
MemoryItems = Annotated[tuple[MemoryText, ...], Field(max_length=6)]


class ConversationMemoryDraft(StrictModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    summary: str = Field(min_length=1, max_length=1800)
    topics: MemoryItems
    user_decisions: MemoryItems
    user_assumptions: MemoryItems
    open_threads: MemoryItems
    prior_proposals: MemoryItems

    @field_validator(
        "summary", "topics", "user_decisions", "user_assumptions", "open_threads", "prior_proposals"
    )
    @classmethod
    def safe_prose(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        from app.conversation.context import check_context_text

        for text in (value,) if isinstance(value, str) else value:
            check_context_text(text)
            if not text.strip() or re.search(
                r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", text, re.I
            ):
                raise ValueError("Memory prose must not introduce identifiers")
        return value

    @model_validator(mode="after")
    def text_budget(self) -> Self:
        fields = ConversationMemoryDraft.model_fields
        text = self.model_dump_json(by_alias=True, include=set(fields))
        if len(text) > 3000 or len(text.encode("utf-8")) > 3200:
            raise ValueError("Memory prose exceeds budget")
        return self


class ConversationMemory(ConversationMemoryDraft):
    historical_referents: Annotated[tuple[Reference, ...], Field(max_length=MAX_REFERENTS)]

    @model_validator(mode="after")
    def memory_budget(self) -> Self:
        from app.conversation.context import check_context_text

        text = self.model_dump_json(by_alias=True)
        check_context_text(text)
        if len(text) > MAX_MEMORY_CHARACTERS or len(text.encode("utf-8")) > MAX_MEMORY_BYTES:
            raise ValueError("Memory exceeds budget")
        return self


def reference_key(reference: Reference) -> str:
    if reference.type == "project":
        return f"project:{reference.project_id}"
    if reference.type == "task":
        return f"task:{reference.project_id}:{reference.task_id}"
    return f"document:{reference.project_id}:{reference.document_id}"


def merge_referents(
    previous: tuple[Reference, ...], serialized: tuple[str, ...]
) -> tuple[Reference, ...]:
    ordered = {reference_key(ref): ref for ref in previous}
    for text in serialized:
        for ref in TypeAdapter(list[Reference]).validate_json(text):
            key = reference_key(ref)
            ordered.pop(key, None)
            ordered[key] = ref
    return tuple(ordered.values())[-MAX_REFERENTS:]


def assemble_memory(
    draft: ConversationMemoryDraft, references: tuple[Reference, ...]
) -> ConversationMemory:
    # Remove only whole oldest references if official labels exhaust the aggregate budget.
    data = draft.model_dump(by_alias=True)
    while True:
        candidate = {**data, "historicalReferents": references}
        text = json.dumps(
            candidate,
            default=lambda ref: ref.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(text) <= MAX_MEMORY_CHARACTERS and len(text.encode("utf-8")) <= MAX_MEMORY_BYTES:
            return ConversationMemory.model_validate_json(text)
        if not references:
            raise ValueError("Memory exceeds budget")
        references = references[1:]
