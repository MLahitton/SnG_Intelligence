import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import SecretStr, TypeAdapter

from app.agent.contracts import Reference
from app.conversation.context import ConversationTurn, check_context_text
from app.conversation.memory import ConversationMemory
from app.conversation.repository import (
    ConversationBusy,
    ConversationEntry,
    ConversationForbidden,
    ConversationRepository,
    ConversationStorageUnavailable,
    ConversationUnavailable,
    StoredConversationMemory,
    StoredConversationTurn,
)

Connection = psycopg.AsyncConnection[dict[str, Any]]


class PostgresConversationRepository:
    def __init__(self, pool: AsyncConnectionPool[Connection]) -> None:
        self._pool = pool

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Connection]:
        try:
            async with self._pool.connection() as connection:
                yield connection
        except (psycopg.Error, PoolTimeout, ValueError, TypeError, KeyError, OSError):
            pass
        else:
            return
        raise ConversationStorageUnavailable() from None

    async def ready(self) -> None:
        from app.conversation.persistence.migrate import check_schema

        async with self._connection() as connection:
            await check_schema(connection)

    async def _owned(
        self, connection: Connection, key: str, subject: str, *, lock: bool = False
    ) -> ConversationEntry:
        cursor = await connection.execute(
            "SELECT conversation_id, subject, created_at, last_activity_at, closed_at, version "
            "FROM intelligence_conversations.conversations WHERE conversation_id = %s"
            + (" FOR UPDATE" if lock else ""),
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ConversationUnavailable()
        if row["subject"] != subject:
            raise ConversationForbidden()
        return ConversationEntry(**row)

    async def get(self, key: str, subject: str) -> ConversationEntry:
        async with self._connection() as connection:
            return await self._owned(connection, key, subject)

    async def _current(self, subject: str, *, replace: bool) -> ConversationEntry:
        async with self._connection() as connection, connection.transaction():
            # Serializes create/resume across processes without keeping an LLM transaction open.
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (subject,)
            )
            cursor = await connection.execute(
                "SELECT conversation_id, subject, created_at, last_activity_at, closed_at, version "
                "FROM intelligence_conversations.conversations "
                "WHERE subject = %s AND closed_at IS NULL",
                (subject,),
            )
            row = await cursor.fetchone()
            if row and not replace:
                return ConversationEntry(**row)
            if row:
                await connection.execute(
                    "UPDATE intelligence_conversations.conversations SET closed_at = now() "
                    "WHERE conversation_id = %s",
                    (row["conversation_id"],),
                )
            cursor = await connection.execute(
                "INSERT INTO intelligence_conversations.conversations (conversation_id, subject) "
                "VALUES (%s, %s) RETURNING conversation_id, subject, created_at, last_activity_at, "
                "closed_at, version",
                (secrets.token_urlsafe(32), subject),
            )
            created = await cursor.fetchone()
            assert created is not None
            return ConversationEntry(**created)

    async def create(self, subject: str) -> ConversationEntry:
        return await self._current(subject, replace=True)

    async def get_current(self, subject: str) -> ConversationEntry:
        return await self._current(subject, replace=False)

    async def append_turn(
        self, key: str, subject: str, turn: ConversationTurn, expected_version: int
    ) -> int:
        for value in (turn.user_content, turn.assistant_content, turn.references):
            check_context_text(value)
        refs = TypeAdapter(list[Reference]).validate_json(turn.references)
        async with self._connection() as connection, connection.transaction():
            entry = await self._owned(connection, key, subject)
            if entry.closed_at is not None:
                raise ConversationUnavailable()
            cursor = await connection.execute(
                "UPDATE intelligence_conversations.conversations SET version = version + 1, "
                "last_activity_at = now() WHERE conversation_id = %s AND subject = %s "
                "AND closed_at IS NULL AND version = %s RETURNING version",
                (key, subject, expected_version),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ConversationBusy()
            ordinal: int = row["version"]
            await connection.execute(
                "INSERT INTO intelligence_conversations.turns (conversation_id, ordinal, modality, "
                'user_content, assistant_content, response_type, "references", created_at) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    key,
                    ordinal,
                    turn.modality,
                    turn.user_content,
                    turn.assistant_content,
                    turn.response_type,
                    Jsonb([r.model_dump(mode="json", by_alias=True) for r in refs]),
                    turn.timestamp,
                ),
            )
            return ordinal

    async def close(self, key: str, subject: str) -> None:
        async with self._connection() as connection, connection.transaction():
            await self._owned(connection, key, subject)
            await connection.execute(
                "UPDATE intelligence_conversations.conversations "
                "SET closed_at = COALESCE(closed_at, now()) "
                "WHERE conversation_id = %s AND subject = %s",
                (key, subject),
            )

    async def list_turns(
        self, key: str, subject: str, *, limit: int | None = None
    ) -> tuple[ConversationTurn, ...]:
        async with self._connection() as connection:
            await self._owned(connection, key, subject)
            cursor = await connection.execute(
                "SELECT modality, user_content, assistant_content, response_type, "
                '"references", created_at '
                "FROM intelligence_conversations.turns WHERE conversation_id = %s "
                "ORDER BY ordinal DESC LIMIT %s",
                (key, limit),
            )
            rows = await cursor.fetchall()
            result = []
            for row in reversed(rows):
                refs = TypeAdapter(list[Reference]).validate_json(json.dumps(row["references"]))
                turn = TypeAdapter(ConversationTurn).validate_python(
                    {
                        "modality": row["modality"],
                        "user_content": row["user_content"],
                        "assistant_content": row["assistant_content"],
                        "response_type": row["response_type"],
                        "references": json.dumps(
                            [r.model_dump(mode="json", by_alias=True) for r in refs]
                        ),
                        "timestamp": row["created_at"],
                    }
                )
                for value in (turn.user_content, turn.assistant_content, turn.references):
                    check_context_text(value)
                result.append(turn)
            return tuple(result)

    async def get_memory(self, key: str, subject: str) -> StoredConversationMemory | None:
        async with self._connection() as connection:
            await self._owned(connection, key, subject)
            cursor = await connection.execute(
                "SELECT compacted_through, revision, payload FROM "
                "intelligence_conversations.conversation_memory WHERE conversation_id = %s",
                (key,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return StoredConversationMemory(
                row["compacted_through"],
                row["revision"],
                ConversationMemory.model_validate_json(json.dumps(row["payload"])),
            )

    async def save_memory(
        self,
        key: str,
        subject: str,
        memory: ConversationMemory,
        compacted_through: int,
        expected_revision: int,
    ) -> StoredConversationMemory:
        memory = ConversationMemory.model_validate_json(memory.model_dump_json(by_alias=True))
        async with self._connection() as connection, connection.transaction():
            entry = await self._owned(connection, key, subject, lock=True)
            if entry.closed_at is not None:
                raise ConversationUnavailable()
            if not 0 < compacted_through <= entry.version or expected_revision < 0:
                raise ConversationBusy()
            payload = Jsonb(memory.model_dump(mode="json", by_alias=True))
            if expected_revision == 0:
                cursor = await connection.execute(
                    "INSERT INTO intelligence_conversations.conversation_memory "
                    "(conversation_id, compacted_through, revision, payload) "
                    "VALUES (%s, %s, 1, %s) "
                    "ON CONFLICT (conversation_id) DO NOTHING RETURNING revision",
                    (key, compacted_through, payload),
                )
            else:
                cursor = await connection.execute(
                    "UPDATE intelligence_conversations.conversation_memory "
                    "SET compacted_through = %s, revision = revision + 1, "
                    "payload = %s, updated_at = now() "
                    "WHERE conversation_id = %s AND revision = %s AND "
                    "compacted_through < %s RETURNING revision",
                    (compacted_through, payload, key, expected_revision, compacted_through),
                )
            row = await cursor.fetchone()
            if row is None:
                raise ConversationBusy()
            return StoredConversationMemory(compacted_through, row["revision"], memory)

    async def list_turn_records(
        self,
        key: str,
        subject: str,
        *,
        after: int,
        through: int,
        limit: int,
        latest: bool = False,
    ) -> tuple[StoredConversationTurn, ...]:
        if after < 0 or through < after or not 1 <= limit <= 16:
            raise ValueError("Invalid history window")
        async with self._connection() as connection:
            await self._owned(connection, key, subject)
            cursor = await connection.execute(
                "SELECT ordinal, modality, user_content, assistant_content, "
                'response_type, "references", created_at '
                "FROM intelligence_conversations.turns WHERE conversation_id = %s "
                "AND ordinal > %s AND ordinal <= %s ORDER BY ordinal "
                + ("DESC" if latest else "ASC")
                + " LIMIT %s",
                (key, after, through, limit),
            )
            rows = await cursor.fetchall()
            result = []
            for row in reversed(rows) if latest else rows:
                refs = TypeAdapter(list[Reference]).validate_json(json.dumps(row["references"]))
                turn = TypeAdapter(ConversationTurn).validate_python(
                    {
                        "modality": row["modality"],
                        "user_content": row["user_content"],
                        "assistant_content": row["assistant_content"],
                        "response_type": row["response_type"],
                        "references": json.dumps(
                            [r.model_dump(mode="json", by_alias=True) for r in refs]
                        ),
                        "timestamp": row["created_at"],
                    }
                )
                for value in (turn.user_content, turn.assistant_content, turn.references):
                    check_context_text(value)
                result.append(StoredConversationTurn(row["ordinal"], turn))
            return tuple(result)


@asynccontextmanager
async def open_repository(database_url: SecretStr) -> AsyncIterator[ConversationRepository]:
    # Pool background failures can include connection details. Application logs use safe categories.
    logging.getLogger("psycopg.pool").disabled = True
    pool = AsyncConnectionPool[Connection](
        database_url.get_secret_value(),
        open=False,
        min_size=1,
        max_size=5,
        timeout=5,
        max_waiting=32,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "connect_timeout": 5,
            "options": "-c statement_timeout=5000 -c lock_timeout=5000",
        },
    )
    try:
        try:
            await pool.open(wait=True, timeout=5)
        except (psycopg.Error, PoolTimeout, ValueError):
            raise ConversationStorageUnavailable() from None
        repository = PostgresConversationRepository(pool)
        await repository.ready()
        yield repository
    finally:
        await pool.close()
