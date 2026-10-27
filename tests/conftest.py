import copy
from contextlib import asynccontextmanager

import httpx
import pytest
from conversation_repository import MemoryRepository

from app.core.config import Settings

PROJECT_ID = "10000000-0000-4000-8000-000000000001"
TASK_ID = "20000000-0000-4000-8000-000000000002"
CLIENT_ID = "30000000-0000-4000-8000-000000000003"
DATE = "2026-10-16T12:00:00Z"
TOKEN = "test-google-id-token.sensitive.signature"
AUTH = {
    "subject": "manager",
    "email": None,
    "name": None,
    "isManagement": True,
    "roles": ["Management"],
}


@pytest.fixture
def settings():
    return Settings(
        sng_control_base_url="https://control.test",
        database_url="postgresql://localhost/offline_test",
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def payloads():
    task = {
        "taskId": TASK_ID,
        "projectId": PROJECT_ID,
        "projectName": "Proyecto",
        "clientId": CLIENT_ID,
        "clientName": "Cliente",
        "sgCode": None,
        "city": "Bogotá",
        "productLine": "FutureProduct",
        "area": "Commercial",
        "type": "FutureTaskType",
        "status": "Pending",
        "assignedAt": DATE,
        "startedAt": None,
        "completedAt": None,
        "deadlineAt": None,
        "wasOverdue": None,
        "leadTimeStatus": "FutureLeadStatus",
    }
    return {
        "/api/auth/me": copy.deepcopy(AUTH),
        "/api/projects": [],
        "/api/management/overview": {
            "totalProjects": 19,
            "tasks": {"pending": 3, "inProgress": 5, "completed": 8},
            "leadTime": {
                "notStarted": 1,
                "onTrack": 2,
                "dueSoon": 3,
                "dueToday": 4,
                "overdue": 5,
                "completedOnTime": 6,
                "completedLate": 7,
                "untrackedLegacy": 8,
            },
            "areas": [
                {
                    "area": "FutureArea",
                    "pending": 3,
                    "inProgress": 2,
                    "completed": 1,
                    "overdue": 5,
                    "dueToday": 4,
                    "dueSoon": 3,
                }
            ],
            "attention": {
                "total": 17,
                "items": [
                    {
                        "taskId": TASK_ID,
                        "projectId": PROJECT_ID,
                        "projectName": "Proyecto",
                        "clientName": "Cliente",
                        "sgCode": None,
                        "area": "Commercial",
                        "taskType": "FutureTaskType",
                        "taskStatus": "Pending",
                        "leadTimeStatus": "FutureLeadStatus",
                        "deadlineAt": None,
                        "startedAt": None,
                    }
                ],
            },
        },
        "/api/management/tasks": [task, {**task, "taskId": CLIENT_ID, "projectName": "A"}],
        f"/api/projects/{PROJECT_ID}": {
            "id": PROJECT_ID,
            "clientId": CLIENT_ID,
            "name": "Proyecto",
            "city": "Bogotá",
            "productLine": "FutureProduct",
            "sgCode": None,
            "createdAt": DATE,
            "updatedAt": DATE,
            "createdBy": None,
        },
        f"/api/projects/{PROJECT_ID}/tasks": [
            {
                "id": TASK_ID,
                "projectId": PROJECT_ID,
                "area": "Budgets",
                "type": "FutureType",
                "status": "FutureStatus",
                "assignedAt": DATE,
                "startedAt": None,
                "completedAt": None,
                "assignedToGoogleSubject": None,
                "deadlineAt": None,
                "wasOverdue": None,
                "leadTimeStatus": "UntrackedLegacy",
            }
        ],
        f"/api/projects/{PROJECT_ID}/documents": {
            "documents": [
                {
                    "id": TASK_ID,
                    "originalFileName": "plano.pdf",
                    "contentType": None,
                    "sizeBytes": 42,
                    "uploadedAt": DATE,
                    "uploadedBy": {"email": None, "name": None},
                    "uploadBatchId": CLIENT_ID,
                    "isCurrent": True,
                    "replacedAt": None,
                    "replacedByDocumentId": None,
                    "inInitialHandoff": False,
                }
            ],
            "initialHandoff": None,
            "canModify": False,
            "canRemove": False,
            "canSendToBudgets": False,
        },
        f"/api/projects/{PROJECT_ID}/notifications": [
            {
                "id": CLIENT_ID,
                "projectId": PROJECT_ID,
                "projectTaskId": TASK_ID,
                "recipientGoogleSubject": "manager",
                "taskType": "FutureType",
                "kind": "FutureKind",
                "state": "FutureState",
                "title": "Aviso",
                "message": "Mensaje oficial",
                "scheduledAt": DATE,
                "createdAt": DATE,
                "resolvedAt": None,
                "deadlineAt": None,
            }
        ],
    }


@pytest.fixture
def mock_backend(payloads):
    requests = []

    def handle(request: httpx.Request):
        # Store safe test observations, never request objects holding credentials.
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "authorized": request.headers.get("authorization") == f"Bearer {TOKEN}",
                "request_id": request.headers.get("x-request-id"),
                "active_role": request.headers.get("x-sng-active-role"),
                "cookie": request.headers.get("cookie"),
            }
        )
        assert request.url.host == "control.test"
        assert not request.url.query
        return httpx.Response(200, json=payloads[request.url.path])

    return httpx.MockTransport(handle), requests


@pytest.fixture(autouse=True)
def conversation_repository(monkeypatch):
    repository = MemoryRepository()

    @asynccontextmanager
    async def open_test_repository(database_url):
        yield repository

    monkeypatch.setenv("SNG_INTELLIGENCE_DATABASE_URL", "postgresql://localhost/offline_test")
    monkeypatch.setattr("app.main.open_repository", open_test_repository)
    return repository


@pytest.fixture(autouse=True)
def offline_memory_compactor(monkeypatch):
    from unittest.mock import AsyncMock

    from app.agent.errors import ProviderUnavailable

    compactor = AsyncMock(side_effect=ProviderUnavailable())
    monkeypatch.setattr("app.conversation.store.compact_memory", compactor)
    return compactor
