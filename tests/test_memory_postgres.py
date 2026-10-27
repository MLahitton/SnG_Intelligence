"""Opt-in isolated PostgreSQL smoke. Never reads the application's database URL."""

import asyncio
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr
from test_conversation import turn
from test_memory import REFERENCE, REFS, FakeCompactor

from app.conversation.persistence import migrate as migration
from app.conversation.persistence.postgres import open_repository
from app.conversation.repository import ConversationBusy, ConversationForbidden
from app.conversation.store import ConversationStore
from app.core.event_loop import loop_factory

pytestmark = pytest.mark.skipif(
    not os.environ.get("SNG_TEST_POSTGRES_BIN"),
    reason="Opt-in isolated cluster: set SNG_TEST_POSTGRES_BIN to PostgreSQL bin directory",
)


def test_version_one_upgrade_memory_cas_and_restart(tmp_path, monkeypatch):
    binaries = Path(os.environ["SNG_TEST_POSTGRES_BIN"])
    cluster = (tmp_path / "cluster").resolve()
    assert cluster.parent == tmp_path.resolve()
    cluster.mkdir()
    data = cluster / "data"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    # Local synthetic database, no credentials or production configuration.
    url = SecretStr(f"postgresql://sng_test@127.0.0.1:{port}/postgres")

    def command(binary, *arguments):
        subprocess.run(
            [str(binaries / binary), *map(str, arguments)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def start():
        command(
            "pg_ctl",
            "-D",
            data,
            "-l",
            cluster / "server.log",
            "-o",
            f"-h 127.0.0.1 -p {port} -c log_statement=none",
            "-w",
            "start",
        )

    def stop():
        command("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")

    original_directory = migration.MIGRATIONS_DIR
    original_bytes = (original_directory / "001_conversations.sql").read_bytes()
    version_one = tmp_path / "version_one"
    version_one.mkdir()
    (version_one / "001_conversations.sql").write_bytes(original_bytes)
    state = {}

    async def seed_version_one():
        monkeypatch.setattr(migration, "MIGRATIONS_DIR", version_one)
        await migration.migrate(url.get_secret_value())
        async with open_repository(url) as repo:
            entry = await repo.create("synthetic-manager")
            state["key"] = entry.conversation_id
            for index in range(20):
                value = turn("Synthetic reply", "Use Casa Ximena as reference", REFS)
                await repo.append_turn(entry.conversation_id, "synthetic-manager", value, index)
            state["turns"] = await repo.list_turns(entry.conversation_id, "synthetic-manager")

    async def upgrade_and_compact():
        monkeypatch.setattr(migration, "MIGRATIONS_DIR", original_directory)
        await migration.migrate(url.get_secret_value())
        await migration.migrate(url.get_secret_value())
        async with open_repository(url) as repo:
            assert await repo.get_memory(state["key"], "synthetic-manager") is None

            async def compact(payload):
                # All connections released and no SQL transaction held across the model await.
                assert (
                    repo._pool.get_stats()["pool_available"] == repo._pool.get_stats()["pool_size"]
                )
                return await FakeCompactor()(payload)

            store = ConversationStore(repo, compact)
            async with store.operation(state["key"], "synthetic-manager") as context:
                assert context.memory.historical_referents == (REFERENCE,)
                assert len(context.turns) == 12
            saved = await repo.get_memory(state["key"], "synthetic-manager")
            state["memory"] = saved
            with pytest.raises(ConversationBusy):
                await repo.save_memory(state["key"], "synthetic-manager", saved.payload, 9, 0)
            with pytest.raises(ConversationForbidden):
                await repo.get_memory(state["key"], "another-manager")
            assert await repo.list_turns(state["key"], "synthetic-manager") == state["turns"]
            assert (await repo.get(state["key"], "synthetic-manager")).version == 20
            await store.shutdown()

    async def resumed():
        async with open_repository(url) as repo:
            store = ConversationStore(repo, FakeCompactor())
            assert (await store.resume("synthetic-manager")).conversation_id == state["key"]
            async with store.operation(state["key"], "synthetic-manager") as context:
                assert context.memory == state["memory"].payload
                assert "Casa Ximena" in str(context.messages())
            assert await repo.list_turns(state["key"], "synthetic-manager") == state["turns"]
            await store.shutdown()

    running = False
    try:
        command(
            "initdb", "-D", data, "-U", "sng_test", "--auth=trust", "--encoding=UTF8", "--no-locale"
        )
        start()
        running = True
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(seed_version_one())
            runner.run(upgrade_and_compact())
        stop()
        running = False
        start()
        running = True
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(resumed())
        assert (original_directory / "001_conversations.sql").read_bytes() == original_bytes
    finally:
        if running:
            stop()
        assert cluster.parent == tmp_path.resolve()
        shutil.rmtree(cluster)
