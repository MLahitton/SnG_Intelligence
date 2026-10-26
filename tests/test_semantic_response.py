"""Evidence boundaries and observable rendering; no live provider or acoustic assertions."""

import copy
import json
from datetime import datetime
from typing import get_args

import httpx
import pytest
from conftest import CLIENT_ID, PROJECT_ID, TASK_ID, TOKEN
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_agent import FakeModel, call, final, run_agent
from test_openai_model import invoke, output_message, response_body
from test_voice import AUTH, ENDPOINTS, voice_app

from app.agent.contracts import ChatResponse, ModelAnswer
from app.agent.errors import InvalidModelResponse, SensitiveContent, UngroundedReference
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.rendering import human_datetime, normalize_speech, render_answer
from app.contracts.presentation import ResponseType
from app.voice.style import speech_instructions


def semantic_payload(**overrides):
    return {
        "responseType": "direct",
        "answer": "Hola",
        "spokenAnswer": "Hola",
        "reference_keys": [],
        "presentation_keys": [],
        **overrides,
    }


@pytest.mark.parametrize("kind", get_args(ResponseType))
def test_closed_response_type_serialization(kind):
    parsed = ModelAnswer.model_validate_json(json.dumps(semantic_payload(responseType=kind)))
    assert parsed.model_dump(by_alias=True)["responseType"] == kind


@pytest.mark.parametrize("kind", ["unknown", "DIRECT", "", None, 1, {"type": "direct"}])
def test_invalid_response_types_rejected(kind):
    with pytest.raises(ValidationError):
        ModelAnswer.model_validate_json(json.dumps(semantic_payload(responseType=kind)))


def test_provider_schema_requires_all_fields_and_no_free_payload():
    schema = ModelAnswer.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]["responseType"]["enum"]) == set(get_args(ResponseType))
    for field in ("projectId", "taskId", "presentation", "value", "deadlineAt"):
        with pytest.raises(ValidationError):
            ModelAnswer.model_validate_json(json.dumps(semantic_payload(**{field: "forged"})))


@pytest.mark.anyio
@pytest.mark.parametrize("field", list(semantic_payload()))
async def test_real_responses_parser_rejects_missing_semantic_fields(field):
    payload = semantic_payload()
    del payload[field]
    with pytest.raises(InvalidModelResponse):
        await invoke(
            lambda _: httpx.Response(200, json=response_body([output_message(json.dumps(payload))]))
        )


