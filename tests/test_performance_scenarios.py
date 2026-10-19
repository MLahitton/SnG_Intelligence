import pytest
from performance_harness import SCENARIOS, run_scenario


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scenario,models,rounds,calls",
    [
        ("total_projects", 2, 1, 1),
        ("project_status", 3, 2, 2),
        ("ambiguous_project", 2, 1, 1),
        ("management_attention", 2, 1, 2),
        ("unavailable", 1, 0, 0),
    ],
)
async def test_baseline_optimized_quality_and_counts(scenario, models, rounds, calls):
    before, base, _ = await run_scenario(scenario, False)
    after, optimized, _ = await run_scenario(scenario, True)
    assert before == after  # Includes authoritative references and typed Presentation.
    assert optimized["auth_calls"] == 1
    assert base["auth_calls"] == 1 + calls
    assert optimized["agent_model_calls"] == base["agent_model_calls"] == models
    assert optimized.get("agent_tool_rounds", 0) == rounds
    assert optimized.get("tool_calls", 0) == calls
    assert scenario in SCENARIOS
    if scenario == "total_projects":
        assert after.answer == "Tenemos 19 proyectos."
    elif scenario == "ambiguous_project":
        assert after.response_type == "clarification" and len(after.references) == 2
    elif scenario == "project_status":
        assert after.references[0].label == "Smoke Test Proyecto"
    elif scenario == "unavailable":
        assert not after.references and after.response_type == "unavailable"
