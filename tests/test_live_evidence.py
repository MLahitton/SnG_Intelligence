"""Offline projection/delegation checks; real model routing remains a manual smoke."""

import json
from uuid import UUID

import httpx
import pytest
from conftest import PROJECT_ID
from conversation_repository import MemoryRepository
from pydantic import SecretStr
from test_agent import FakeModel, call, final, run_agent
from test_conversation import turn
from test_documents import META, Model, arguments, environment
from test_live import KEY, LIVE_SETTINGS, SDP, SESSION
from test_live_provider import created
from test_memory import REFERENCE, REFS, draft
from test_quotations import quotation_path, quotations  # noqa: F401
from test_sideband import begin, complete, function, setup_live, until

from app.agent.core import Agent
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.rendering import render_answer
from app.agent.toolbox import TOOL_DEFINITIONS, ToolBox
from app.conversation.context import CONTEXT_RULE, ConversationContext
from app.conversation.memory import assemble_memory
from app.conversation.store import ConversationStore
from app.documents.provider import DocumentAnalysisResult
from app.live.prompt import DELEGATION_INSTRUCTIONS, LIVE_INSTRUCTIONS
from app.live.provider import OpenAILiveProvider, session_config

OLD = "El total es 48.609.963,176076 y la moneda no está especificada."
CURRENT = "El ítem de mayor valor es PAS01 Pasamanos, con un total de $48.609.963,18 COP."


@pytest.mark.parametrize("with_memory", [False, True])
def test_live_projection_omits_old_answers_preserves_referents_and_agent_history(with_memory):
    memory = assemble_memory(draft(), (REFERENCE,)) if with_memory else None
    context = ConversationContext(
        (turn(OLD, "¿Cuál es el proyecto más urgente?", REFS),), memory, incomplete=True
    )
    before = context.messages()
    wire = context.live_input()
    text = json.dumps(wire, ensure_ascii=False)
    assert "48.609.963" not in text and "moneda no está especificada" not in text
    assert "Casa Ximena" in text and PROJECT_ID in text
    assert "¿Cuál es el proyecto más urgente?" in text
    assert wire[0]["content"][0]["text"] == CONTEXT_RULE
    assert "Context coverage is incomplete" in text
    if memory:
        assert memory.model_dump_json(by_alias=True) in wire[1]["content"][0]["text"]
    assert context.messages() == before and OLD in [m.content for m in before]


def test_required_tool_is_scoped_to_responses_delegation_and_policy_is_explicit():
    session = session_config(LIVE_SETTINGS)
    assert session["delegation"]["type"] == "responses"
    responses = session["delegation"]["responses"]
    assert responses["tool_choice"] == "required"
    assert [t["name"] for t in responses["tools"]] == ["consult_sng_intelligence"]
    assert "tool_choice" not in session
    for text in (
        "Delegation policy:",
        "Backend tools:",
        "Generate <|SpawnThinking|> when:",
        "Do not generate <|SpawnThinking|> when:",
        "Generate <|SpawnThinking|> BEFORE",
        "¿Cuánto cuesta el ítem cinco?",
        "¿Cuál es el más caro?",
        "¿Y cuánto vale ese?",
        "Hola",
        "Gracias",
        "Eso sería todo",
        "no son evidencia actual",
    ):
        assert text in session["instructions"]
    assert "fuente exclusiva" in DELEGATION_INSTRUCTIONS
    assert "No respondas hechos empresariales de memoria" in DELEGATION_INSTRUCTIONS
    assert "spokenAnswer es la autoridad factual" in LIVE_INSTRUCTIONS


@pytest.mark.anyio
@pytest.mark.usefixtures("quotations")
@pytest.mark.parametrize("mode", ["analysis", "recommendation"])
async def test_quotation_emphasis_reads_structured_source_only(mock_backend, mode):
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "quotation"),
        final(
            "Destacaría el vidrio Templado de la cotización confirmada.",
            [f"project:{PROJECT_ID}"],
            mode=mode,
        ),
    )
    result, metrics = await run_agent(
        model,
        mock_backend[0],
        "Revisa el documento, la cotización real de Casa Ximena. ¿Sobre qué haríamos énfasis?",
    )
    assert metrics["tools_used"] == ["get_project_details", "get_commercial_quotation"]
    assert not any("/documents" in r["path"] for r in mock_backend[1])
    assert result.references[0].project_id == UUID(PROJECT_ID)
    assert "Templado" in model.inputs[-1][0][-1].content


