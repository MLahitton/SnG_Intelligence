"""Numeric, run-local telemetry. Never accepts business payloads or provider IDs."""

import json
import logging
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Literal

Metric = Literal[
    "conversation_memory_compaction_ms",
    "conversation_memory_model_calls",
    "document_metadata_ms",
    "document_download_ms",
    "document_analysis_provider_ms",
    "document_analysis_total_ms",
    "documents_count",
    "total_bytes",
    "conversation_context_turns",
    "conversation_context_chars",
    "management_revalidation_ms",
    "agent_total_ms",
    "agent_model_ms",
    "backend_tools_ms",
    "management_snapshot_ms",
    "comparison_fetch_ms",
    "agent_model_calls",
    "agent_tool_rounds",
    "tool_calls",
    "auth_calls",
]


@dataclass(slots=True)
class LatencyMetrics:
    values: dict[Metric, float] = field(
        default_factory=lambda: {
            "management_revalidation_ms": 0,
            "agent_total_ms": 0,
            "agent_model_ms": 0,
            "backend_tools_ms": 0,
            "agent_model_calls": 0,
            "agent_tool_rounds": 0,
            "tool_calls": 0,
            "auth_calls": 0,
        }
    )

    def add(self, name: Metric, value: float) -> None:
        self.values[name] = self.values.get(name, 0) + value

    def snapshot(self) -> dict[str, float]:
        return {key: round(value, 2) for key, value in self.values.items()}


_current: ContextVar[LatencyMetrics | None] = ContextVar("latency", default=None)


@contextmanager
def latency_scope(metrics: LatencyMetrics) -> Iterator[None]:
    token = _current.set(metrics)
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def agent_latency(destination: MutableMapping[str, object]) -> Iterator[None]:
    """Instrument HTTP runs too, preserving an existing Live/benchmark scope."""
    metrics = _current.get()
    if metrics is not None:
        try:
            yield
        finally:
            destination.update(metrics.snapshot())
    else:
        metrics = LatencyMetrics()
        with latency_scope(metrics):
            try:
                yield
            finally:
                destination.update(metrics.snapshot())


def count(name: Metric, amount: float = 1) -> None:
    if (metrics := _current.get()) is not None:
        metrics.add(name, amount)


@contextmanager
def measure(name: Metric) -> Iterator[None]:
    start = perf_counter()
    try:
        yield
    finally:
        count(name, (perf_counter() - start) * 1000)


BackendOperation = Literal[
    "auth_me",
    "management_overview",
    "management_tasks",
    "projects_list",
    "project_details",
    "project_tasks",
    "documents",
    "notifications",
    "budget_quotation",
    "commercial_quotation",
    "client_quotation_decision",
    "quotation_rounds",
    "quotation_portfolio",
]


def backend_timing(operation: BackendOperation, duration: float, status: str) -> None:
    count(
        "auth_calls" if operation == "auth_me" else "backend_tools_ms",
        1 if operation == "auth_me" else duration,
    )
    logging.getLogger("sng_intelligence").info(
        json.dumps(
            {
                "operation": operation,
                "duration_ms": round(duration, 2),
                "status_category": status,
            }
        )
    )
