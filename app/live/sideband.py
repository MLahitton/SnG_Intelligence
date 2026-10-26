import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic, perf_counter
from typing import Protocol

from app.agent.contracts import ChatResponse
from app.agent.errors import ProviderConfigurationError, ProviderRateLimited, ProviderTimeout
from app.conversation.context import ConversationContext, ConversationTurn
from app.conversation.store import ConversationStore
from app.core.errors import IntelligenceError
from app.core.latency import LatencyMetrics, latency_scope
from app.live.credentials import LiveCredentialContext
from app.live.errors import LiveProtocolError, LiveSessionForbidden, LiveSessionUnavailable
from app.live.events import (
    Closed,
    DelegationCreated,
    FunctionCall,
    ItemDone,
    ProviderError,
    Question,
    ResponseEvent,
    ResponseLifecycle,
    parse_event,
)
from app.live.registry import LiveEntry, LiveRegistry
from app.live.sideband_transport import SidebandConnector, SidebandTransport


class AgentExecutor(Protocol):
    async def run(
        self, session_id: str, question: str, context: ConversationContext | None = None
    ) -> ChatResponse: ...


@dataclass(slots=True, repr=False)
class _Run:
    entry: LiveEntry
    request_id: str
    connector: SidebandConnector
    socket: SidebandTransport | None = None
    receiver: asyncio.Task[None] | None = None
    expiry: asyncio.Task[None] | None = None
    job: asyncio.Task[None] | None = None
    closer: asyncio.Task[None] | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    # Only current protocol metadata and a bounded pending batch, never a history.
    delegation_id: str | None = None
    response_id: str | None = None
    rounds: int = 0
    pending: list[FunctionCall] = field(default_factory=list)
    accepted: set[str] = field(default_factory=set)
    completed: bool = False
    continuing: bool = False
    closing: bool = False
    latency: LatencyMetrics = field(default_factory=LatencyMetrics)
    turn_number: int = 0
    delegation_started: float | None = None
    call_ready: float | None = None
    output_sent: float | None = None
    latency_reported: bool = False