@pytest.mark.anyio
@pytest.mark.usefixtures("quotations")
async def test_live_old_money_is_not_replayed_and_fresh_quotation_wins(mock_backend, payloads):
    commercial = payloads[quotation_path("commercial")]
    commercial["items"][0]["totalPrice"] = "48609963.18"
    commercial["items"][0]["values"]["itemNumber"] = "PAS01"
    commercial["items"][0]["values"]["system"] = "Pasamanos"
    store = ConversationStore(MemoryRepository())
    key = (await store.create("manager")).conversation_id
    async with store.operation(key, "manager"):
        await store.append(key, "manager", turn(OLD, "¿Cuánto vale Casa Ximena?", REFS))
    async with store.operation(key, "manager") as context:
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(201, json=created())

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            await OpenAILiveProvider(http, SecretStr(KEY), LIVE_SETTINGS).create(SDP, context)
        session = requests[0]["session"]
        assert "48.609.963" not in json.dumps(session["input"])
        assert "Casa Ximena" in json.dumps(session["input"])
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "quotation"),
        final(CURRENT, [f"project:{PROJECT_ID}"]),
    )
    try:
        async with setup_live(mock_backend[0], model) as (manager, socket, vault, registry, _):
            manager._conversations = store
            await store.bind_live(SESSION, key, "manager")
            begin(socket)
            socket.incoming.put_nowait(
                function(question="¿Cuál es el ítem más caro de Casa Ximena?")
            )
            complete(socket)
            await until(lambda: any(e["type"] == "response.create" for e in socket.sent))
            assert not socket.closed and registry.owned(SESSION, "manager")
            assert vault._get(SESSION).subject == "manager"
            assert OLD in repr(model.inputs[0][0])  # Agent still sees full bounded context.
            evidence = model.inputs[-1][0][-1].content
            assert json.loads(evidence)["data"]["currencyCode"] == "COP"
            assert "48609963.18" in evidence
            assert any(r["path"] == quotation_path("commercial") for r in mock_backend[1])
            saved = store.repository.turns[key][-1]
            display, spoken = render_answer(CURRENT, {"Casa Ximena"})
            assert display == CURRENT
            assert saved.assistant_content == spoken
            assert json.loads(saved.references)[0]["projectId"] == PROJECT_ID
            outputs = [
                json.loads(e["item"]["output"])
                for e in socket.sent
                if e["type"] == "response.item.create"
            ]
            assert "pesos con dieciocho centavos" in outputs[-1]["spokenAnswer"]
            assert outputs[-1]["spokenAnswer"] == spoken
            assert "176076" not in str(outputs) and "moneda no" not in str(outputs)
    finally:
        await store.shutdown()


def test_quotation_routing_is_prompt_led_and_preserves_document_boundary():
    descriptions = {t.name: t.description for t in TOOL_DEFINITIONS}
    assert "Prefer over generic document" in descriptions["get_commercial_quotation"]
    assert "structured quotation data" in descriptions["analyze_project_documents"]
    for text in (
        "Quotation Source Routing V1.2",
        "condiciones de pago escritas",
        "garantías",
        "rendimiento acústico",
        "unitPrice",
        "currencyCode",
    ):
        assert text in SYSTEM_PROMPT


@pytest.mark.anyio
async def test_written_payment_terms_still_use_document_analysis(payloads):
    question = "¿Qué condiciones de pago están escritas en el archivo de cotización?"
    answer = "El archivo de prueba indica pago anticipado."
    source = f"document:{UUID(int=1)}"
    document_model = Model(
        DocumentAnalysisResult(answer=answer, source_keys=[source], limitations=[])
    )
    async with environment(payloads, model=document_model) as (service, _, paths, tools, guard, _):
        args = arguments().model_dump(mode="json")
        args["question"] = question
        model = FakeModel(
            call("find_projects", {"query": "Proyecto"}),
            call("get_project_documents", {"project_id": PROJECT_ID}, "metadata"),
            call("analyze_project_documents", args, "analysis"),
            final(answer, kind="document_analysis", presentation=[source]),
        )
        result = await Agent(model, ToolBox(tools, guard, service), guard, {}).run(question)
        assert result.answer == answer and result.references[0].type == "document"
        assert META in paths and any(path.endswith("/file") for path in paths)
        assert len(document_model.calls) == 1 and document_model.calls[0][0] == question
        assert not any("commercial-quotation" in path for path in paths)
