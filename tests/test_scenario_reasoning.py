"""Offline scripted flows; not a semantic classifier or real-provider quality eval."""

import json
from uuid import UUID

import httpx
import pytest
from conftest import DATE, PROJECT_ID
from conversation_repository import MemoryRepository
from test_agent import FakeModel, call, final
from test_decision_support import with_context
from test_documents import META, Model, arguments, document, environment
from test_live import SESSION
from test_memory import REFERENCE, draft
from test_sideband import begin, complete, function, setup_live, until

from app.agent.core import Agent
from app.agent.errors import InvalidModelResponse
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.toolbox import TOOL_DEFINITIONS, ToolBox
from app.conversation.context import ConversationContext
from app.conversation.memory import assemble_memory
from app.conversation.store import ConversationStore
from app.documents.provider import DocumentAnalysisResult

pytestmark = pytest.mark.anyio

SCENARIO = (
    "La tarea actual es presentar la cotización al cliente. Bajo ese supuesto, una demora "
    "podría prolongar la etapa comercial y retrasar la decisión. No tengo un plazo formal "
    "de respuesta ni dependencias suficientes para cuantificar el impacto posterior."
)
NO_QUANTIFICATION = (
    "Bajo el supuesto de Producción +3 días, podría haber efectos posteriores, pero los datos "
    "actuales solo muestran la tarea comercial. No puedo cuantificar el impacto en Logística "
    "ni confirmar una dependencia formal."
)
INVALID_CAUSAL_FIXTURE = "Logística se moverá exactamente 3 días"


def current_project(payloads):
    payloads[f"/api/projects/{PROJECT_ID}"]["name"] = "Casa Ximena"
    payloads[f"/api/projects/{PROJECT_ID}/tasks"][0].update(
        area="Commercial",
        type="PresentQuotationToClient",
        status="InProgress",
        startedAt=DATE,
        leadTimeStatus="OnTrack",
    )


def history():
    return ConversationContext(
        memory=assemble_memory(
            draft(
                summary="Históricamente Casa Ximena estaba esperando al cliente.",
                userAssumptions=("Si el cliente tarda tres días más en responder.",),
            ),
            (REFERENCE,),
        )
    )


@pytest.mark.parametrize(
    "question",
    [
        "¿Qué pasa si el cliente se demora tres días?",
        "¿Qué pasa si el cliente se demora más días en responder de lo que debería?",
        "¿Y si no logramos presentar hoy la cotización?",
        "¿Y si sigue tardando?",
    ],
)
@pytest.mark.parametrize("document_state", ["too_large", "unavailable"])
async def test_operational_scenario_completes_without_document_reads(
    payloads, question, document_state
):
    current_project(payloads)

    def unavailable(request, _):
        if document_state == "unavailable" and "/documents" in request.url.path:
            return httpx.Response(404)
        return None

    docs = [document(sizeBytes=21_000_000)] if document_state == "too_large" else []
    async with environment(payloads, docs, handler=unavailable) as (
        service,
        document_model,
        paths,
        tools,
        guard,
        _,
    ):
        scripted_final = final(SCENARIO, [f"project:{PROJECT_ID}"], mode="scenario")
        model = FakeModel(call("get_project_details", {"project_id": PROJECT_ID}), scripted_final)
        metrics = {}
        result = await Agent(model, ToolBox(tools, guard, service), guard, metrics).run(
            question, history()
        )
        assert scripted_final.answer.answer_mode == "scenario"
        assert metrics["tools_used"] == ["get_project_details"]
        assert paths.count(f"/api/projects/{PROJECT_ID}") == 1
        assert paths.count(f"/api/projects/{PROJECT_ID}/tasks") == 1
        assert not any("/documents" in path for path in paths) and not document_model.calls
        assert result.answer == SCENARIO and result.response_type == "direct"
        assert result.references[0].project_id == UUID(PROJECT_ID)
        assert "answerMode" not in result.model_dump_json(by_alias=True)
        assert "Históricamente" in repr(model.inputs[0][0])
        evidence = model.inputs[-1][0][-1].content
        assert "PresentQuotationToClient" in evidence and "InProgress" in evidence
        assert "Bajo ese supuesto" in result.answer and "cuantificar" in result.answer


