import json
import logging

import httpx
import pytest
from conftest import AUTH, PROJECT_ID, TOKEN
from fastapi.testclient import TestClient
from test_agent import FakeModel, call, final

from app.agent.errors import ProviderConfigurationError, ProviderTimeout, ProviderUnavailable
from app.api.dependencies import get_agent_model
from app.main import create_app


def chat_app(settings, transport, model):
    app = create_app(settings, transport=transport)
    app.dependency_overrides[get_agent_model] = lambda: model
    return app


@pytest.mark.parametrize("management,status", [(True, 200), (False, 403)])
def test_chat_management_guard(settings, mock_backend, payloads, management, status):
    payloads["/api/auth/me"]["isManagement"] = management
    transport, _ = mock_backend
    model = FakeModel(final("Hola", mode="conversation"))
    with TestClient(chat_app(settings, transport, model)) as api:
        response = api.post(
            "/api/intelligence/chat",
            json={"message": "Hola"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == status
    assert len(model.inputs) == int(management)


def test_chat_without_token_is_401(settings, mock_backend):
    transport, calls = mock_backend
    model = FakeModel(final("Hola", mode="conversation"))
    with TestClient(chat_app(settings, transport, model)) as api:
        response = api.post("/api/intelligence/chat", json={"message": "Hola"})
    assert response.status_code == 401
    assert not calls and not model.inputs


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"message": ""},
        {"message": "   "},
        {"message": "a" * 4001},
        {"message": 1},
        {"message": "Hola", "token": TOKEN},
        {"message": "Hola", "subject": "forged"},
        {"message": "Hola", "isManagement": True},
        {"message": "Hola", "history": []},
        {"message": "Hola", "email": "fake@example.test"},
    ],
)
def test_chat_request_closed_and_validation_does_not_echo_inputs(
    settings, mock_backend, body, caplog
):
    transport, _ = mock_backend
    model = FakeModel(final("Hola", mode="conversation"))
    with TestClient(chat_app(settings, transport, model)) as api:
        response = api.post(
            "/api/intelligence/chat", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 422
    assert TOKEN not in response.text + caplog.text
    assert not model.inputs


@pytest.mark.parametrize(
    "key,model_name", [(None, None), ("fake-key", None), (None, "fake-model"), ("", "")]
)
def test_missing_provider_config_does_not_break_health(
    settings, mock_backend, monkeypatch, key, model_name
):
    for name, value in [("OPENAI_API_KEY", key), ("SNG_INTELLIGENCE_MODEL", model_name)]:
        monkeypatch.delenv(name, raising=False)
        if value is not None:
            monkeypatch.setenv(name, value)
    transport, calls = mock_backend
    with TestClient(create_app(settings, transport=transport)) as api:
        assert api.get("/health").status_code == 200
        assert not calls
        response = api.post(
            "/api/intelligence/chat",
            json={"message": "Hola"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 503
    assert response.json()["error"] == "agent_not_configured"
    assert [c["path"] for c in calls] == ["/api/auth/me"]


def test_no_management_is_rejected_before_provider_configuration(
    settings, mock_backend, payloads, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    payloads["/api/auth/me"]["isManagement"] = False
    transport, _ = mock_backend
    with TestClient(create_app(settings, transport=transport)) as api:
        response = api.post(
            "/api/intelligence/chat",
            json={"message": "Hola"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 403


def test_chat_exact_reference_contract_and_safe_metrics(settings, mock_backend, caplog):
    transport, _ = mock_backend
    model = FakeModel(
        call("get_project_details", {"project_id": PROJECT_ID}),
        final("Respuesta privada", [f"project:{PROJECT_ID}"]),
    )
    caplog.set_level(logging.INFO, logger="sng_intelligence")
    with TestClient(chat_app(settings, transport, model)) as api:
        response = api.post(
            "/api/intelligence/chat",
            json={"message": "Pregunta privada"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "responseType": "direct",
        "answer": "Respuesta privada",
        "spokenAnswer": "Respuesta privada",
        "presentation": {"kind": "direct", "metric": None},
        "references": [
            {
                "type": "project",
                "projectId": PROJECT_ID,
                "label": "Proyecto",
                "sgCode": None,
            }
        ],
    }
    logs = [
        json.loads(record.message) for record in caplog.records if record.name == "sng_intelligence"
    ]
    assert logs[-1]["tool_calls"] == 1
    assert logs[-1]["tools_used"] == ["get_project_details"]
    assert logs[-1]["status"] == 200
    for secret in ["Pregunta privada", "Respuesta privada", TOKEN]:
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "error,status",
    [(ProviderTimeout(), 504), (ProviderUnavailable(), 503), (ProviderConfigurationError(), 503)],
)
def test_provider_failure_never_logs_user_out(settings, mock_backend, error, status):
    transport, _ = mock_backend
    with TestClient(chat_app(settings, transport, FakeModel(error))) as api:
        response = api.post(
            "/api/intelligence/chat",
            json={"message": "Hola"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == status
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize("backend_status,status", [(404, 404), (403, 403), (500, 502)])
def test_tool_failure_is_safe_and_stops_generation(settings, backend_status, status):
    transport = httpx.MockTransport(
        lambda r: (
            httpx.Response(200, json=AUTH)
            if r.url.path == "/api/auth/me"
            else httpx.Response(backend_status, text=TOKEN)
        )
    )
    model = FakeModel(call("get_project_details", {"project_id": PROJECT_ID}), final())
    with TestClient(chat_app(settings, transport, model)) as api:
        response = api.post(
            "/api/intelligence/chat",
            json={"message": "Proyecto"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == status
    assert TOKEN not in response.text
    assert len(model.inputs) == 1


def test_backend_unavailable_stops_before_model(settings):
    def unavailable(request):
        raise httpx.ConnectError(TOKEN, request=request)

    model = FakeModel(final("Hola", mode="conversation"))
    with TestClient(chat_app(settings, httpx.MockTransport(unavailable), model)) as api:
        response = api.post(
            "/api/intelligence/chat",
            json={"message": "Hola"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 503
    assert not model.inputs


def test_provider_key_and_google_token_cannot_be_reflected(
    settings, mock_backend, monkeypatch, caplog
):
    key = "test-provider-private-key"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    transport, _ = mock_backend
    for secret in [TOKEN, key]:
        model = FakeModel(final("Here is " + secret))
        with TestClient(chat_app(settings, transport, model)) as api:
            response = api.post(
                "/api/intelligence/chat",
                json={"message": "Hola"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert response.status_code == 422
        assert secret not in response.text + caplog.text


def test_independent_requests_have_no_memory(settings, mock_backend):
    transport, _ = mock_backend
    model = FakeModel(final("Hola", mode="conversation"), final("Hola", mode="conversation"))
    with TestClient(chat_app(settings, transport, model)) as api:
        for message in ["primera pregunta", "segunda pregunta"]:
            assert (
                api.post(
                    "/api/intelligence/chat",
                    json={"message": message},
                    headers={"Authorization": f"Bearer {TOKEN}"},
                ).status_code
                == 200
            )
    assert len(model.inputs[0][0]) == len(model.inputs[1][0]) == 1
    assert "primera pregunta" not in repr(model.inputs[1])