@pytest.mark.anyio
async def test_direct_metric_is_official_total_not_count_of_attention_items(mock_backend):
    transport, _ = mock_backend
    result, _ = await run_agent(
        FakeModel(
            call("get_management_overview"),
            final("Tenemos 19 proyectos.", presentation=["metric:total_projects"]),
        ),
        transport,
    )
    body = result.model_dump(mode="json", by_alias=True)
    assert set(body) == {"responseType", "answer", "spokenAnswer", "references", "presentation"}
    assert body["presentation"] == {
        "kind": "direct",
        "metric": {"key": "total_projects", "label": "Proyectos", "value": 19},
    }
    assert body["answer"] == body["spokenAnswer"]
    assert ChatResponse.model_validate_json(json.dumps(body)) == result
    body["responseType"] = "unavailable"
    with pytest.raises(ValidationError):
        ChatResponse.model_validate_json(json.dumps(body))
    body["responseType"] = "direct"
    body["presentation"]["metric"]["url"] = "https://evil.test"
    with pytest.raises(ValidationError):
        ChatResponse.model_validate_json(json.dumps(body))


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["Smoke Test Proyecto", "SG001"])
async def test_project_name_first_metadata_and_spoken_from_same_source(
    mock_backend, payloads, query
):
    project = payloads[f"/api/projects/{PROJECT_ID}"]
    project.update(name="Smoke Test Proyecto", sgCode="SG001")
    payloads["/api/projects"] = [project]
    task = payloads[f"/api/projects/{PROJECT_ID}/tasks"][0]
    task.update(
        type="PrepareQuotation",
        status="InProgress",
        leadTimeStatus="OnTrack",
        deadlineAt="2026-10-21T22:00:00Z",
    )
    original = copy.deepcopy(payloads)
    transport, _ = mock_backend
    result, metrics = await run_agent(
        FakeModel(
            call("find_projects", {"query": query}),
            call("get_project_details", {"project_id": PROJECT_ID}, "details"),
            final(
                "PrepareQuotation: OnTrack. Vence 2026-10-21T22:00:00Z.",
                [f"project:{PROJECT_ID}", f"task:{TASK_ID}"],
                kind="project_status",
                presentation=[f"project:{PROJECT_ID}"],
            ),
        ),
        transport,
        f"¿Cómo va {query}?",
    )
    assert result.answer.startswith("Smoke Test Proyecto:")
    assert result.spoken_answer.startswith("Smoke Test Proyecto:")
    assert "SG001" not in result.spoken_answer
    assert "Preparar cotización: En tiempo" in result.answer
    assert "5:00 p. m." in result.answer and "cinco de la tarde" in result.spoken_answer
    assert all(
        word not in result.answer + result.spoken_answer
        for word in ("PrepareQuotation", "OnTrack", "2026-10-21T22:00:00Z")
    )
    metadata = result.model_dump(mode="json", by_alias=True)["presentation"]
    assert metadata["project"]["projectName"] == "Smoke Test Proyecto"
    assert metadata["project"]["sgCode"] == "SG001"
    assert metadata["tasks"][0]["deadlineAt"] == task["deadlineAt"]
    assert metadata["tasks"][0]["area"] == "Presupuestos"
    assert metadata["tasks"][0]["status"] == "En curso"
    assert metadata["tasks"][0]["leadTimeStatus"] == "En tiempo"
    assert str(result.references[1].task_id) == TASK_ID
    assert payloads == original
    assert metrics["tools_used"] == ["find_projects", "get_project_details"]


@pytest.mark.anyio
async def test_pending_without_deadline_preserves_official_not_started(mock_backend, payloads):
    task = payloads[f"/api/projects/{PROJECT_ID}/tasks"][0]
    task.update(type="PrepareQuotation", status="Pending", leadTimeStatus="NotStarted")
    transport, _ = mock_backend
    result, _ = await run_agent(
        FakeModel(
            call("get_project_details", {"project_id": PROJECT_ID}),
            final(
                "Preparar cotización: pendiente de inicio, sin plazo disponible.",
                kind="project_status",
                presentation=[f"project:{PROJECT_ID}"],
            ),
        ),
        transport,
    )
    assert result.presentation.tasks[0].deadline_at is None
    assert result.presentation.tasks[0].lead_time_status == "Sin iniciar"
    assert "atrasada" not in result.answer + result.spoken_answer
    assert "fuera de plazo" not in result.answer + result.spoken_answer


@pytest.mark.anyio
async def test_brief_and_list_ground_entities_and_keep_recommendation_separate(mock_backend):
    transport, _ = mock_backend
    prose = "Hecho: hay 5 tareas atrasadas. Recomendaría revisar los pendientes."
    result, _ = await run_agent(
        FakeModel(
            call("get_management_overview"),
            final(
                prose,
                kind="management_brief",
                presentation=[
                    "metric:lead_time.overdue",
                    "metric:attention.total",
                    f"task:{TASK_ID}",
                ],
            ),
        ),
        transport,
    )
    assert [m.value for m in result.presentation.metrics] == [5, 17]
    assert len(result.presentation.items) == 1  # 17 is official; never recompute it as 1.
    assert result.presentation.items[0].project_name == "Proyecto"
    assert result.spoken_answer == prose
    listed, _ = await run_agent(
        FakeModel(
            call("list_management_tasks"),
            final("Tareas recuperadas.", kind="list", presentation=[f"task:{TASK_ID}"]),
        ),
        transport,
    )
    assert str(listed.presentation.items[0].task_id) == TASK_ID
    assert listed.presentation.items[0].task is None  # Unknown enums don't acquire meanings.


