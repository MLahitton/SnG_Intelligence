import copy
import json
import logging
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import AUTH, CLIENT_ID, DATE, PROJECT_ID, TASK_ID, TOKEN
from conversation_repository import MemoryRepository
from pydantic import SecretStr, ValidationError
from test_agent import FakeModel, call, final, run_agent
from test_live import SESSION
from test_sideband import begin, complete, function, setup_live, until

from app.agent.contracts import ModelTurn
from app.agent.errors import (
    InvalidModelResponse,
    InvalidToolCall,
    SensitiveContent,
    ToolResultTooLarge,
)
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.quotations import quotation_data
from app.clients.sng_control import SnGControlClient
from app.clients.transport import create_http_client
from app.contracts.quotations import ConfirmedBudgetQuotation, ConfirmedCommercialQuotation
from app.conversation.store import ConversationStore
from app.core.errors import (
    AuthenticationRequired,
    BackendResponseError,
    BackendTimeout,
    BackendUnavailable,
    InvalidArguments,
    ManagementRequired,
)
from app.tools import ReadTools

pytestmark = pytest.mark.anyio
REFERENCE = f"project:{PROJECT_ID}"
METHODS = ["get_budget_quotation", "get_commercial_quotation"]


def quotation_path(source):
    return f"/api/projects/{PROJECT_ID}/{source}-quotation/view"


@pytest.fixture
def quotations(payloads):
    values = dict(
        itemNumber="01",
        location="Sala",
        system="A",
        glass="Laminado",
        aluminumFinish="Natural",
        lockType="",
        observations="",
        width=1.25,
        height=2.8,
        squareMeters=7.125,
        quantity=2,
    )
    budget = dict(
        projectId=PROJECT_ID,
        quotationVersionId=TASK_ID,
        versionNumber=1,
        confirmedAt=DATE,
        document=dict(
            fileName="synthetic.xlsx",
            clientName="Cliente",
            projectName="Casa Ximena",
            location="Ciudad",
            clientLine="Residencial",
            productionLine="Línea",
            preparedBy="Equipo",
            sgCode="SG0012",
        ),
        items=[
            dict(
                id=str(uuid4()),
                section="Main",
                sourceRow=15 + n,
                confirmed={**values, "itemNumber": f"0{n + 1}", "quantity": quantity},
                extracted={**values, "system": "EXTRACTED-NOT-CONFIRMED"},
                imageStatus=None,
                hasImage=False,
                technicalDifferences=[],
            )
            for n, quantity in enumerate([2, 1, 4])
        ],
    )
    commercial = dict(
        projectId=PROJECT_ID,
        commercialQuotationId=CLIENT_ID,
        confirmedAt=DATE,
        # Intentionally not a recomputed sum of item totals: the Backend owns this number.
        projectTotal=98.1234,
        items=[
            dict(
                section=item["section"],
                sourceRow=item["sourceRow"],
                commercialCatalogItemId=item["id"],
                values={**item["confirmed"], "system": "B", "glass": "Templado"},
                unitPrice=1.2345,
                totalPrice=42.5678,
                imageStatus="Stored",
                hasImage=True,
                technicalDifferences=[
                    dict(field="Glass", previousValue="Laminado", currentValue="Templado")
                ],
            )
            for item in budget["items"]
        ],
    )
    payloads[quotation_path("budget")] = budget
    payloads[quotation_path("commercial")] = commercial
    payloads[f"/api/projects/{PROJECT_ID}"]["name"] = "Casa Ximena"
    return budget, commercial


