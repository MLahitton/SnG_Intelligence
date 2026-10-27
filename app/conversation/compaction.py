import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from app.agent.errors import (
    InvalidModelResponse,
    ProviderTimeout,
    ProviderUnavailable,
    SensitiveContent,
)
from app.conversation.context import (
    MAX_CONTEXT_TURNS,
    ConversationContext,
    build_context,
    check_context_text,
)
from app.conversation.memory import ConversationMemoryDraft, assemble_memory, merge_referents
from app.conversation.repository import (
    ConversationBusy,
    ConversationForbidden,
    ConversationRepository,
    ConversationStorageUnavailable,
    ConversationUnavailable,
    StoredConversationMemory,
    StoredConversationTurn,
)
from app.core.latency import count

RECENT_TURNS_TARGET = 12
COMPACTION_TRIGGER = 16
COMPACTION_BATCH_TURNS = 8
MAX_COMPACTION_CHARACTERS = 48_000
MAX_COMPACTION_BYTES = 144_000
MAX_COMPACTION_SECONDS = 25
Compactor = Callable[[str], Awaitable[ConversationMemoryDraft]]
logger = logging.getLogger("sng_intelligence")


def compaction_input(
    memory: StoredConversationMemory | None, records: tuple[StoredConversationTurn, ...]
) -> str:
    for record in records:
        check_context_text(record.turn.user_content)
        check_context_text(record.turn.assistant_content)
    references = merge_referents(
        memory.payload.historical_referents if memory else (),
        tuple(record.turn.references for record in records),
    )
    payload = json.dumps(
        {
            "previousMemory": memory.payload.model_dump(mode="json", by_alias=True)
            if memory
            else None,
            "nextTurns": [
                {
                    "ordinal": record.ordinal,
                    "modality": record.turn.modality,
                    "userContent": record.turn.user_content,
                    "assistantContent": record.turn.assistant_content,
                    "responseType": record.turn.response_type,
                }
                for record in records
            ],
            "allowedHistoricalReferents": [
                ref.model_dump(mode="json", by_alias=True) for ref in references
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    check_context_text(payload)
    return payload


def failure_category(error: Exception) -> str:
    if isinstance(error, (TimeoutError, ProviderTimeout)):
        return "timeout"
    if isinstance(error, ConversationBusy):
        return "cas_conflict"
    if isinstance(error, SensitiveContent):
        return "sensitive_content"
    if isinstance(error, (InvalidModelResponse, ValueError, TypeError)):
        return "invalid_memory"
    if isinstance(error, ProviderUnavailable):
        return "provider_unavailable"
    if isinstance(error, ConversationStorageUnavailable):
        return "storage_unavailable"
    return "compaction_unavailable"


class MemoryCompaction:
    def __init__(self, repository: ConversationRepository, compactor: Compactor) -> None:
        self._repository = repository
        self._compact = compactor

    async def context(self, key: str, subject: str, version: int) -> ConversationContext:
        memory = None
        try:
            memory = await self._repository.get_memory(key, subject)
            if memory is not None and not (
                0 < memory.compacted_through <= version and memory.revision > 0
            ):
                memory = None
                raise ValueError("Invalid memory coverage")
            through = memory.compacted_through if memory else 0
            if version - through > COMPACTION_TRIGGER:
                memory = await self._update(key, subject, version, memory)
        except (ConversationUnavailable, ConversationForbidden):
            raise
        except Exception as error:
            # Optional projection failure never replaces valid memory or hides
            # transcript read failures.
            logger.info(
                "conversation_memory_compaction_failed category=%s", failure_category(error)
            )
        through = memory.compacted_through if memory else 0
        records = await self._repository.list_turn_records(
            key,
            subject,
            after=through,
            through=version,
            limit=MAX_CONTEXT_TURNS,
            latest=True,
        )
        gap = bool(records and records[0].ordinal > through + 1) or (
            not records and through < version
        )
        return build_context(
            tuple(r.turn for r in records), memory.payload if memory else None, incomplete=gap
        )

    async def _update(
        self,
        key: str,
        subject: str,
        version: int,
        previous: StoredConversationMemory | None,
    ) -> StoredConversationMemory | None:
        through = previous.compacted_through if previous else 0
        start = perf_counter()
        calls = 0
        logger.info("conversation_memory_compaction_started")
        try:
            async with asyncio.timeout(MAX_COMPACTION_SECONDS):
                records = await self._repository.list_turn_records(
                    key,
                    subject,
                    after=through,
                    through=version - RECENT_TURNS_TARGET,
                    limit=COMPACTION_BATCH_TURNS,
                )
                batch: tuple[StoredConversationTurn, ...] = ()
                payload = ""
                for record in records:
                    if record.ordinal != through + len(batch) + 1:
                        raise ValueError("Noncontiguous compaction batch")
                    candidate = compaction_input(previous, (*batch, record))
                    if (
                        len(candidate) > MAX_COMPACTION_CHARACTERS
                        or len(candidate.encode("utf-8")) > MAX_COMPACTION_BYTES
                    ):
                        break
                    batch = (*batch, record)
                    payload = candidate
                if not batch:
                    logger.info(
                        "conversation_memory_compaction_failed category=oversized_or_missing_turn"
                    )
                    return previous
                calls = 1
                count("conversation_memory_model_calls")
                draft = await self._compact(payload)
                # Validate even an injected compactor/model_construct result before persistence.
                draft = ConversationMemoryDraft.model_validate_json(
                    draft.model_dump_json(by_alias=True)
                )
                refs = merge_referents(
                    previous.payload.historical_referents if previous else (),
                    tuple(r.turn.references for r in batch),
                )
                result = assemble_memory(draft, refs)
                saved = await self._repository.save_memory(
                    key,
                    subject,
                    result,
                    batch[-1].ordinal,
                    previous.revision if previous else 0,
                )
                logger.info(
                    "conversation_memory_compaction_succeeded "
                    "turns_compacted=%d memory_characters=%d compacted_through=%d",
                    len(batch),
                    len(result.model_dump_json(by_alias=True)),
                    saved.compacted_through,
                )
                return saved
        finally:
            elapsed = (perf_counter() - start) * 1000
            count("conversation_memory_compaction_ms", elapsed)
            logger.info(
                "conversation_memory_compaction_ms=%.2f conversation_memory_model_calls=%d",
                elapsed,
                calls,
            )