@pytest.mark.anyio
async def test_comparison_preserves_each_official_area_count(mock_backend, payloads):
    areas = payloads["/api/management/overview"]["areas"]
    areas[0]["area"] = "Budgets"
    areas.append({**areas[0], "area": "Commercial", "pending": 6})
    transport, _ = mock_backend
    result, _ = await run_agent(
        FakeModel(
            call("get_management_overview"),
            final(
                "Comercial tiene más tareas pendientes.",
                kind="comparison",
                presentation=["area:0", "area:1"],
            ),
        ),
        transport,
    )
    assert [a.area for a in result.presentation.areas] == ["Presupuestos", "Comercial"]
    assert [a.pending for a in result.presentation.areas] == [3, 6]
    assert "eficiencia" not in result.answer


@pytest.mark.anyio
async def test_ambiguity_requires_all_candidates_and_never_selects_first(mock_backend, payloads):
    base = payloads[f"/api/projects/{PROJECT_ID}"]
    payloads["/api/projects"] = [
        {**base, "name": "Casa Ximena"},
        {**base, "id": CLIENT_ID, "name": "Casa Norte"},
    ]
    transport, calls = mock_backend
    result, _ = await run_agent(
        FakeModel(
            call("find_projects", {"query": "Casa"}),
            final(
                "¿Casa Ximena o Casa Norte?",
                kind="clarification",
                presentation=[f"project:{PROJECT_ID}", f"project:{CLIENT_ID}"],
            ),
        ),
        transport,
    )
    assert [p.project_name for p in result.presentation.candidates] == ["Casa Ximena", "Casa Norte"]
    assert not any(c["path"].startswith("/api/projects/") for c in calls)
    with pytest.raises(UngroundedReference):
        await run_agent(
            FakeModel(
                call("find_projects", {"query": "Casa"}),
                final(
                    "¿Casa Ximena?", kind="clarification", presentation=[f"project:{PROJECT_ID}"]
                ),
            ),
            transport,
        )


@pytest.mark.anyio
async def test_lookup_order_is_name_partial_auxiliary_then_sg(mock_backend, payloads):
    base = payloads[f"/api/projects/{PROJECT_ID}"]
    payloads["/api/projects"] = [
        {**base, "name": "Otro", "sgCode": "SG001"},
        {**base, "id": CLIENT_ID, "name": "Norte", "city": "SG001"},
        {**base, "id": TASK_ID, "name": "Casa SG001"},
        {**base, "id": "40000000-0000-4000-8000-000000000004", "name": "SG001"},
    ]
    transport, _ = mock_backend
    model = FakeModel(call("find_projects", {"query": "SG"}), final())
    await run_agent(model, transport)
    data = json.loads(model.inputs[1][0][-1].content)["data"]
    assert [c["projectName"] for c in data["candidates"]] == [
        "Casa SG001",
        "SG001",
        "Norte",
        "Otro",
    ]
    assert data["ambiguous"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_exact_project_name_precedes_partial_without_arbitrary_choice(
    mock_backend, payloads, duplicate
):
    base = payloads[f"/api/projects/{PROJECT_ID}"]
    payloads["/api/projects"] = [
        {**base, "id": CLIENT_ID, "name": "Smoke Test Proyecto 2"},
        {**base, "name": "Smoke Test Proyecto"},
    ]
    if duplicate:
        payloads["/api/projects"].append({**base, "id": TASK_ID, "name": "Smoke Test Proyecto"})
    transport, _ = mock_backend
    model = FakeModel(call("find_projects", {"query": "  smoke TEST proyecto  "}), final())
    await run_agent(model, transport)
    data = json.loads(model.inputs[1][0][-1].content)["data"]
    assert len(data["candidates"]) == (2 if duplicate else 1)
    assert all(c["projectName"] == "Smoke Test Proyecto" for c in data["candidates"])
    assert data["ambiguous"] is duplicate


@pytest.mark.anyio
async def test_unavailable_is_not_zero_or_fabricated_metric(mock_backend):
    transport, calls = mock_backend
    result, _ = await run_agent(
        FakeModel(final("No tengo datos de facturación disponibles.", kind="unavailable")),
        transport,
        "¿Cuánto facturamos este mes?",
    )
    assert result.presentation.reason == "data_not_available"
    assert result.answer == result.spoken_answer
    assert not result.references and not calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind,keys",
    [
        ("direct", ["metric:revenue"]),
        ("project_status", [f"project:{CLIENT_ID}"]),
        ("list", [f"task:{CLIENT_ID}"]),
        ("direct", [f"project:{PROJECT_ID}"]),
        ("project_status", [f"project:{PROJECT_ID}"]),  # Overview is not project details.
        ("unavailable", ["metric:total_projects"]),
        ("comparison", ["metric:total_projects"]),
    ],
)
async def test_presentation_rejects_unretrieved_or_wrong_kind_evidence(mock_backend, kind, keys):
    transport, _ = mock_backend
    with pytest.raises(UngroundedReference):
        await run_agent(
            FakeModel(call("get_management_overview"), final(kind=kind, presentation=keys)),
            transport,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "spoken", ["Tenemos 20 proyectos.", "Tenemos 19 proyectos y Casa Inventada."]
)
async def test_spoken_cannot_introduce_new_figures_or_entities(mock_backend, spoken):
    transport, _ = mock_backend
    with pytest.raises(InvalidModelResponse):
        await run_agent(
            FakeModel(
                call("get_management_overview"), final("Tenemos 19 proyectos.", spoken=spoken)
            ),
            transport,
        )


