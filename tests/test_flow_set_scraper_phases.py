"""SCRAPER_PHASES derivation (plan → discovery → catalog → gate).

The scraper's plan object is mission.research_plan, not architecture —
PhaseRule.attr names it, and the requires_planning observation derives
from the attr. The spec is exercised directly via evaluate_phases here;
the check_pipeline_phase wiring lands with the flows commit (the set is
registered in FLOW_SETS only once research_control exists).
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.flow_sets import SCRAPER_PHASES, evaluate_phases


def _goal(gtype, status="incomplete"):
    return SimpleNamespace(type=gtype, status=status)


def _mission(goals, *, plan=True):
    return SimpleNamespace(
        research_plan=object() if plan else None,
        architecture=None,  # scraper missions have no code architecture
        goals=goals,
    )


def test_no_research_plan_is_plan_phase():
    phase, obs = evaluate_phases(_mission([], plan=False), SCRAPER_PHASES)
    assert (phase, obs) == ("plan", "No research_plan — needs planning")


def test_no_goals_is_plan_phase():
    phase, _ = evaluate_phases(_mission([]), SCRAPER_PHASES)
    assert phase == "plan"


def test_incomplete_aspect_goals_are_discovery():
    m = _mission(
        [_goal("discovery"), _goal("discovery", "complete"), _goal("extraction")]
    )
    phase, obs = evaluate_phases(m, SCRAPER_PHASES)
    assert phase == "discovery"
    assert obs == "Discovery phase: 1/2 aspect(s) incomplete"


def test_discovery_done_routes_to_catalog():
    m = _mission([_goal("discovery", "complete"), _goal("extraction")])
    phase, obs = evaluate_phases(m, SCRAPER_PHASES)
    assert phase == "catalog"
    assert obs == "Catalog phase: 1/1 corpus goal(s) incomplete"


def test_all_complete_is_gate():
    m = _mission([_goal("discovery", "complete"), _goal("extraction", "complete")])
    phase, _ = evaluate_phases(m, SCRAPER_PHASES)
    assert phase == "gate"


def test_code_core_goal_types_are_inert_in_scraper_spec():
    # A functional/structural goal (e.g. from a mis-config) never routes
    # the scraper spec — only discovery/extraction types participate.
    m = _mission(
        [
            _goal("functional"),
            _goal("discovery", "complete"),
            _goal("extraction", "complete"),
        ]
    )
    phase, _ = evaluate_phases(m, SCRAPER_PHASES)
    assert phase == "gate"