@pytest.mark.parametrize(
    "source,contract",
    [
        ("budget", ConfirmedBudgetQuotation),
        ("commercial", ConfirmedCommercialQuotation),
    ],
)
async def test_strict_client_and_readtools_paths_role_authorization(
    quotations, mock_backend, source, contract
):
    transport, calls = mock_backend
    async with create_http_client("https://control.test", 7, transport) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        try:
            result = await getattr(ReadTools(client), f"get_{source}_quotation")(PROJECT_ID)
        finally:
            client.clear_credentials()
    assert isinstance(result, contract) and result.project_id == UUID(PROJECT_ID)
    assert [r["path"] for r in calls] == ["/api/auth/me", quotation_path(source)]
    assert calls[-1]["method"] == "GET" and calls[-1]["active_role"] == "Management"
    assert all(r["authorized"] for r in calls)


@pytest.mark.parametrize("source", ["budget", "commercial"])
@pytest.mark.parametrize(
    "status,error",
    [
        (401, AuthenticationRequired),
        (403, ManagementRequired),
        (404, None),
        (429, BackendUnavailable),
        (500, BackendResponseError),
        (302, BackendResponseError),
    ],
)
async def test_only_quotation_404_is_optional(source, status, error):
    seen = []

    def backend(request):
        seen.append(request.url.path)
        assert request.headers["X-SnG-Active-Role"] == "Management"
        return httpx.Response(
            status, text="private diagnostic", headers={"Location": "https://evil.test"}
        )

    async with create_http_client("https://control.test", 7, httpx.MockTransport(backend)) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        try:
            method = getattr(client, f"get_{source}_quotation")
            if error:
                with pytest.raises(error) as caught:
                    await method(PROJECT_ID)
                assert "private" not in str(caught.value) and TOKEN not in str(caught.value)
            else:
                assert await method(PROJECT_ID) is None
        finally:
            client.clear_credentials()
    assert seen == [quotation_path(source)]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("subject", ["invalid-id", "forbidden", "missing-quotation", "timeout"])
async def test_readtools_safety(method, subject):
    seen = []

    def backend(request):
        seen.append(request.url.path)
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={**AUTH, "isManagement": subject != "forbidden"})
        if subject == "timeout":
            raise httpx.ReadTimeout("private transport error", request=request)
        return httpx.Response(404)

    async with create_http_client("https://control.test", 7, httpx.MockTransport(backend)) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        try:
            tool = getattr(ReadTools(client), method)
            if subject == "missing-quotation":
                assert await tool(PROJECT_ID) is None
            else:
                error = {
                    "invalid-id": InvalidArguments,
                    "forbidden": ManagementRequired,
                    "timeout": BackendTimeout,
                }[subject]
                with pytest.raises(error):
                    await tool("https://evil.test" if subject == "invalid-id" else PROJECT_ID)
        finally:
            client.clear_credentials()
    if subject == "invalid-id":
        assert not seen
    if subject == "forbidden":
        assert seen == ["/api/auth/me"]


@pytest.mark.parametrize("source,index", [("budget", 0), ("commercial", 1)])
@pytest.mark.parametrize(
    "invalid",
    [
        "extra",
        "nested-extra",
        "quantity-bool",
        "quantity-string",
        "measure-bool",
        "section",
        "image-status",
        "field",
        "missing",
        "wrong-project",
        "date",
        "negative-price",
    ],
)
async def test_incompatible_backend_contract_fails_safely(quotations, source, index, invalid):
    data = copy.deepcopy(quotations[index])
    item = data["items"][0]
    values = item["confirmed" if source == "budget" else "values"]
    if invalid == "extra":
        data["storageKey"] = "private-path"
    elif invalid == "nested-extra":
        values["secret"] = "private-data"
    elif invalid == "quantity-bool":
        values["quantity"] = True
    elif invalid == "quantity-string":
        values["quantity"] = "2"
    elif invalid == "measure-bool":
        values["width"] = True
    elif invalid == "section":
        item["section"] = "Unknown"
    elif invalid == "image-status":
        item["imageStatus"] = 1
    elif invalid == "field":
        item["technicalDifferences"] = [dict(field="Invented", previousValue="", currentValue="")]
    elif invalid == "missing":
        del values["glass"]
    elif invalid == "wrong-project":
        data["projectId"] = CLIENT_ID
    elif invalid == "date":
        data["confirmedAt"] = 123
    elif source == "commercial":
        item["unitPrice"] = -1
    else:
        values["height"] = -1
    async with create_http_client(
        "https://control.test", 7, httpx.MockTransport(lambda _: httpx.Response(200, json=data))
    ) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        try:
            with pytest.raises(BackendResponseError) as caught:
                await getattr(client, f"get_{source}_quotation")(PROJECT_ID)
            assert "private" not in str(caught.value)
        finally:
            client.clear_credentials()