async def test_contract_question_still_reads_metadata_and_content(payloads):
    question = "¿Qué plazo de respuesta establece el contrato?"
    document_answer = "El contrato de prueba establece cinco días para responder."
    document_model = Model(
        DocumentAnalysisResult(
            answer=document_answer, source_keys=[f"document:{UUID(int=1)}"], limitations=[]
        )
    )
    async with environment(payloads, model=document_model) as (service, _, paths, tools, guard, _):
        args = arguments().model_dump(mode="json")
        args["question"] = question
        model = FakeModel(
            call("find_projects", {"query": "Proyecto"}),
            call("get_project_documents", {"project_id": PROJECT_ID}, "metadata"),
            call("analyze_project_documents", args, "analysis"),
            final(
                document_answer, kind="document_analysis", presentation=[f"document:{UUID(int=1)}"]
            ),
        )
        metrics = {}
        result = await Agent(model, ToolBox(tools, guard, service), guard, metrics).run(question)
        assert metrics["tools_used"] == [
            "find_projects",
            "get_project_documents",
            "analyze_project_documents",
        ]
        assert META in paths and any(path.endswith("/file") for path in paths)
        assert len(document_model.calls) == 1
        assert document_model.calls[0][0] == question
        assert result.response_type == "document_analysis" and result.answer == document_answer
        assert result.references[0].type == "document"


async def test_scenario_memory_cannot_supply_current_evidence(mock_backend):
    with pytest.raises(InvalidModelResponse):
        await with_context(
            FakeModel(final(SCENARIO, mode="scenario")),
            mock_backend[0],
            history(),
            "¿Y si sigue tardando?",
        )
    assert not mock_backend[1]


async def test_unquantified_production_scenario_preserves_official_deadline(payloads, mock_backend):
    current_project(payloads)
    before = json.dumps(payloads, sort_keys=True)
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        final(NO_QUANTIFICATION, [f"project:{PROJECT_ID}"], mode="scenario"),
    )
    result = await with_context(model, mock_backend[0], history(), "¿Y si Producción tarda 3 días?")
    assert result.answer == NO_QUANTIFICATION
    assert INVALID_CAUSAL_FIXTURE not in result.answer
    assert json.dumps(payloads, sort_keys=True) == before
    assert all(request["method"] == "GET" for request in mock_backend[1])
    # This checks the scripted response contract, not rejection of arbitrary false model prose.
    assert "without formal dependencies do not assert Producción +3" in SYSTEM_PROMPT


async def test_live_project_then_delay_then_production_keeps_session_and_fresh_reads(
    payloads, mock_backend
):
    current_project(payloads)
    steps = [
        (
            "factual",
            "¿Cómo va Casa Ximena?",
            "Casa Ximena tiene la presentación al cliente en curso.",
        ),
        ("scenario", "¿Y si el cliente se demora tres días más?", SCENARIO),
        ("scenario", "¿Eso afectaría Producción?", NO_QUANTIFICATION),
    ]
    scripted = []
    for mode, _, answer in steps:
        scripted.extend(
            [
                call("get_project_details", {"project_id": PROJECT_ID}),
                final(answer, [f"project:{PROJECT_ID}"], mode=mode),
            ]
        )
    model = FakeModel(*scripted)
    store = ConversationStore(MemoryRepository())
    key = (await store.create("manager")).conversation_id
    try:
        async with setup_live(mock_backend[0], model) as (manager, socket, vault, registry, _):
            manager._conversations = store
            await store.bind_live(SESSION, key, "manager")
            for number, (_, question, answer) in enumerate(steps, 1):
                begin(socket, f"response_{number}")
                socket.incoming.put_nowait(function(f"call_{number}", question))
                complete(socket, f"response_{number}")
                await until(
                    lambda n=number: sum(s["type"] == "response.create" for s in socket.sent) == n
                )
                assert not socket.closed and registry.owned(SESSION, "manager")
                assert vault._get(SESSION).subject == "manager"
                assert len(store.repository.turns[key]) == number
                paths = [r["path"] for r in mock_backend[1]]
                assert paths.count(f"/api/projects/{PROJECT_ID}") == number
                assert paths.count(f"/api/projects/{PROJECT_ID}/tasks") == number
                assert paths.count("/api/auth/me") == number
                assert not any("/documents" in path for path in paths)
                assert answer in repr(socket.sent)
            assert "answerMode" not in repr(socket.sent)
            assert "answer_mode" not in repr(store.repository.turns[key])
            assert INVALID_CAUSAL_FIXTURE not in repr(socket.sent)
    finally:
        await store.shutdown()


def test_model_receives_explicit_scenario_and_document_boundaries():
    # Prompt-level regression is deliberate: tool selection stays with the model, not a router.
    for rule in (
        'words like "debería", "plazo", "demora" or "tiempo"',
        "CURRENT FACTS, ASSUMPTION, POSSIBLE EFFECTS",
        "UNQUANTIFIED / UNKNOWN",
        "No formal dependency available != No analysis possible",
        "minimum sufficient evidence",
        "document content is indispensable",
    ):
        assert rule in SYSTEM_PROMPT
    descriptions = {tool.name: tool.description for tool in TOOL_DEFINITIONS}
    for name in ("get_project_documents", "analyze_project_documents"):
        assert "uncertainty fallback" in descriptions[name]
    assert "hypothetical" in descriptions["analyze_project_documents"]
    assert "current project/tasks suffice" in descriptions["get_project_documents"]
