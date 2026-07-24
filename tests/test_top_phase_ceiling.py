"""Stackable phase ceiling: config.top_phase caps the pipeline ladder.

Rules ranked above the ceiling are skipped by evaluate_phases; when every
applicable rule is satisfied the mission is 'complete' AT the ceiling
(mission_control's existing phase == 'complete' route finalizes). Default
ceiling "quality" reproduces the legacy pipeline exactly. Unranked rule
sets (scraper, ingest) never skip and keep the legacy exhaustion error.
"""

from __future__ import annotations


from agent.flow_sets import CODE_CORE_PHASES, PHASE_RANKS, evaluate_phases
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


def _mission(tmp_path, top_phase="quality", goals=None, **flags):
    m = MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), top_phase=top_phase),
        goals=goals or [],
    )
    for k, v in flags.items():
        setattr(m, k, v)
    return m


def _g(gtype, status="incomplete"):
    return GoalRecord(description=gtype, type=gtype, status=status)


def test_default_ceiling_reaches_quality_terminal(tmp_path):
    # All goals complete + env + tests verified -> quality gate (today's path)
    m = _mission(
        tmp_path,
        goals=[_g("structural", "complete"), _g("functional", "complete")],
        environment_verified=True,
        tests_verified=True,
    )
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "quality"


def test_structural_ceiling_completes_after_structural(tmp_path):
    # Structural done; functional incomplete + env unverified would normally
    # fire — the ceiling skips them and completes.
    m = _mission(
        tmp_path,
        top_phase="structural",
        goals=[_g("structural", "complete"), _g("functional")],
    )
    phase, obs = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "complete"
    assert "structural" in obs


def test_structural_ceiling_still_works_structural_goals(tmp_path):
    m = _mission(tmp_path, top_phase="structural", goals=[_g("structural")])
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "structural"


def test_functional_ceiling_skips_tests_and_quality(tmp_path):
    m = _mission(
        tmp_path,
        top_phase="functional",
        goals=[_g("structural", "complete"), _g("functional", "complete")],
        environment_verified=True,
        tests_verified=False,  # would fire test_suite under default ceiling
    )
    phase, obs = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "complete"
    assert "functional" in obs


def test_functional_ceiling_keeps_environment_rule(tmp_path):
    # environment (rank 20) is BELOW the functional ceiling — still fires.
    m = _mission(
        tmp_path,
        top_phase="functional",
        goals=[_g("structural", "complete")],
        environment_verified=False,
    )
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "environment"


def test_rank0_rules_ignore_ceiling(tmp_path):
    # A pending directive (rank 0) intercepts regardless of ceiling.
    m = _mission(
        tmp_path,
        top_phase="structural",
        goals=[_g("structural", "complete")],
    )
    m.pending_directive = "do more"
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "replan"


def test_unranked_sets_keep_legacy_exhaustion(tmp_path):
    from agent.flow_sets import PhaseRule

    # A rule set with no ranked rules and no terminal: legacy error path,
    # NOT the ceiling-complete return.
    rules = (PhaseRule(kind="flag_unset", phase="x", flag="tests_verified"),)
    m = _mission(tmp_path, top_phase="structural", tests_verified=True)
    phase, obs = evaluate_phases(m, rules)
    assert phase == "plan"
    assert "exhausted" in obs


def test_legacy_mission_json_defaults(tmp_path):
    legacy = {
        "objective": "t",
        "status": "active",
        "config": {"working_directory": str(tmp_path)},
    }
    m = MissionState.model_validate(legacy)
    assert m.config.top_phase == "quality"
    assert m.completed_at_phase == ""


def test_phase_ranks_ladder_is_ordered():
    order = [
        "structural",
        "environment",
        "functional",
        "test_suite",
        "quality",
        "polish",
    ]
    ranks = [PHASE_RANKS[p] for p in order]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)