async def test_projection_preserves_decimal_and_official_differences_without_internal_ids(
    quotations,
):
    for source, model, payload in zip(
        ["budget", "commercial"],
        [ConfirmedBudgetQuotation, ConfirmedCommercialQuotation],
        quotations,
        strict=True,
    ):
        parsed = model.model_validate_json(json.dumps(payload))
        result = quotation_data(UUID(PROJECT_ID), source, parsed)
        assert result["itemCount"] == 3 and result["totalQuantity"] == 7
        assert result["items"][0]["squareMeters"] == "7.125"
        assert result["items"][0]["width"] == "1.25"  # no reconstruction
        text = json.dumps(result)
        for internal in (
            "quotationVersionId",
            "commercialQuotationId",
            "commercialCatalogItemId",
            "extracted",
            "EXTRACTED-NOT-CONFIRMED",
            "totalSquareMeters",
            "currency",
        ):
            assert internal not in text
        assert not any(i["id"] in text for i in quotations[0]["items"])
        if source == "commercial":
            assert parsed.project_total == Decimal("98.1234")
            assert result["projectTotal"] == "98.1234"
            assert result["items"][0]["unitPrice"] == "1.2345"
            assert (
                result["items"][0]["technicalDifferences"]
                == payload["items"][0]["technicalDifferences"]
            )
        else:
            assert "Price" not in text and "projectTotal" not in result
            assert result["document"] == payload["document"]


@pytest.mark.parametrize("method", METHODS)
async def test_agent_requires_fresh_project_before_any_quotation_read(mock_backend, method):
    model = FakeModel(call(method, {"project_id": PROJECT_ID}))
    with pytest.raises(InvalidToolCall):
        await run_agent(model, mock_backend[0])
    assert not mock_backend[1]
    # Even a valid project read in the same requested batch cannot supply prior evidence.
    batch = ModelTurn(
        calls=(
            *call("get_project_details", {"project_id": PROJECT_ID}, "one").calls,
            *call(method, {"project_id": PROJECT_ID}, "two").calls,
        )
    )
    with pytest.raises(InvalidToolCall):
        await run_agent(FakeModel(batch), mock_backend[0])
    assert not mock_backend[1]


@pytest.mark.parametrize("source", ["budget", "commercial"])
async def test_large_quotation_is_rejected_without_partial_output(quotations, mock_backend, source):
    data = quotations[0 if source == "budget" else 1]
    field = "confirmed" if source == "budget" else "values"
    data["items"][0][field]["observations"] = "x" * 49_000
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call(f"get_{source}_quotation", {"project_id": PROJECT_ID}, "two"),
        final(),
    )
    with pytest.raises(ToolResultTooLarge):
        await run_agent(model, mock_backend[0])
    assert len(model.inputs) == 2