@pytest.mark.anyio
async def test_credential_in_spoken_is_rejected_before_presentation(mock_backend):
    transport, _ = mock_backend
    with pytest.raises(SensitiveContent):
        await run_agent(FakeModel(final(spoken=TOKEN)), transport)


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-10-21T22:00:00Z", "21 de octubre de 2026, a las cinco de la tarde"),
        ("2026-10-22T02:00:00Z", "21 de octubre de 2026, a las nueve de la noche"),
        ("2026-10-21T17:00:00-05:00", "21 de octubre de 2026, a las cinco de la tarde"),
        ("2026-10-22T05:00:00Z", "22 de octubre de 2026, a medianoche"),
        ("2026-10-21T17:00:00Z", "21 de octubre de 2026, a mediodía"),
    ],
)
def test_spoken_dates_use_bogota_without_changing_official_timestamp(stamp, expected):
    date = datetime.fromisoformat(stamp)
    assert human_datetime(date, spoken=True) == expected
    assert date == datetime.fromisoformat(stamp)


def test_speech_lexicon_preserves_names_and_never_inserts_unrequested_sg():
    names = {"Casa Ximena", "Smoke Test Proyecto", "Casa SG001", "Casa OnTrack"}
    text = "Casa Ximena, Smoke Test Proyecto, Casa SG001, Casa OnTrack. Código SG005."
    result = normalize_speech(text, names)
    assert all(name in result for name in names)
    assert result.endswith("Código ese ge cero cero cinco.")
    assert normalize_speech("SG001") == "ese ge cero cero uno"
    assert normalize_speech("Casa Ximena") == "Casa Ximena"
    assert normalize_speech("ASG001X") == "ASG001X"
    visual, spoken = render_answer(
        "**Hay 2 proyectos:**\n• Casa Ximena\n• Smoke Test Proyecto", names
    )
    assert "**" in visual and "•" in visual
    assert "**" not in spoken and "•" not in spoken
    assert "Hay 2 proyectos:" in spoken
    assert "ese ge" not in spoken
    assert "Casa OnTrack" in render_answer("Casa OnTrack: OnTrack", names)[0]


