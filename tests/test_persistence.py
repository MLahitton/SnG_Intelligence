import asyncio
import traceback
from contextlib import asynccontextmanager
from dataclasses import asdict
from unittest.mock import AsyncMock

import psycopg
import pytest
from conversation_repository import MemoryRepository
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from test_agent import FakeModel, call, final
from test_chat import chat_app
from test_conversation import BASE, turn
from test_live import AUTH

from app.conversation.persistence import migrate as migration
from app.conversation.persistence.postgres import PostgresConversationRepository, open_repository
from app.conversation.repository import (
    ConversationBusy,
    ConversationForbidden,
    ConversationStorageUnavailable,
    ConversationUnavailable,
)
from app.conversation.store import ConversationStore
from app.core.config import ConfigurationError, Settings

pytestmark = pytest.mark.anyio


async def test_resume_atomic_new_archives_and_owner_isolation():
    repo = MemoryRepository()
    stores = [ConversationStore(repo) for _ in range(12)]
    entries = await asyncio.gather(*(s.resume("owner") for s in stores))
    key = entries[0].conversation_id
    assert {e.conversation_id for e in entries} == {key}
    assert (await stores[0].resume("other")).conversation_id != key
    for method, args in [
        (repo.get, (key, "other")),
        (repo.list_turns, (key, "other")),
        (repo.append_turn, (key, "other", turn(), 0)),
        (repo.close, (key, "other")),
    ]:
        with pytest.raises(ConversationForbidden):
            await method(*args)
    async with stores[0].operation(key, "owner"):
        await stores[0].append(key, "owner", turn())
    new = await stores[0].create("owner")
    assert new.conversation_id != key
    assert (await repo.get(key, "owner")).closed_at is not None
    assert len(await repo.list_turns(key, "owner")) == 1
    assert await repo.list_turns(new.conversation_id, "owner") == ()


async def test_cross_runtime_version_rejects_stale_append_without_duplicates():
    repo = MemoryRepository()
    first, second = ConversationStore(repo), ConversationStore(repo)
    key = (await first.resume("owner")).conversation_id
    async with first.operation(key, "owner"):
        async with second.operation(key, "owner"):
            await first.append(key, "owner", turn("first"))
            with pytest.raises(ConversationBusy):
                await second.append(key, "owner", turn("stale"))
    assert [t.assistant_content for t in await repo.list_turns(key, "owner")] == ["first"]
    assert (await repo.get(key, "owner")).version == 1


def test_new_application_resumes_same_history_with_fresh_backend_reads(settings, mock_backend):
    transport, requests = mock_backend
    first_model = FakeModel(
        call("get_management_overview"),
        final("Tenemos 19 proyectos."),
        call("get_management_overview"),
        final("Siguen siendo 19."),
    )
    with TestClient(chat_app(settings, transport, first_model)) as api:
        response = api.post(BASE + "/resume", json={}, headers=AUTH)
        assert response.status_code == 200
        assert set(response.json()) == {"conversationId", "createdAt", "lastActivityAt"}
        key = response.json()["conversationId"]
        for message in ["Cuantos proyectos?", "Y ahora?"]:
            assert (
                api.post(
                    "/api/intelligence/chat",
                    json={"message": message, "conversationId": key},
                    headers=AUTH,
                ).status_code
                == 200
            )
    model = FakeModel(call("get_management_overview"), final("Tenemos 19 proyectos ahora."))
    with TestClient(chat_app(settings, transport, model)) as api:
        assert api.post(BASE + "/resume", json={}, headers=AUTH).json()["conversationId"] == key
        assert (
            api.post(
                "/api/intelligence/chat",
                json={"message": "Y ahora?", "conversationId": key},
                headers=AUTH,
            ).status_code
            == 200
        )
        assert len(api.app.state.conversations.repository.turns[key]) == 3
        assert "Siguen siendo 19." in repr(model.inputs[0][0])
    assert sum(r["path"] == "/api/management/overview" for r in requests) == 3


def test_append_failure_never_returns_success(
    settings, mock_backend, conversation_repository, monkeypatch
):
    transport, _ = mock_backend
    model = FakeModel(final("Hola", mode="conversation"))
    with TestClient(chat_app(settings, transport, model)) as api:
        key = api.post(BASE + "/resume", json={}, headers=AUTH).json()["conversationId"]
        monkeypatch.setattr(
            conversation_repository,
            "append_turn",
            AsyncMock(side_effect=ConversationStorageUnavailable()),
        )
        response = api.post(
            "/api/intelligence/chat", json={"message": "Hola", "conversationId": key}, headers=AUTH
        )
        assert (
            response.status_code == 503
            and response.json()["error"] == "conversation_storage_unavailable"
        )
        assert "Safe answer" not in response.text and not conversation_repository.turns[key]


