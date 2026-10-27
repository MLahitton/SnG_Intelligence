from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import psycopg
import pytest
from test_memory import REFERENCE, draft
from test_persistence import Pool, ScriptedConnection, metadata

from app.conversation.memory import assemble_memory
from app.conversation.persistence import migrate as migration
from app.conversation.persistence.postgres import PostgresConversationRepository
from app.conversation.repository import (
    ConversationBusy,
    ConversationForbidden,
    ConversationStorageUnavailable,
    ConversationUnavailable,
)

pytestmark = pytest.mark.anyio


def connect(monkeypatch, db):
    @asynccontextmanager
    async def connection():
        yield db

    monkeypatch.setattr(
        psycopg.AsyncConnection, "connect", AsyncMock(side_effect=lambda *a, **kw: connection())
    )


async def test_existing_version_one_applies_only_two_then_is_idempotent(monkeypatch):
    known = migration.migrations()
    rows = [{"version": m.version, "checksum": m.checksum} for m in known]
    db = ScriptedConnection(None, {"name": "schema_version"}, rows[:1], *([None] * 5))
    connect(monkeypatch, db)
    await migration.migrate("postgresql://localhost/offline")
    assert not db.results and db.commits == 1
    assert [p for q, p in db.calls if q.startswith("INSERT INTO")] == [(2, known[1].checksum)]
    assert not any(known[0].sql in q for q, _ in db.calls)
    assert any(
        known[1].sql in q or "CREATE TABLE intelligence_conversations.conversation_memory" in q
        for q, _ in db.calls
    )
    db.calls.clear()
    db.results = [None, {"name": "schema_version"}, rows, None, None, None, None]
    await migration.migrate("postgresql://localhost/offline")
    assert not any("CREATE" in q or "INSERT" in q for q, _ in db.calls)
    assert not db.results and db.commits == 2


@pytest.mark.parametrize("invalid", ["checksum1", "checksum2", "gap", "future", "empty"])
async def test_unknown_or_modified_schema_rejected_at_migration_and_startup(monkeypatch, invalid):
    rows = [{"version": m.version, "checksum": m.checksum} for m in migration.migrations()]
    if invalid.startswith("checksum"):
        rows[int(invalid[-1]) - 1]["checksum"] = "modified"
    elif invalid == "gap":
        rows = rows[1:]
    elif invalid == "future":
        rows.append({"version": 3, "checksum": "future"})
    else:
        rows = []
    db = ScriptedConnection(None, {"name": "schema_version"}, rows)
    connect(monkeypatch, db)
    with pytest.raises(ConversationStorageUnavailable):
        await migration.migrate("postgresql://localhost/offline")
    assert db.rollbacks == 1 and not any("INSERT" in q for q, _ in db.calls)
    with pytest.raises(ConversationStorageUnavailable):
        await migration.check_schema(ScriptedConnection(rows))


@pytest.mark.parametrize(
    "names",
    [
        [],
        ["002_memory.sql"],
        ["001_first.sql", "003_gap.sql"],
        ["001_a.sql", "001_b.sql"],
        ["bad.sql"],
    ],
)
def test_migration_discovery_rejects_missing_gaps_duplicates_and_names(
    tmp_path, monkeypatch, names
):
    for name in names:
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(migration, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(ConversationStorageUnavailable):
        migration.migrations()


async def test_memory_owner_checked_before_payload_or_write():
    row = await metadata()
    payload = assemble_memory(draft(), (REFERENCE,))
    db = ScriptedConnection(row, row, row)
    repository = PostgresConversationRepository(Pool(db))
    for operation in [
        repository.get_memory(row["conversation_id"], "intruder"),
        repository.save_memory(row["conversation_id"], "intruder", payload, 1, 0),
        repository.list_turn_records(
            row["conversation_id"], "intruder", after=0, through=1, limit=1
        ),
    ]:
        with pytest.raises(ConversationForbidden):
            await operation
    assert all("subject" in q and "conversation_memory" not in q for q, _ in db.calls)


async def test_memory_cas_insert_update_conflict_and_closed():
    row = {**await metadata(), "version": 30}
    key = row["conversation_id"]
    payload = assemble_memory(draft(), (REFERENCE,))
    db = ScriptedConnection(row, {"revision": 1}, row, {"revision": 2}, row, None)
    repository = PostgresConversationRepository(Pool(db))
    first = await repository.save_memory(key, "owner", payload, 8, 0)
    second = await repository.save_memory(key, "owner", payload, 16, 1)
    assert (first.revision, second.revision) == (1, 2)
    assert db.calls[1][1][:2] == (key, 8)
    assert db.calls[3][1][2:] == (key, 1, 16)
    assert "FOR UPDATE" in db.calls[0][0]
    assert "revision = %s" in db.calls[3][0] and "compacted_through < %s" in db.calls[3][0]
    with pytest.raises(ConversationBusy):
        await repository.save_memory(key, "owner", payload, 24, 1)
    db.results = [{**row, "closed_at": row["created_at"]}]
    with pytest.raises(ConversationUnavailable):
        await repository.save_memory(key, "owner", payload, 24, 2)
    assert db.commits == 2 and db.rollbacks == 2
    assert not any("UPDATE intelligence_conversations.turns" in q for q, _ in db.calls)


async def test_memory_read_and_ordinal_window_are_bounded_and_chronological():
    row = {**await metadata(), "version": 30}
    payload = assemble_memory(draft(), (REFERENCE,))
    record = dict(
        modality="text",
        user_content="Question",
        assistant_content="Answer",
        response_type="direct",
        references=[],
        created_at=row["created_at"],
    )
    db = ScriptedConnection(
        row,
        {
            "compacted_through": 8,
            "revision": 1,
            "payload": payload.model_dump(mode="json", by_alias=True),
        },
        row,
        [{**record, "ordinal": 10}, {**record, "ordinal": 9}],
    )
    repository = PostgresConversationRepository(Pool(db))
    memory = await repository.get_memory(row["conversation_id"], "owner")
    assert memory.payload == payload and memory.compacted_through == 8
    turns = await repository.list_turn_records(
        row["conversation_id"], "owner", after=8, through=30, limit=16, latest=True
    )
    assert [t.ordinal for t in turns] == [9, 10]
    assert db.calls[-1][1] == (row["conversation_id"], 8, 30, 16)
    assert "ordinal DESC LIMIT" in db.calls[-1][0]