@pytest.mark.parametrize(
    "question,source,answer",
    [
        ("¿Cuántos ítems tiene Casa Ximena?", "commercial", "Casa Ximena tiene 3 ítems cotizados."),
        (
            "¿Cuántas estructuras de vidrio tiene?",
            "commercial",
            "La cotización registra 7 unidades en 3 ítems.",
        ),
        ("¿Qué sistema tiene?", "commercial", "El sistema registrado es B."),
        ("¿Qué sistema tenía Presupuestos?", "budget", "El sistema de Presupuestos es A."),
        ("¿Cuánto cuesta el proyecto?", "commercial", "El total registrado es 98.1234."),
        ("¿Qué cambió Comercial?", "commercial", "El vidrio cambió de Laminado a Templado."),
    ],
)
async def test_scripted_agent_quotation_semantics_and_source(
    quotations, mock_backend, question, source, answer
):
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call(f"get_{source}_quotation", {"project_id": PROJECT_ID}, "two"),
        final(answer, [REFERENCE]),
    )
    result, _ = await run_agent(model, mock_backend[0], question)
    data = json.loads(model.inputs[-1][0][-1].content)["data"]
    assert data["source"] == source and data["available"] is True
    assert (data["itemCount"], data["totalQuantity"]) == (3, 7)
    assert data["items"][0]["system"] == ("A" if source == "budget" else "B")
    assert result.answer == answer and "tareas" not in result.answer
    assert result.references[0].project_id == UUID(PROJECT_ID)
    paths = [r["path"] for r in mock_backend[1]]
    assert quotation_path(source) in paths
    assert quotation_path("commercial" if source == "budget" else "budget") not in paths
    assert not any("/file" in p or "/image" in p for p in paths)


async def test_task_question_still_uses_tasks_not_quotation(quotations, mock_backend):
    result, _ = await run_agent(
        FakeModel(
            call("get_project_details", {"project_id": PROJECT_ID}),
            final("Casa Ximena tiene 1 tarea.", [REFERENCE]),
        ),
        mock_backend[0],
        "¿Cuántas tareas tiene Casa Ximena?",
    )
    assert "1 tarea" in result.answer and "ítems" not in result.answer
    assert not any("quotation" in r["path"] for r in mock_backend[1])


async def test_absence_not_zero_and_no_task_count_fallback(quotations, payloads):
    def backend(request):
        if "quotation/view" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json=payloads[request.url.path])

    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "two"),
        call("get_budget_quotation", {"project_id": PROJECT_ID}, "three"),
        final("Tiene 1 tarea.", [REFERENCE]),
    )
    with pytest.raises(InvalidModelResponse):
        await run_agent(model, httpx.MockTransport(backend), "¿Cuántos ítems tiene?")
    for inputs in model.inputs[-2:]:
        data = json.loads(inputs[0][-1].content)["data"]
        assert (
            data["available"] is False and "itemCount" not in data and "totalQuantity" not in data
        )


async def test_no_new_navigation_contract_or_silent_empty_fields(quotations):
    parsed = ConfirmedBudgetQuotation.model_validate_json(json.dumps(quotations[0]))
    data = quotation_data(UUID(PROJECT_ID), "budget", parsed)
    assert data["items"][0]["lockType"] == ""
    assert data["items"][0]["imageStatus"] is None
    assert "No sumes ni multipliques para crear totalSquareMeters" in SYSTEM_PROMPT
    assert "Nunca respondas preguntas sobre ítems" in SYSTEM_PROMPT
    assert "NUNCA uses tareas como fallback" in SYSTEM_PROMPT
    assert "no uses itemCount de una cotización como conteo de tareas" in SYSTEM_PROMPT
    assert "no añadas COP, pesos ni $" in SYSTEM_PROMPT
    with pytest.raises(ValidationError):
        ConfirmedBudgetQuotation.model_validate_json('{"projectId":"https://evil.test"}')


