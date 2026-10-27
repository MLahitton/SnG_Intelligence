"""Explicit migration command: python -m app.conversation.persistence.migrate."""

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL

from app.conversation.repository import ConversationStorageUnavailable

MIGRATION = Path(__file__).with_name("migrations") / "001_conversations.sql"


async def check_schema(connection: psycopg.AsyncConnection[dict[str, object]]) -> None:
    cursor = await connection.execute(
        "SELECT version, checksum FROM intelligence_conversations.schema_version ORDER BY version"
    )
    rows = await cursor.fetchall()
    expected = hashlib.sha256(MIGRATION.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    if rows != [{"version": 1, "checksum": expected}]:
        raise ConversationStorageUnavailable()
    await connection.execute(
        "SELECT c.conversation_id, c.subject, c.created_at, c.last_activity_at, c.closed_at, "
        "c.version, t.ordinal, t.modality, t.user_content, t.assistant_content, t.response_type, "
        't."references", t.created_at FROM intelligence_conversations.conversations c '
        "LEFT JOIN intelligence_conversations.turns t USING (conversation_id) LIMIT 0"
    )


async def migrate(database_url: str) -> None:
    try:
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
            if row is not None and row["name"] is not None:
                await check_schema(connection)
                return
            await connection.execute("CREATE SCHEMA intelligence_conversations")
            await connection.execute(
                "CREATE TABLE intelligence_conversations.schema_version "
                "(version integer PRIMARY KEY, checksum text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            migration = MIGRATION.read_text(encoding="utf-8").encode("utf-8")
            await connection.execute(SQL(migration.decode("utf-8")))
            await connection.execute(
                "INSERT INTO intelligence_conversations.schema_version "
                "(version, checksum) VALUES (1, %s)",
                (hashlib.sha256(migration).hexdigest(),),
            )
    except (psycopg.Error, ValueError, OSError):
        raise ConversationStorageUnavailable() from None


def main() -> None:
    from app.core.config import validate_database_url

    try:
        url = validate_database_url(os.environ.get("SNG_INTELLIGENCE_DATABASE_URL", ""))
        loop_factory = (
            asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
        )
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(migrate(url))
    except (ValueError, ConversationStorageUnavailable):
        print(
            "Conversation migration failed: check database configuration and schema.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print("Conversation schema version 1 ready.")


if __name__ == "__main__":
    main()