def test_readiness_database_failure_does_not_change_liveness(
    settings, mock_backend, conversation_repository, monkeypatch
):
    transport, _ = mock_backend
    with TestClient(chat_app(settings, transport, FakeModel())) as api:
        assert api.get("/readiness").status_code == 200
        monkeypatch.setattr(
            conversation_repository,
            "ready",
            AsyncMock(side_effect=ConversationStorageUnavailable()),
        )
        assert api.get("/health").json() == {"status": "ok"}
        assert api.get("/readiness").status_code == 503


@pytest.mark.parametrize(
    "url",
    [
        "",
        "sqlite:///local",
        "postgresql://",
        "postgresql://localhost",
        "postgresql://localhost/db#secret",
        "postgresql://localhost:bad/db",
    ],
)
def test_database_config_invalid_and_secret_safe(url, monkeypatch):
    monkeypatch.setenv("SNG_CONTROL_BASE_URL", "https://control.test")
    monkeypatch.setenv("SNG_INTELLIGENCE_DATABASE_URL", url)
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env()
    assert error.value.__context__ is None
    with pytest.raises(ValidationError):
        Settings(sng_control_base_url="https://control.test", database_url=url)


def test_database_secret_not_serialized(settings):
    url = settings.database_url.get_secret_value()
    assert url not in repr(settings) + settings.model_dump_json()
    assert isinstance(settings.database_url, SecretStr)


class ScriptedConnection:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.in_transaction = False

    async def execute(self, query, params=None):
        self.calls.append((str(query), params))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        cursor = AsyncMock()
        cursor.fetchone.return_value = result
        cursor.fetchall.return_value = result
        return cursor

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1
        finally:
            self.in_transaction = False


class Pool:
    def __init__(self, connection):
        self.db = connection
        self.open = AsyncMock()
        self.close = AsyncMock()

    @asynccontextmanager
    async def connection(self):
        yield self.db


async def metadata():
    return asdict(await MemoryRepository().create("owner"))


async def test_postgres_append_parameters_atomic_ordinal_and_rollback():
    row = await metadata()
    key = row["conversation_id"]
    db = ScriptedConnection(row, {"version": 1}, None)
    repository = PostgresConversationRepository(Pool(db))
    assert await repository.append_turn(key, "owner", turn(), 0) == 1
    assert db.commits == 1
    assert db.calls[1][1] == (key, "owner", 0)
    assert "closed_at IS NULL AND version = %s" in db.calls[1][0]
    assert db.calls[2][1][:2] == (key, 1)
    assert len(db.calls[2][1]) == 8
    assert db.calls[2][1][6].obj == []
    db.results = [row, {"version": 2}, psycopg.OperationalError("private-dsn")]
    with pytest.raises(ConversationStorageUnavailable) as error:
        await repository.append_turn(key, "owner", turn(), 1)
    assert "private-dsn" not in "".join(traceback.format_exception(error.value))
    assert db.rollbacks == 1
    db.results = [row, None]
    with pytest.raises(ConversationBusy):
        await repository.append_turn(key, "owner", turn(), 0)
    assert db.rollbacks == 2


async def test_postgres_resume_locks_subject_and_new_archives_without_deleting_history():
    row = await metadata()
    db = ScriptedConnection(None, row)
    repository = PostgresConversationRepository(Pool(db))
    assert (await repository.get_current("owner")).conversation_id == row["conversation_id"]
    assert "pg_advisory_xact_lock" in db.calls[0][0] and db.calls[0][1] == ("owner",)
    new = {**row, "conversation_id": "n" * 43}
    db.results = [None, row, None, new]
    assert (await repository.create("owner")).conversation_id == new["conversation_id"]
    assert "SET closed_at = now()" in db.calls[-2][0]
    assert not any("DELETE" in q for q, _ in db.calls)
    assert db.commits == 2