class LiveSidebandManager:
    def __init__(
        self,
        registry: LiveRegistry,
        credentials: LiveCredentialContext,
        executor: AgentExecutor,
        *,
        conversations: ConversationStore | None = None,
        close_grace: float = 1.0,
        tool_timeout: float = 120.0,
    ) -> None:
        self._conversations = conversations
        self._registry = registry
        self._credentials = credentials
        self._executor = executor
        self._runs: dict[str, _Run] = {}
        self._closed_owners: dict[str, str] = {}
        self._close_grace = close_grace
        self._tool_timeout = tool_timeout
        self._stopped = False

    def _log(
        self, run: _Run, event: str, error: str | None = None, duration: float | None = None
    ) -> None:
        logging.getLogger("sng_intelligence").info(
            json.dumps(
                {
                    "request_id": run.request_id,
                    "live_event": event,
                    "error_type": error,
                    "live_duration_seconds": self._registry.duration(run.entry),
                    "live_delegations_count": len(run.entry.calls),
                    "agent_duration_ms": duration,
                    "turn_number": run.turn_number,
                    "response_number": run.rounds,
                    "pending_calls": len(run.pending),
                    "state": "closing" if run.closing else "active",
                }
            )
        )

    def _latency_log(self, run: _Run, status: str) -> None:
        if run.delegation_started is None or run.latency_reported:
            return
        run.latency_reported = True
        now = perf_counter()
        logging.getLogger("sng_intelligence").info(
            json.dumps(
                {
                    "operation": "live_turn",
                    "request_id": run.request_id,
                    "turn_number": run.turn_number,
                    "status_category": status,
                    **run.latency.snapshot(),
                    "delegation_to_tool_call_ready_ms": (
                        round((run.call_ready - run.delegation_started) * 1000, 2)
                        if run.call_ready is not None
                        else None
                    ),
                    "delegation_total_ms": round((now - run.delegation_started) * 1000, 2),
                    "tool_output_to_delegated_response_ms": (
                        round((now - run.output_sent) * 1000, 2)
                        if status == "completed" and run.output_sent is not None
                        else None
                    ),
                    # Live has no correlated speech-stop/first-played-audio signal.
                    "speech_end_to_delegation_ms": None,
                    "tool_output_to_live_response_ms": None,
                    "live_response_to_first_audio_ms": None,
                    "total_turn_ms": None,
                }
            )
        )

    async def attach(
        self, session_id: str, subject: str, connector: SidebandConnector, request_id: str
    ) -> None:
        if self._stopped or session_id in self._runs:
            raise LiveSessionUnavailable()
        entry = self._registry.owned(session_id, subject)
        run = _Run(entry, request_id, connector)
        self._runs[session_id] = run
        try:
            # A successful authenticated WebSocket handshake is ready; no invented
            # ack or session.start on an already running session.
            async with asyncio.timeout(max(0, entry.expires_at - monotonic())):
                run.socket = await connector.attach(session_id)
            self._credentials._get(session_id)
            if run.closing or self._stopped:
                raise LiveSessionUnavailable()
            run.receiver = asyncio.create_task(self._receive(session_id, run))
            run.expiry = asyncio.create_task(self._expire(session_id, run))
            self._log(run, "sideband_attached")
        except BaseException:
            await asyncio.shield(self._begin_close(session_id, run, "attach_failure"))
            # An attach finishing concurrently with revocation must also close its socket.
            if run.socket is not None:
                await run.socket.close()
            raise

    def _active(self, session_id: str, run: _Run) -> None:
        self._credentials._get(session_id)
        if run.closing or self._registry.owned(session_id, run.entry.subject) is not run.entry:
            raise LiveSessionUnavailable()

    async def _expire(self, session_id: str, run: _Run) -> None:
        await asyncio.sleep(max(0, run.entry.expires_at - monotonic()))
        self._begin_close(session_id, run, "expired")

    async def _receive(self, session_id: str, run: _Run) -> None:
        assert run.socket is not None
        try:
            while True:
                raw = await run.socket.receive()
                if not run.closing:
                    self._credentials.check(
                        session_id, raw.decode() if isinstance(raw, bytes) else raw
                    )
                event = parse_event(raw)
                del raw
                if isinstance(event, Closed):
                    self._log(run, "provider_session_closed")
                    run.closed.set()
                    self._begin_close(session_id, run, "session_closed")
                    return
                if run.closing:
                    continue
                self._active(session_id, run)
                if isinstance(event, ProviderError):
                    # Never log arbitrary provider codes/messages: they are untrusted text.
                    code = event.error.code
                    self._log(
                        run,
                        "provider_error",
                        code
                        if code
                        in {
                            "responses_validation_failed",
                            "rate_limit_exceeded",
                            "rate_limit_error",
                            "tool_outputs_required",
                            "function_call_outputs_required",
                            "server_error",
                        }
                        else "other_provider_error",
                    )
                    if event.error.code == "responses_validation_failed":
                        raise ProviderConfigurationError()
                    if event.error.code in {"rate_limit_exceeded", "rate_limit_error"}:
                        raise ProviderRateLimited()
                    raise LiveProtocolError()
                if isinstance(event, DelegationCreated):
                    if event.delegation.id == run.delegation_id:
                        continue
                    if run.pending or (run.job is not None and not run.job.done()):
                        raise LiveProtocolError()
                    self._latency_log(run, "superseded")
                    run.latency = LatencyMetrics()
                    run.turn_number += 1
                    run.delegation_started = perf_counter()
                    run.call_ready = run.output_sent = None
                    run.latency_reported = False
                    run.delegation_id = event.delegation.id
                    run.response_id = event.delegation.response_id
                    run.completed = False
                    self._log(run, "delegation_created")
                elif isinstance(event, ResponseEvent):
                    if (
                        run.continuing
                        and run.job is not None
                        and isinstance(event.event, ResponseLifecycle)
                        and event.event.type == "response.created"
                    ):
                        await run.job
                        if run.closing:
                            continue
                    self._response(session_id, run, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category = exc.code if isinstance(exc, IntelligenceError) else "live_sideband_malformed"
            self._begin_close(session_id, run, category)

    def _response(self, session_id: str, run: _Run, event: ResponseEvent) -> None:
        if event.delegation_id != run.delegation_id:
            raise LiveProtocolError()
        inner = event.event
        if isinstance(inner, ResponseLifecycle):
            if inner.type == "response.created":
                if inner.response.id == run.response_id:
                    return
                if run.pending or (run.job is not None and not run.job.done()):
                    raise LiveProtocolError()
                run.rounds += 1
                if run.rounds > 128:
                    raise LiveProtocolError()
                run.response_id = inner.response.id
                run.completed = False
                self._log(run, "response.created")
            else:
                if inner.response.id != run.response_id:
                    raise LiveProtocolError()
                if inner.type in {"response.failed", "response.incomplete"}:
                    self._log(run, inner.type)
                    self._latency_log(run, inner.type)
                    self._begin_close(session_id, run, inner.type.replace(".", "_"))
                    return
                if run.completed:
                    return
                run.completed = True
                self._log(run, "response.completed")
                if run.pending:
                    batch, run.pending = run.pending, []
                    run.job = asyncio.create_task(self._execute(session_id, run, batch))
                else:
                    self._latency_log(run, "completed")
        elif isinstance(inner, ItemDone):
            call = inner.item
            if call.call_id in run.accepted:
                return  # At most once for the lifetime of this session; no retry/cache.
            if (
                not run.response_id
                or run.completed
                or len(run.pending) >= 8
                or len(run.accepted) >= 64
            ):
                raise LiveProtocolError()
            Question.model_validate_json(call.arguments)
            if run.call_ready is None:
                run.call_ready = perf_counter()
            run.accepted.add(call.call_id)
            run.pending.append(call)

    async def _execute(self, session_id: str, run: _Run, calls: list[FunctionCall]) -> None:
        with latency_scope(run.latency):
            await self._execute_batch(session_id, run, calls)

    async def _execute_batch(self, session_id: str, run: _Run, calls: list[FunctionCall]) -> None:
        assert run.socket is not None
        try:
            for call in calls:
                self._active(session_id, run)
                question = Question.model_validate_json(call.arguments).question
                start = perf_counter()
                with self._registry.delegation(session_id, run.entry.subject, call.call_id):
                    async with asyncio.timeout(self._tool_timeout):
                        result = await self._delegate(session_id, run, question)
                    self._active(session_id, run)
                    output = json.dumps(
                        {
                            "responseType": result.response_type,
                            "spokenAnswer": result.spoken_answer,
                        },
                        ensure_ascii=False,
                    )
                    self._credentials.check(session_id, output)
                    await run.socket.send(
                        {
                            "type": "response.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": call.call_id,
                                "output": output,
                            },
                        }
                    )
                    run.output_sent = perf_counter()
                    self._log(run, "delegated", duration=round((perf_counter() - start) * 1000, 2))
                del result, output, question
            self._active(session_id, run)
            # Exactly one continuation after all calls in the completed response.
            run.continuing = True
            await run.socket.send({"type": "response.create"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category = (
                ProviderTimeout.code
                if isinstance(exc, TimeoutError)
                else exc.code
                if isinstance(exc, IntelligenceError)
                else "live_agent_failure"
            )
            self._begin_close(session_id, run, category)
        finally:
            run.continuing = False
            calls.clear()

    async def _delegate(self, session_id: str, run: _Run, question: str) -> ChatResponse:
        store = self._conversations
        key = store.conversation_for_live(session_id) if store is not None else None
        if store is None or key is None:
            return await self._executor.run(session_id, question)
        async with store.operation(key, run.entry.subject, live=True) as context:
            result = await self._executor.run(session_id, question, context)
            self._active(session_id, run)
            # Core guards both inputs and output. Defence in depth before retaining a turn.
            self._credentials.check(session_id, question)
            self._credentials.check(session_id, result.model_dump_json())
            store.append(
                key, run.entry.subject, ConversationTurn.completed("voice", question, result)
            )
            return result

    async def close_internal(self, session_id: str) -> None:
        run = self._runs.get(session_id)
        if run is not None:
            await asyncio.shield(self._begin_close(session_id, run, "conversation_closed"))

    def _begin_close(self, session_id: str, run: _Run, reason: str) -> asyncio.Task[None]:
        if run.closer is not None:
            return run.closer
        run.closing = True
        run.pending.clear()
        # Revoke synchronously BEFORE any await, cancellation or remote cleanup.
        self._credentials.revoke(session_id)
        self._registry.discard(session_id)
        self._closed_owners[session_id] = run.entry.subject
        if len(self._closed_owners) > 128:
            del self._closed_owners[next(iter(self._closed_owners))]
        run.closer = asyncio.create_task(self._finish_close(session_id, run, reason))
        return run.closer

    async def _finish_close(self, session_id: str, run: _Run, reason: str) -> None:
        for task in (run.job, run.expiry):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        run.pending.clear()
        self._latency_log(run, "closed")
        try:
            if run.socket is None:
                await self._remote_cleanup(session_id, run)
            elif not run.closed.is_set():
                async with asyncio.timeout(self._close_grace):
                    await run.socket.send({"type": "session.close"})
                    if run.receiver is None or run.receiver.done():
                        while not isinstance(parse_event(await run.socket.receive()), Closed):
                            pass
                        run.closed.set()
                    else:
                        await run.closed.wait()
        except Exception:
            # One bounded attach solely to close remote media if the original socket
            # died. Never resume tool execution or extend credentials on this connection.
            await self._remote_cleanup(session_id, run)
        finally:
            if run.receiver is not None:
                run.receiver.cancel()
                with suppress(asyncio.CancelledError):
                    await run.receiver
            if run.socket is not None:
                with suppress(Exception):
                    await run.socket.close()
            self._runs.pop(session_id, None)
            if self._conversations is not None:
                self._conversations.detach_live(session_id)
            self._log(run, "session_closed", reason)
            run.delegation_id = run.response_id = None
            run.accepted.clear()

    async def _remote_cleanup(self, session_id: str, run: _Run) -> None:
        socket: SidebandTransport | None = None
        try:
            async with asyncio.timeout(3):
                socket = await run.connector.attach(session_id)
                await socket.send({"type": "session.close"})
                async with asyncio.timeout(self._close_grace):
                    while not isinstance(parse_event(await socket.receive()), Closed):
                        pass
        except Exception:
            self._log(run, "remote_close_unconfirmed")
        finally:
            if socket is not None:
                with suppress(Exception):
                    await socket.close()

    async def close(self, session_id: str, subject: str) -> None:
        run = self._runs.get(session_id)
        if run is None:
            owner = self._closed_owners.get(session_id)
            if owner is None:
                raise LiveSessionUnavailable()
            if owner != subject:
                raise LiveSessionForbidden()
            return
        if run.entry.subject != subject:
            raise LiveSessionForbidden()
        await asyncio.shield(self._begin_close(session_id, run, "requested_close"))

    async def shutdown(self) -> None:
        self._stopped = True
        tasks = [self._begin_close(key, run, "shutdown") for key, run in list(self._runs.items())]
        self._credentials.clear()
        if tasks:
            await asyncio.gather(*tasks)
        self._closed_owners.clear()
