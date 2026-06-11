"""ResearchPlanState / AspectSpec model shapes (scraper flow set).

The research plan is the scraper's ArchitectureState analog —
mission.research_plan is additive (code missions and pre-scraper
mission.json files load unchanged), AspectSpec.from_llm_dict tolerates
field-name drift, and goal types discovery/extraction round-trip.
"""

from __future__ import annotations

from agent.persistence.models import (
    AspectSpec,
    GoalRecord,
    MissionConfig,
    MissionState,
    ResearchPlanState,
)


def test_aspect_from_llm_dict_tolerates_drift():
    a = AspectSpec.from_llm_dict(
        {"aspect": "grain boundaries", "queries": "GB segregation HEA", "target": "5"}
    )
    assert a.name == "grain boundaries"
    assert a.seed_queries == ["GB segregation HEA"]
    assert a.coverage_target == 5


def test_aspect_from_llm_dict_defaults():
    a = AspectSpec.from_llm_dict({"name": "x", "coverage_target": "junk"})
    assert a.coverage_target == 10
    assert a.seed_queries == []


def test_mission_round_trips_research_plan():
    m = MissionState(
        objective="abstract text",
        config=MissionConfig(working_directory="/tmp/x"),
        research_plan=ResearchPlanState(
            abstract="abstract text",
            aspects=[AspectSpec(name="a1", seed_queries=["q"])],
        ),
        goals=[
            GoalRecord(description="d", type="discovery"),
            GoalRecord(description="e", type="extraction"),
        ],
    )
    again = MissionState.model_validate(m.model_dump())
    assert again.research_plan.aspects[0].name == "a1"
    assert [g.type for g in again.goals] == ["discovery", "extraction"]


def test_pre_scraper_mission_json_loads_with_none_plan():
    legacy = {
        "objective": "t",
        "status": "active",
        "config": {"working_directory": "/tmp/x"},
    }
    assert MissionState.model_validate(legacy).research_plan is None