async def test_postgres_closed_foreign_missing_and_history_projection():
    row = await metadata()
    db = ScriptedConnection(None, row, {**row, "closed_at": row["created_at"]})
    repository = PostgresConversationRepository(Pool(db))
    with pytest.raises(ConversationUnavailable):
        await repository.get("x" * 43, "owner")
    with pytest.raises(ConversationForbidden):
        await repository.list_turns(row["conversation_id"], "other")
    with pytest.raises(ConversationUnavailable):
        await repository.append_turn(row["conversation_id"], "owner", turn(), 0)
    history = dict(
        modality="voice",
        user_content="question",
        assistant_content="answer",
        response_type="direct",
        references=[],
        created_at=row["created_at"],
    )
    db.results = [row, [{**history, "assistant_content": "new"}, history]]
    turns = await repository.list_turns(row["conversation_id"], "owner", limit=16)
    assert [t.assistant_content for t in turns] == ["answer", "new"]
    assert db.calls[-1][1] == (row["conversation_id"], 16)
    db.results = [row, [{**history, "response_type": "invented"}]]
    with pytest.raises(ConversationStorageUnavailable):
        await repository.list_turns(row["conversation_id"], "owner")


@pytest.mark.parametrize(
    "rows", [[], [{"version": 1, "checksum": "wrong"}], [{"version": 2, "checksum": "future"}]]
)
async def test_schema_mismatch_fails_startup_safely_and_closes_pool(monkeypatch, rows):
    pool = Pool(ScriptedConnection(rows))
    from app.conversation.persistence import postgres

    class PoolFactory:
        def __class_getitem__(cls, item):
            return lambda *args, **kwargs: pool

    monkeypatch.setattr(postgres, "AsyncConnectionPool", PoolFactory)
    with pytest.raises(ConversationStorageUnavailable):
        async with open_repository(SecretStr("postgresql://private:secret@localhost/db")):
            pytest.fail("incompatible schema cannot start")
    pool.open.assert_awaited_once()
    pool.close.assert_awaited_once()


async def test_migration_explicit_version_checksum_and_repeat_validation(monkeypatch):
    known = migration.migrations()
    rows = [{"version": m.version, "checksum": m.checksum} for m in known]
    db = ScriptedConnection(None, {"name": None}, *([None] * 8))

    @asynccontextmanager
    async def connection():
        yield db

    monkeypatch.setattr(
        psycopg.AsyncConnection, "connect", AsyncMock(side_effect=lambda *a, **kw: connection())
    )
    await migration.migrate("postgresql://localhost/offline")
    assert db.commits == 1
    inserts = [params for query, params in db.calls if query.startswith("INSERT INTO")]
    assert inserts == [(m.version, m.checksum) for m in known]
    db.calls.clear()
    db.results = [None, {"name": "schema_version"}, rows, None, None, None, None]
    await migration.migrate("postgresql://localhost/offline")
    assert db.commits == 2 and not any("CREATE" in q or "INSERT" in q for q, _ in db.calls)


def test_selector_loop_factory_matches_uvicorn():
    import sys

    import uvicorn

    config = uvicorn.Config("app.main:app", loop="app.core.event_loop:loop_factory")
    factory = config.get_loop_factory()
    loop = factory()
    try:
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


async def test_live_database_append_failure_suppresses_output_and_preserves_history(
    mock_backend, monkeypatch
):
    from test_live import SESSION
    from test_sideband import begin, complete, function, setup_live, until

    transport, _ = mock_backend
    repository = MemoryRepository()
    store = ConversationStore(repository)
    entry = await store.resume("manager")
    async with setup_live(transport) as (manager, socket, vault, registry, model):
        manager._conversations = store
        store.close_live = manager.close_internal
        await store.bind_live(SESSION, entry.conversation_id, "manager")
        monkeypatch.setattr(
            repository, "append_turn", AsyncMock(side_effect=ConversationStorageUnavailable())
        )
        begin(socket)
        socket.incoming.put_nowait(function())
        complete(socket)
        await until(lambda: socket.closed)
        assert not repository.turns[entry.conversation_id]
        assert (await repository.get(entry.conversation_id, "manager")).closed_at is None
        assert not any(
            e["type"] in {"response.item.create", "response.create"} for e in socket.sent
        )
        assert not vault._LiveCredentialContext__items and not registry._entries
    await store.shutdown()


async def test_close_after_commit_does_not_invite_stale_id_retry():
    repository = MemoryRepository()
    store = ConversationStore(repository)
    key = (await store.resume("owner")).conversation_id
    with pytest.raises(ConversationBusy):
        async with store.operation(key, "owner"):
            await store.append(key, "owner", turn())
            await repository.close(key, "owner")
    assert len(await repository.list_turns(key, "owner")) == 1
    await store.shutdown()
