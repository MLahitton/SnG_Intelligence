"""Offline by default. --real explicitly invokes paid Responses + read-only Backend.

Measures Agent orchestration, NOT WebRTC, Live delegation, VAD or first useful audio.
Never prints business answers or provider exceptions. No result files are written.
"""

import argparse
import asyncio
import json
import logging
import math
import os
import statistics
import sys
from pathlib import Path
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import SecretStr  # noqa: E402

from app.agent.core import Agent  # noqa: E402
from app.agent.openai_model import configured_model  # noqa: E402
from app.agent.security import CredentialGuard  # noqa: E402
from app.agent.toolbox import ToolBox  # noqa: E402
from app.auth.management_guard import require_management  # noqa: E402
from app.clients.sng_control import SnGControlClient  # noqa: E402
from app.clients.transport import create_http_client  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.latency import LatencyMetrics, latency_scope, measure  # noqa: E402
from app.live.model import configured_live_agent_model  # noqa: E402
from app.tools import ReadTools  # noqa: E402

SCENARIOS = {
    "total_projects": "Â¿CuÃ¡ntos proyectos tenemos?",
    "project_status": "Â¿CÃ³mo va Smoke Test Proyecto?",
    "ambiguous_project": "Â¿CÃ³mo va Casa?",
    "management_attention": "Â¿QuÃ© deberÃ­a preocuparme hoy?",
}


class BaselineClient(SnGControlClient):
    """Benchmark-only serialization of the two reads formerly sequential in search."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._search_lock = asyncio.Lock()

    async def list_projects(self):
        async with self._search_lock:
            return await super().list_projects()

    async def list_management_tasks(self):
        async with self._search_lock:
            return await super().list_management_tasks()


def summarize(scenario, rows):
    durations = sorted(row[1] for row in rows)
    return {
        "scenario": scenario,
        "runs": len(rows),
        "p50_ms": round(statistics.median(durations), 2),
        "p95_ms": round(durations[math.ceil(len(rows) * 0.95) - 1], 2) if len(rows) >= 20 else None,
        "min_ms": round(min(durations), 2),
        "max_ms": round(max(durations), 2),
        **{
            name: statistics.mean(row[0].get(name, 0) for row in rows)
            for name in ("auth_calls", "agent_model_calls", "agent_tool_rounds", "tool_calls")
        },
    }


async def real_sample(http, token, subject, question, optimized):
    client = (SnGControlClient if optimized else BaselineClient)(http, token, uuid4())
    guard = CredentialGuard(token, SecretStr(os.environ.get("OPENAI_API_KEY", "")))
    metrics = LatencyMetrics()
    start = perf_counter()
    try:
        with latency_scope(metrics):
            if optimized:
                async with ReadTools._authorized_run(client, subject) as tools:
                    async with configured_live_agent_model() as model:
                        await Agent(model, ToolBox(tools, guard), guard, {}).run(question)
            else:
                with measure("management_revalidation_ms"):
                    identity = await require_management(client)
                    if identity.subject != subject:
                        raise RuntimeError("Authorization changed")
                async with configured_model() as model:
                    await Agent(model, ToolBox(ReadTools(client), guard), guard, {}).run(question)
        return metrics.snapshot(), (perf_counter() - start) * 1000
    finally:
        client.clear_credentials()


async def main(args):
    logging.disable(logging.CRITICAL)
    optimized = args.profile == "optimized"
    report = []
    if not args.real:
        sys.path.insert(0, str(ROOT / "tests"))
        from performance_harness import run_scenario

        for scenario in SCENARIOS:
            rows = []
            for _ in range(args.runs):
                _, metrics, elapsed = await run_scenario(scenario, optimized)
                rows.append((metrics, elapsed))
            report.append(summarize(scenario, rows))
    else:
        # Environment only; never credentials in CLI arguments or files.
        settings = Settings.from_env()
        token = SecretStr(os.environ.get("SNG_SMOKE_GOOGLE_TOKEN", ""))
        if not token.get_secret_value() or not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Missing configuration")
        async with asyncio.timeout(600):
            async with create_http_client(
                settings.sng_control_base_url, settings.sng_control_timeout_seconds
            ) as http:
                initial = SnGControlClient(http, token, uuid4())
                try:
                    subject = (await require_management(initial)).subject
                finally:
                    initial.clear_credentials()
                for scenario, question in SCENARIOS.items():
                    rows = [
                        await real_sample(http, token, subject, question, optimized)
                        for _ in range(args.runs)
                    ]
                    report.append(summarize(scenario, rows))
        del token
    print(
        json.dumps(
            {
                "mode": "real_agent_only" if args.real else "synthetic_agent_only",
                "profile": args.profile,
                "scenarios": report,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="Explicitly run paid provider requests")
    parser.add_argument("--profile", choices=["baseline", "optimized"], default="optimized")
    parser.add_argument("--runs", type=int, choices=range(1, 101), default=5, metavar="1..100")
    options = parser.parse_args()
    try:
        asyncio.run(main(options))
    except Exception:
        print(json.dumps({"status": "benchmark_failed", "details": "withheld"}))
        sys.exit(1)
