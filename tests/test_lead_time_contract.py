"""Official wire names; historical spellings are rejection cases only."""

import json

import pytest
from pydantic import ValidationError

from app.contracts.management import AttentionItem, ManagementOverview, ManagementTask
from app.contracts.projects import ProjectTask
from tests.conftest import PROJECT_ID


@pytest.mark.parametrize(
    "model,path",
    [
        (ManagementTask, "/api/management/tasks"),
        (ProjectTask, f"/api/projects/{PROJECT_ID}/tasks"),
    ],
)
def test_task_reads_and_serializes_official_lead_time_status(payloads, model, path):
    payload = {**payloads[path][0], "leadTimeStatus": "OnTrack"}
    task = model.model_validate_json(json.dumps(payload))
    assert task.lead_time_status == "OnTrack"
    wire = json.loads(task.model_dump_json(by_alias=True))
    assert wire["leadTimeStatus"] == "OnTrack"
    assert "leakTimeStatus" not in wire
    assert "LeadTimeStatus" not in wire

    for obsolete in ("leakTimeStatus", "LeakTimeStatus", "LeadTimeStatus"):
        wrong = dict(payload)
        wrong[obsolete] = wrong.pop("leadTimeStatus")
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(wrong))


def test_overview_and_attention_use_one_official_contract(payloads):
    payload = payloads["/api/management/overview"]
    overview = ManagementOverview.model_validate_json(json.dumps(payload))
    assert overview.lead_time.overdue == 5
    wire = json.loads(overview.model_dump_json(by_alias=True))
    assert wire["leadTime"] == payload["leadTime"]
    assert "leakTime" not in wire
    for obsolete in ("leakTime", "LeakTime", "LeadTime"):
        wrong = dict(payload)
        wrong[obsolete] = wrong.pop("leadTime")
        with pytest.raises(ValidationError):
            ManagementOverview.model_validate_json(json.dumps(wrong))

    attention = {**payload["attention"]["items"][0], "leadTimeStatus": "OnTrack"}
    item = AttentionItem.model_validate_json(json.dumps(attention))
    assert item.lead_time_status == "OnTrack"
    assert json.loads(item.model_dump_json(by_alias=True))["leadTimeStatus"] == "OnTrack"
    for obsolete in ("leakTimeStatus", "LeakTimeStatus", "LeadTimeStatus"):
        wrong = dict(attention)
        wrong[obsolete] = wrong.pop("leadTimeStatus")
        with pytest.raises(ValidationError):
            AttentionItem.model_validate_json(json.dumps(wrong))
