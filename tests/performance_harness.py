"""Scripted offline evals: real Core/tools/contracts, synthetic Backend and model delays.

The baseline serializes search reads and rechecks auth per tool, matching Voice 2C.
This measures orchestration only, never model intelligence or actual provider latency.
"""

import asyncio
import copy
import json
from time import perf_counter
from uuid import uuid4

import httpx
from conftest import CLIENT_ID, PROJECT_ID, TOKEN, payloads
from pydantic import SecretStr
from test_agent import FakeModel, call, final

from app.agent.contracts import ModelTurn, ToolCall
from app.agent.core import Agent
from app.agent.security import CredentialGuard
from app.agent.toolbox import ToolBox
from app.auth.management_guard import require_management
from app.clients.sng_control import SnGControlClient
from app.core.latency import LatencyMetrics, latency_scope, measure
from app.tools import ReadTools

SCENARIOS = {
    "total_projects": "¿Cuántos proyectos tenemos?",
    "project_status": "¿Cómo va Smoke Test Proyecto?",
    "ambiguous_project": "¿Cómo va Casa?",
    "management_attention": "¿Qué debería preocuparme hoy?",
    "unavailable": "¿Cuánto facturamos este mes?",
}


class ScenarioModel(FakeModel):
    async def respond(self, messages, tools):
        await asyncio.sleep(0.01)
        # Final synthetic prose derives from the actual tool envelope.
        if len(self.turns) == 1 and messages[-1].role == "tool":
            data = json.loads(messages[-1].content)["data"]
            if "totalProjects" in data:
                self.turns[-1] = final(f"Tenemos {data['totalProjects']} proyectos.")
        return await super().respond(messages, tools)


async def run_scenario(scenario, optimized=True):
    data = copy.deepcopy(payloads.__wrapped__())
    project = data[f"/api/projects/{PROJECT_ID}"]
    project["name"] = "Smoke Test Proyecto"
    data["/api/projects"] = [
        project,
        {**project, "id": CLIENT_ID, "name": "Casa Ximena"},
        {**project, "id": "40000000-0000-4000-8000-000000000004", "name": "Casa Norte"},
    ]
    if scenario == "total_projects":
        turns = [call("get_management_overview"), final()]
    elif scenario == "project_status":
        turns = [
            call("find_projects", {"query": "Smoke Test Proyecto"}),
            call("get_project_details", {"project_id": PROJECT_ID}, "call_2"),
            final(
                "Smoke Test Proyecto: sin seguimiento histórico.",
                [f"project:{PROJECT_ID}"],
                kind="project_status",
                presentation=[f"project:{PROJECT_ID}"],
            ),
        ]
    elif scenario == "ambiguous_project":
        turns = [
            call("find_projects", {"query": "Casa"}),
            final(
                "Encontré Casa Ximena y Casa Norte. ¿Cuál deseas consultar?",
                [f"project:{CLIENT_ID}", "project:40000000-0000-4000-8000-000000000004"],
                kind="clarification",
            ),
        ]
    elif scenario == "management_attention":
        turns = [
            ModelTurn(
                calls=(
                    ToolCall("one", "get_management_overview", "{}"),
                    ToolCall("two", "list_management_tasks", "{}"),
                )
            ),
            final("Hay tareas que requieren revisión.", [f"project:{PROJECT_ID}"]),
        ]
    else:
        turns = [final("No dispongo de datos de facturación.", kind="unavailable")]
    model = ScenarioModel(*turns)
    lock = asyncio.Lock()

    async def respond(request):
        async def read():
            await asyncio.sleep(0.01)
            return httpx.Response(200, json=data[request.url.path])

        if not optimized and request.url.path in {"/api/projects", "/api/management/tasks"}:
            async with lock:
                return await read()
        return await read()

    guard = CredentialGuard(SecretStr(TOKEN))
    metrics = LatencyMetrics()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://control.test"
    ) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        start = perf_counter()
        try:
            with latency_scope(metrics):
                if optimized:
                    async with ReadTools._authorized_run(client, "manager") as tools:
                        result = await Agent(model, ToolBox(tools, guard), guard, {}).run(
                            SCENARIOS[scenario]
                        )
                else:
                    with measure("management_revalidation_ms"):
                        await require_management(client)
                    result = await Agent(model, ToolBox(ReadTools(client), guard), guard, {}).run(
                        SCENARIOS[scenario]
                    )
            return result, metrics.snapshot(), (perf_counter() - start) * 1000
        finally:
            client.clear_credentials()