@pytest.mark.parametrize("stage", ["commercial", "budget", "none"])
async def test_live_six_fresh_turns_fallback_and_unavailable_do_not_close(
    quotations, payloads, caplog, stage
):
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    paths = []

    def backend(request):
        path = request.url.path
        paths.append(path)
        if (path == quotation_path("commercial") and stage != "commercial") or (
            path == quotation_path("budget") and stage == "none"
        ):
            return httpx.Response(404)
        return httpx.Response(200, json=payloads[path])

    questions = [
        "¿Cómo va Casa Ximena?",
        "¿En qué estado está?",
        "¿Cuántos ítems tiene ese proyecto?",
        "¿Cuántas estructuras de vidrio tiene?",
        "¿Qué vidrio usa?",
        "¿Cuánto cuesta?",
    ]
    answers = [
        "Casa Ximena tiene 1 tarea.",
        "Casa Ximena: sin seguimiento histórico.",
        "La cotización tiene 3 ítems.",
        "La cotización registra 7 unidades en 3 ítems.",
        "El vidrio es Templado." if stage == "commercial" else "El vidrio es Laminado.",
        "El total registrado es 98.1234.",
    ]
    turns = []
    for index in range(6):
        turns.append(call("get_project_details", {"project_id": PROJECT_ID}))
        if index >= 2:
            turns.append(call("get_commercial_quotation", {"project_id": PROJECT_ID}, "two"))
            if index < 5 and stage != "commercial":
                turns.append(call("get_budget_quotation", {"project_id": PROJECT_ID}, "three"))
        unavailable = index >= 2 and (stage == "none" or (stage == "budget" and index == 5))
        turns.append(
            final(
                "No hay una cotización confirmada disponible para responder ese dato."
                if unavailable
                else answers[index],
                [REFERENCE],
                kind="unavailable" if unavailable else "direct",
            )
        )
    model = FakeModel(*turns)
    store = ConversationStore(MemoryRepository())
    entry = await store.create("manager")
    try:
        async with setup_live(httpx.MockTransport(backend), model) as (
            manager,
            socket,
            vault,
            registry,
            _,
        ):
            manager._conversations = store
            await store.bind_live(SESSION, entry.conversation_id, "manager")
            for number, question in enumerate(questions, 1):
                begin(socket, f"response_{number}")
                socket.incoming.put_nowait(function(f"call_{number}", question))
                complete(socket, f"response_{number}")
                await until(
                    lambda expected=number: (
                        sum(s["type"] == "response.create" for s in socket.sent) == expected
                    )
                )
                assert not socket.closed and registry.owned(SESSION, "manager")
                assert vault._get(SESSION).subject == "manager"
                assert store.conversation_for_live(SESSION) == entry.conversation_id
            assert len(store.repository.turns[entry.conversation_id]) == 6 and not model.turns
            for turn in store.repository.turns[entry.conversation_id]:
                assert json.loads(turn.references)[0]["projectId"] == PROJECT_ID
            assert paths.count("/api/auth/me") == 6
            assert paths.count(f"/api/projects/{PROJECT_ID}") == 6
            assert paths.count(quotation_path("commercial")) == 4
            assert paths.count(quotation_path("budget")) == (0 if stage == "commercial" else 3)
            assert "tarea" not in store.repository.turns[entry.conversation_id][2].assistant_content
            outputs = [
                json.loads(s["item"]["output"])
                for s in socket.sent
                if s["type"] == "response.item.create"
            ]
            assert len(outputs) == 6
            assert outputs[-1]["responseType"] == (
                "direct" if stage == "commercial" else "unavailable"
            )
            for messages, _ in model.inputs[2:]:
                assert "Casa Ximena" in repr(messages)
    finally:
        await store.shutdown()
    for private in (TOKEN, PROJECT_ID, "Casa Ximena", "98.1234", "Laminado"):
        assert private not in caplog.text


@pytest.mark.parametrize("method", METHODS)
async def test_ambiguous_lookup_cannot_choose_quotation(quotations, mock_backend, payloads, method):
    first = payloads[f"/api/projects/{PROJECT_ID}"]
    payloads["/api/projects"] = [first, {**first, "id": CLIENT_ID, "name": "Casa Norte"}]
    model = FakeModel(
        call("find_projects", {"query": "Casa"}), call(method, {"project_id": PROJECT_ID}, "two")
    )
    with pytest.raises(InvalidToolCall):
        await run_agent(model, mock_backend[0])
    assert not any("quotation" in r["path"] for r in mock_backend[1])


