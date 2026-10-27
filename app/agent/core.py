import asyncio
import json
import logging
import re
from collections.abc import MutableMapping

from app.agent.contracts import AgentModel, ChatResponse, ModelMessage, ModelTurn
from app.agent.errors import (
    AgentLimitExceeded,
    InvalidModelResponse,
    InvalidToolCall,
    UngroundedReference,
)
from app.agent.prompt import GROUNDING_REPAIR_INSTRUCTION
from app.agent.rendering import render_answer
from app.agent.security import CredentialGuard
from app.agent.toolbox import TOOL_DEFINITIONS, ToolBox
from app.contracts.presentation import (
    ComparisonPresentation,
    DocumentAnalysisPresentation,
    ProjectPresentation,
)
from app.conversation.context import ConversationContext, check_context_text
from app.core.latency import agent_latency, count, measure

MAX_TOOL_ROUNDS = 6
MAX_TOOL_CALLS = 8
MAX_TRANSCRIPT_BYTES = 128_000
MAX_EXECUTION_SECONDS = 120


class Agent:
    def __init__(
        self,
        model: AgentModel,
        tools: ToolBox,
        guard: CredentialGuard,
        metrics: MutableMapping[str, object],
    ) -> None:
        self._model = model
        self._tools = tools
        self._guard = guard
        self._metrics = metrics

    async def run(self, message: str, context: ConversationContext | None = None) -> ChatResponse:
        try:
            async with asyncio.timeout(MAX_EXECUTION_SECONDS):
                with agent_latency(self._metrics), measure("agent_total_ms"):
                    return await self._run(message, context)
        except TimeoutError:
            pass
        raise AgentLimitExceeded()

    async def _run(self, message: str, context: ConversationContext | None = None) -> ChatResponse:
        self._guard.check(message)
        if context is not None:
            check_context_text(message)
        messages = list(context.messages()) if context is not None else []
        for previous in messages:
            self._guard.check(previous.content)
        if context is not None:
            self._metrics.update(
                conversation_context_turns=len(context.turns),
                conversation_context_chars=context.characters,
            )
        count("conversation_context_turns", len(context.turns) if context else 0)
        count("conversation_context_chars", context.characters if context else 0)
        messages.append(ModelMessage(role="user", content=message))
        used_ids: set[str] = set()
        names: list[str] = []
        self._metrics.update(tool_calls=0, tools_used=names)
        size = sum(len(item.content.encode("utf-8")) for item in messages)
        if size > MAX_TRANSCRIPT_BYTES:
            raise AgentLimitExceeded()
        for round_number in range(MAX_TOOL_ROUNDS + 1):
            count("agent_model_calls")
            with measure("agent_model_ms"):
                turn = await self._model.respond(tuple(messages), TOOL_DEFINITIONS)
            if turn.answer is not None:
                try:
                    return self._finalize(turn, context, bool(names))
                except UngroundedReference:
                    return await self._repair(
                        turn, messages, context, bool(names), size, round_number + 2
                    )
            if not turn.calls:
                raise InvalidModelResponse()
            if round_number == MAX_TOOL_ROUNDS or len(names) + len(turn.calls) > MAX_TOOL_CALLS:
                raise AgentLimitExceeded()
            # Validate the entire batch before executing any call in it.
            for call in turn.calls:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", call.id) or call.id in used_ids:
                    raise InvalidToolCall()
                used_ids.add(call.id)
                self._guard.check(call.id)
                self._guard.check(call.name)
                self._tools.validate(call)
            count("agent_tool_rounds")
            messages.append(ModelMessage(role="assistant", calls=turn.calls))
            for call in turn.calls:
                count("tool_calls")
                names.append(call.name)
                self._metrics["tool_calls"] = len(names)
                result = await self._tools.execute(call)
                size += len(result.encode("utf-8")) + len(call.arguments.encode("utf-8"))
                if size > MAX_TRANSCRIPT_BYTES:
                    raise AgentLimitExceeded()
                messages.append(ModelMessage(role="tool", content=result, call_id=call.id))
        raise AgentLimitExceeded()

    def _finalize(
        self, turn: ModelTurn, context: ConversationContext | None, has_reads: bool
    ) -> ChatResponse:
        if turn.answer is None:
            raise InvalidModelResponse()
        if turn.calls or not turn.answer.answer.strip():
            raise InvalidModelResponse()
        self._guard.check(turn.answer.model_dump_json())
        # Historical prose can never substitute for a fresh read. Safe clarification
        # and unavailable responses can still ask for information without inventing it.
        if (
            context
            and context.has_history
            and not has_reads
            and turn.answer.response_type not in ("clarification", "unavailable")
        ):
            raise InvalidModelResponse()
        # Spoken prose is not an independent factual channel. Reject divergence,
        # then render both surfaces from the same content and evidence.
        if turn.answer.spoken_answer != turn.answer.answer:
            raise InvalidModelResponse()
        if self._tools.quotations_unavailable and turn.answer.response_type not in (
            "unavailable",
            "clarification",
        ):
            # A missing quotation must not become a factual task-count fallback.
            raise InvalidModelResponse()
        if (
            self._tools.document_response_type is not None
            and turn.answer.response_type != self._tools.document_response_type
        ):
            raise InvalidModelResponse()
        if (
            self._tools.document_choices
            and set(turn.answer.presentation_keys) != self._tools.document_choices
        ):
            raise InvalidModelResponse()
        presentation = self._tools.presentation.build(
            turn.answer.response_type, turn.answer.presentation_keys
        )
        content = turn.answer.answer
        if isinstance(presentation, DocumentAnalysisPresentation):
            if self._tools.document_answer is None:
                raise InvalidModelResponse()
            content = self._tools.document_answer
        if isinstance(presentation, ProjectPresentation):
            name = presentation.project.project_name
            if not content.startswith(name):
                content = f"{name}: {content}"
        answer, spoken = render_answer(content, self._tools.presentation.names)
        reference_keys = turn.answer.reference_keys
        if isinstance(presentation, ComparisonPresentation) and presentation.projects:
            # Preserve the complete compared order for cross-modal ordinals,
            # even if the model selects references in a different order or omits one.
            reference_keys = [
                f"project:{item.project.project_id}" for item in presentation.projects
            ] + reference_keys
        if isinstance(presentation, DocumentAnalysisPresentation):
            reference_keys = (
                [f"document:{s.document_id}" for s in presentation.sources]
                + [
                    f"project:{p.project_id}"
                    for p in [presentation.project, *presentation.related_projects]
                ]
                + reference_keys
            )
        response = ChatResponse(
            response_type=turn.answer.response_type,
            answer=answer,
            spoken_answer=spoken,
            references=self._tools.references(reference_keys),
            presentation=presentation,
        )
        self._guard.check(response.model_dump_json())
        return response

    async def _repair(
        self,
        initial: ModelTurn,
        messages: list[ModelMessage],
        context: ConversationContext | None,
        has_reads: bool,
        size: int,
        model_call_number: int,
    ) -> ChatResponse:
        assert initial.answer is not None
        reference_count = len(initial.answer.reference_keys)
        self._repair_log("grounding_repair_started", model_call_number, reference_count)
        try:
            previous = initial.answer.model_dump_json()
            self._guard.check(previous)
            size += len(previous.encode("utf-8")) + len(
                GROUNDING_REPAIR_INSTRUCTION.encode("utf-8")
            )
            if size > MAX_TRANSCRIPT_BYTES:
                raise AgentLimitExceeded()
            repair_messages = (
                *messages,
                ModelMessage(role="assistant", content=previous),
                ModelMessage(role="developer", content=GROUNDING_REPAIR_INSTRUCTION),
            )
            count("agent_model_calls")
            with measure("agent_model_ms"):
                repaired = await self._model.respond(repair_messages, ())
            if repaired.calls or repaired.answer is None:
                raise InvalidModelResponse()
            serialized = repaired.answer.model_dump_json()
            self._guard.check(serialized)
            if size + len(serialized.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
                raise AgentLimitExceeded()
            # Repair metadata only: even a valid new fact must not enter through this path.
            if repaired.answer.answer != initial.answer.answer:
                raise InvalidModelResponse()
            result = self._finalize(repaired, context, has_reads)
        except BaseException:
            self._repair_log("grounding_repair_failed", model_call_number, reference_count)
            raise
        self._repair_log(
            "grounding_repair_succeeded", model_call_number, len(repaired.answer.reference_keys)
        )
        return result

    def _repair_log(self, event: str, model_call_number: int, reference_count: int) -> None:
        logging.getLogger("sng_intelligence").info(
            json.dumps(
                {
                    "agent_event": event,
                    "model_call_number": model_call_number,
                    "reference_count": reference_count,
                    "available_reference_count": self._tools.available_reference_count,
                }
            )
        )