@pytest.mark.parametrize("kind", get_args(ResponseType))
def test_speech_response_type_only_selects_style(settings, mock_backend, kind):
    transport, _ = mock_backend
    app, _, tts, model = voice_app(settings, transport)
    text = "Casa Ximena tiene 2 tareas."
    with TestClient(app) as api:
        response = api.post(ENDPOINTS[2], headers=AUTH, json={"text": text, "responseType": kind})
    assert response.status_code == 200
    assert tts.inputs == [text]  # No re-interpretation or normalization without evidence.
    assert tts.instructions == [speech_instructions(kind)]
    assert not model.inputs


def test_tts_styles_distinct_and_request_contract_closed(settings, mock_backend):
    assert (
        len({speech_instructions(t) for t in ("direct", "management_brief", "clarification")}) == 3
    )
    transport, _ = mock_backend
    app, _, tts, _ = voice_app(settings, transport)
    with TestClient(app) as api:
        assert api.post(ENDPOINTS[2], headers=AUTH, json={"text": "Hola"}).status_code == 200
        for extra in ({"responseType": "arbitrary"}, {"instructions": "injected style"}):
            assert (
                api.post(ENDPOINTS[2], headers=AUTH, json={"text": "Hola", **extra}).status_code
                == 422
            )
    assert tts.instructions == [speech_instructions("direct")]


def test_prompt_natural_naming_recommendations_and_no_false_memory():
    for policy in (
        "nombre de proyecto es la identidad conversacional principal",
        "SG es metadata secundaria",
        "como coletilla",
        "Recomendaría",
        "Conversation 3A conserva referencias ordenadas",
        "spokenAnswer DEBE copiar answer exactamente",
    ):
        assert policy in SYSTEM_PROMPT


def test_renderer_preserves_negative_figures_and_names_containing_dates():
    text = "- 5\n2026-10-21T22:00:00Z\nCasa SG001 OnTrack"
    names = {"2026-10-21T22:00:00Z", "Casa SG001 OnTrack"}
    visual, spoken = render_answer(text, names)
    assert visual == spoken == text


@pytest.mark.anyio
async def test_metadata_rejects_task_attached_to_wrong_project(mock_backend, payloads):
    payloads[f"/api/projects/{PROJECT_ID}/tasks"][0]["projectId"] = CLIENT_ID
    transport, _ = mock_backend
    model = FakeModel(call("get_project_details", {"project_id": PROJECT_ID}), final())
    with pytest.raises(UngroundedReference):
        await run_agent(model, transport)
    assert len(model.inputs) == 1


@pytest.mark.anyio
async def test_presentation_evidence_does_not_carry_to_next_request(mock_backend):
    transport, _ = mock_backend
    result, _ = await run_agent(
        FakeModel(
            call("get_management_overview"),
            final("Hay 19 proyectos.", presentation=["metric:total_projects"]),
        ),
        transport,
    )
    assert result.presentation.metric.value == 19
    with pytest.raises(UngroundedReference):
        await run_agent(
            FakeModel(final("Hay 19 proyectos.", presentation=["metric:total_projects"])), transport
        )


def test_voice_new_semantic_project_contract_matches_chat(settings, mock_backend):
    from test_voice import send

    transport, _ = mock_backend
    turns = [
        call("get_project_details", {"project_id": PROJECT_ID}),
        final(
            "Tareas recuperadas.",
            [f"project:{PROJECT_ID}"],
            kind="project_status",
            presentation=[f"project:{PROJECT_ID}"],
        ),
    ]
    app, stt, _, _ = voice_app(settings, transport, model=FakeModel(*turns, *turns))
    with TestClient(app) as api:
        voice = send(api, ENDPOINTS[1])
        chat = api.post("/api/intelligence/chat", headers=AUTH, json={"message": stt.result})
    assert voice.status_code == chat.status_code == 200
    assert voice.json() == {"transcript": stt.result, **chat.json()}
    assert voice.json()["responseType"] == "project_status"
    assert voice.json()["answer"].startswith("Proyecto:")
    assert voice.json()["presentation"]["project"]["projectId"] == PROJECT_ID