async def test_basic_voice_uses_same_quotation_agent(quotations, mock_backend, settings):
    from fastapi.testclient import TestClient
    from test_voice import ENDPOINTS, send, voice_app

    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "two"),
        final("La cotización tiene 3 ítems y 7 unidades.", [REFERENCE]),
    )
    app, stt, _, _ = voice_app(settings, mock_backend[0], model=model)
    stt.result = "¿Cuántos ítems tiene Casa Ximena?"
    with TestClient(app) as api:
        response = send(api, ENDPOINTS[1])
    assert response.status_code == 200
    assert response.json()["transcript"] == stt.result
    assert "3 ítems y 7 unidades" in response.json()["answer"]
    assert response.json()["references"][0]["projectId"] == PROJECT_ID


async def test_thirteen_items_sixteen_units_full_bounded_projection(quotations, mock_backend):
    budget, commercial = quotations
    for data in (budget, commercial):
        template = data["items"][0]
        field = "confirmed" if data is budget else "values"
        data["items"] = [
            {
                **copy.deepcopy(template),
                "sourceRow": 15 + n,
                field: {
                    **template[field],
                    "itemNumber": str(n + 1),
                    "quantity": 4 if n == 0 else 1,
                },
            }
            for n in range(13)
        ]
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "two"),
        final("La cotización registra 16 unidades en 13 ítems.", [REFERENCE]),
    )
    await run_agent(model, mock_backend[0], "¿Cuántas estructuras tiene?")
    result = model.inputs[-1][0][-1].content
    envelope = json.loads(result)
    assert envelope["complete"] is True
    assert (envelope["data"]["itemCount"], envelope["data"]["totalQuantity"]) == (13, 16)
    assert len(envelope["data"]["items"]) == 13
    assert len(result.encode("utf-8")) < 48_000


async def test_quotation_guard_and_existing_grounding_repair(quotations, mock_backend, caplog):
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    commercial = quotations[1]
    internal_key = f"project:{commercial['commercialQuotationId']}"
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "two"),
        final("La cotización tiene 3 ítems.", [internal_key]),
        final("La cotización tiene 3 ítems.", [REFERENCE]),
    )
    result, _ = await run_agent(model, mock_backend[0])
    assert result.references[0].project_id == UUID(PROJECT_ID)
    assert model.inputs[-1][1] == () and len(model.inputs) == 4
    assert "grounding_repair_succeeded" in caplog.text
    commercial["items"][0]["values"]["observations"] = TOKEN
    unsafe = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        call("get_commercial_quotation", {"project_id": PROJECT_ID}, "two"),
    )
    with pytest.raises(SensitiveContent):
        await run_agent(unsafe, mock_backend[0])
    assert len(unsafe.inputs) == 2
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("method", METHODS)
async def test_historical_project_reference_does_not_authorize_quotation(mock_backend, method):
    from datetime import UTC, datetime

    from app.agent.core import Agent
    from app.agent.security import CredentialGuard
    from app.agent.toolbox import ToolBox
    from app.conversation.context import ConversationContext, ConversationTurn

    history = ConversationContext(
        (
            ConversationTurn(
                "voice",
                "¿Cómo va Casa Ximena?",
                "Casa Ximena: pendiente.",
                "direct",
                json.dumps(
                    [dict(type="project", projectId=PROJECT_ID, label="Casa Ximena", sgCode=None)]
                ),
                datetime.now(UTC),
            ),
        )
    )
    guard = CredentialGuard(SecretStr(TOKEN), SecretStr("fake-openai-key"))
    model = FakeModel(call(method, {"project_id": PROJECT_ID}))
    async with create_http_client("https://control.test", 7, mock_backend[0]) as http:
        client = SnGControlClient(http, SecretStr(TOKEN), uuid4())
        try:
            with pytest.raises(InvalidToolCall):
                await Agent(model, ToolBox(ReadTools(client), guard), guard, {}).run(
                    "¿Cuántos ítems tiene ese proyecto?", history
                )
        finally:
            client.clear_credentials()
    assert not mock_backend[1]
