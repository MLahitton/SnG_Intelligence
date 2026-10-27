"""Explicit, ordered migrations: python -m app.conversation.persistence.migrate."""

import asyncio
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL

from app.conversation.repository import ConversationStorageUnavailable

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


@dataclass(frozen=True)
class Migration:
    version: int
    sql: str
    checksum: str


def migrations() -> tuple[Migration, ...]:
    result: list[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = re.fullmatch(r"(\d{3})_[a-z_]+\.sql", path.name)
        if match is None or int(match[1]) != len(result) + 1:
            raise ConversationStorageUnavailable()
        sql = path.read_text(encoding="utf-8")
        result.append(Migration(int(match[1]), sql, hashlib.sha256(sql.encode()).hexdigest()))
    if not result:
        raise ConversationStorageUnavailable()
    return tuple(result)


def validate_versions(
    rows: list[dict[str, object]], known: tuple[Migration, ...], *, complete: bool
) -> None:
    expected = [{"version": m.version, "checksum": m.checksum} for m in known]
    if len(rows) > len(known) or rows != expected[: len(rows)] or (complete and rows != expected):
        raise ConversationStorageUnavailable()


async def check_columns(
    connection: psycopg.AsyncConnection[dict[str, object]], version: int
) -> None:
    if version >= 1:
        await connection.execute(
            "SELECT c.conversation_id, c.subject, c.created_at, c.last_activity_at, c.closed_at, "
            "c.version, t.ordinal, t.modality, t.user_content, "
            "t.assistant_content, t.response_type, "
            't."references", t.created_at FROM intelligence_conversations.conversations c '
            "LEFT JOIN intelligence_conversations.turns t USING (conversation_id) LIMIT 0"
        )
    if version >= 2:
        await connection.execute(
            "SELECT conversation_id, compacted_through, revision, payload, updated_at "
            "FROM intelligence_conversations.conversation_memory LIMIT 0"
        )


async def check_schema(connection: psycopg.AsyncConnection[dict[str, object]]) -> None:
    known = migrations()
    cursor = await connection.execute(
        "SELECT version, checksum FROM intelligence_conversations.schema_version ORDER BY version"
    )
    validate_versions(await cursor.fetchall(), known, complete=True)
    await check_columns(connection, known[-1].version)


async def migrate(database_url: str) -> None:
    try:
        known = migrations()
        async with (
            await psycopg.AsyncConnection.connect(
                database_url,
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=5,
                options="-c statement_timeout=10000 -c lock_timeout=10000",
            ) as connection,
            connection.transaction(),
        ):
            await connection.execute("SELECT pg_advisory_xact_lock(794817312)")
            cursor = await connection.execute(
                "SELECT to_regclass('intelligence_conversations.schema_version') AS name"
            )
            row = await cursor.fetchone()
            applied: list[dict[str, object]] = []
            if row is not None and row["name"] is not None:
                cursor = await connection.execute(
                    "SELECT version, checksum FROM "
                    "intelligence_conversations.schema_version ORDER BY version"
                )
                applied = await cursor.fetchall()
                if not applied:
                    raise ConversationStorageUnavailable()
                validate_versions(applied, known, complete=False)
                await check_columns(connection, len(applied))
            else:
                await connection.execute("CREATE SCHEMA intelligence_conversations")
                await connection.execute(
                    "CREATE TABLE intelligence_conversations.schema_version "
                    "(version integer PRIMARY KEY, checksum text NOT NULL, "
                    "applied_at timestamptz NOT NULL DEFAULT now())"
                )
            for migration in known[len(applied) :]:
                await connection.execute(SQL(migration.sql))
                await connection.execute(
                    "INSERT INTO intelligence_conversations.schema_version "
                    "(version, checksum) VALUES (%s, %s)",
                    (migration.version, migration.checksum),
                )
            await check_columns(connection, known[-1].version)
    except (psycopg.Error, ValueError, OSError):
        raise ConversationStorageUnavailable() from None


def main() -> None:
    from app.core.config import validate_database_url
    from app.core.event_loop import loop_factory

    try:
        url = validate_database_url(os.environ.get("SNG_INTELLIGENCE_DATABASE_URL", ""))
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(migrate(url))
    except (ValueError, OSError, ConversationStorageUnavailable):
        print(
            "Conversation migration failed: check database configuration and schema.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print("Conversation schema migrations ready.")


if __name__ == "__main__":
    main()
