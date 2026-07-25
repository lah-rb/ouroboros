"""quality_gate-origin findings route differently than design goals.

Three fixes the goal-driven validation forced (one injected startup crash
spawned 10 duplicate goals and thrashed ~10 false-pass/re-gate rounds before a
fix landed):

1. ``_quality_finding_signature`` anchors on the code locus (Python files +
   dotted/snake_case identifiers) so the gate's rephrasings of ONE defect
   ("references undefined X" / "raises NameError for X" / "crashes with X") map
   to ONE signature -> ONE goal, instead of one per round.
2. ``action_functional_sweep_next`` goes diagnose-FIRST for quality_gate-origin
   goals — the gate already confirmed the defect, so re-reproducing it via
   interact (which mis-frames a bug report as "verify this works") is skipped.
3. The post-fix interact re-test uses defect-resolution polarity for
   quality_gate-origin goals ("verify the defect no longer occurs"), not the
   design-goal "verify the described behavior works correctly".
"""

from __future__ import annotations

import pytest

from agent.actions.mission_actions import (
    _functional_retest_directive,
    _quality_finding_signature,
    action_functional_sweep_next,
    action_harvest_quality_findings,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    DirectiveReport,
    GoalRecord,
    MissionConfig,
    MissionState,
)
from tests.conftest import quality_gate_result as _qg


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        goals=goals if goals is not None else [],
        config=MissionConfig(working_directory="/tmp/x"),
    )


def _si(mission, **ctx) -> StepInput:
    return StepInput(
        context={"mission": mission, **ctx},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="x"),
        effects=MockEffects(),
    )


# ── Fix 1: signature is robust to the gate's rephrasing of one defect ──────

# The exact rephrasings the live validation produced for the single injected
# `_GOALDRIVEN_VALIDATION_SENTINEL` startup crash.
_SENTINEL_VARIANTS = [
    "main.py line 24 references undefined _GOALDRIVEN_VALIDATION_SENTINEL",
    "main.py line 24 references undefined variable _GOALDRIVEN_VALIDATION_SENTINEL",
    "main.py raises NameError for undefined '_GOALDRIVEN_VALIDATION_SENTINEL'",
    "main.py crashes on startup with NameError: _GOALDRIVEN_VALIDATION_SENTINEL",
    "main.py line 99 references undefined variable `_GOALDRIVEN_VALIDATION_SENTINEL`",
]


def test_signature_collapses_rephrasings_of_one_defect():
    sigs = {_quality_finding_signature({"issue": v}) for v in _SENTINEL_VARIANTS}
    assert len(sigs) == 1  # 10 dup goals in the wild -> exactly one now
    # anchored on file + symbol, line numbers dropped (they shift after edits)
    (only,) = sigs
    assert "main.py" in only and "_goaldriven_validation_sentinel" in only
    assert "line" not in only and "99" not in only


def test_signature_keeps_distinct_findings_distinct():
    a = _quality_finding_signature({"issue": "the `use` command has no effect"})
    b = _quality_finding_signature({"issue": "NPC dialogue branching is not tested"})
    c = _quality_finding_signature({"issue": "Kitchen room has no items"})
    assert len({a, b, c}) == 3


def test_signature_prose_fallback_when_no_code_anchor():
    # No .py / dotted / snake_case token -> normalized prose key (still stable).
    s = _quality_finding_signature({"issue": "Only 3 of 6 rooms were verified"})
    assert s == "only 3 of 6 rooms were verified"


@pytest.mark.asyncio
async def test_harvest_dedups_rephrased_defect_into_one_goal():
    """The acute bug: 5 rephrasings of one crash must yield ONE goal, not 5."""
    m = _mission()
    for v in _SENTINEL_VARIANTS:
        await action_harvest_quality_findings(
            _si(m, **_qg({"issue": v, "class": "functional"}))
        )
        # each round, all prior goals are still incomplete (being worked), so the
        # rephrase is skipped as in-flight
        for g in m.goals:
            g.status = "incomplete"
    qg = [g for g in m.goals if g.origin == "quality_gate"]
    assert len(qg) == 1


# ── Fix 2: quality_gate-origin functional goals go diagnose-first ─────────


def _fgoal(origin: str, reports=None) -> GoalRecord:
    return GoalRecord(
        description="main.py crashes on startup with NameError",
        type="functional",
        status="incomplete",
        origin=origin,
        interaction_mode="exploratory",
        reports=reports or [],
    )


@pytest.mark.asyncio
async def test_quality_gate_functional_goal_diagnoses_first():
    g = _fgoal("quality_gate")
    out = await action_functional_sweep_next(_si(_mission([g])))
    assert out.result.get("needs_fix") is True  # -> diagnose, not interact
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "diagnose_issue"
    assert dc["goal_id"] == g.id


@pytest.mark.asyncio
async def test_design_functional_goal_still_reproduces_via_interact():
    """Regression guard: design-origin goals keep interact-first (reproduce)."""
    g = _fgoal("design")
    out = await action_functional_sweep_next(_si(_mission([g])))
    assert out.result.get("needs_test") is True
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "interact"
    assert dc.get("charter_mode", "") != "explore"  # verify, not explore


# ── Brownfield "absent = build" goals (capability_absent) ────────────────


def _cgoal(reports=None) -> GoalRecord:
    return GoalRecord(
        description="Player can enter the boss room from the guard room",
        type="functional",
        status="incomplete",
        origin="directive",
        capability_absent=True,
        interaction_mode="exploratory",
        reports=reports or [],
    )


@pytest.mark.asyncio
async def test_capability_absent_first_dispatch_explores():
    out = await action_functional_sweep_next(_si(_mission([_cgoal()])))
    assert out.result.get("needs_test") is True
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "interact"
    assert dc["charter_mode"] == "explore"
    assert dc["interaction_mode"] == "exploratory"
    assert "does not exist yet" in dc["flow_directive"]


@pytest.mark.asyncio
async def test_capability_absent_explore_routes_to_build_not_complete():
    # The explore-interact session "succeeds" at scouting — but nothing is
    # built yet, so it must route to diagnose (build), NOT complete the goal.
    g = _cgoal(
        reports=[
            DirectiveReport(
                flow="interact", status="success", summary="spec: add east exit"
            )
        ]
    )
    out = await action_functional_sweep_next(_si(_mission([g])))
    assert g.status == "incomplete"  # did NOT complete on the explore success
    assert out.result.get("needs_fix") is True
    assert out.context_updates["dispatch_config"]["flow"] == "diagnose_issue"


@pytest.mark.asyncio
async def test_capability_absent_completes_after_build_and_retest():
    # Once a build (file_ops) has happened, a successful re-test interact
    # completes the goal like any functional goal.
    g = _cgoal(
        reports=[
            DirectiveReport(flow="interact", status="success", summary="spec"),
            DirectiveReport(flow="file_ops", status="success", summary="built it"),
            DirectiveReport(flow="interact", status="success", summary="works"),
        ]
    )
    await action_functional_sweep_next(_si(_mission([g])))
    assert g.status == "complete"


# ── Fix 3: post-fix re-test directive polarity ───────────────────────────


def test_retest_polarity_quality_gate_is_defect_resolution():
    g = _fgoal("quality_gate")
    d = _functional_retest_directive(g, after="fix").lower()
    assert "defect" in d and "no longer" in d
    assert "works correctly" not in d  # the polarity that false-passed crashes


def test_retest_polarity_design_is_capability_verification():
    g = _fgoal("design")
    d = _functional_retest_directive(g, after="fix").lower()
    assert "works correctly" in d
    assert "no longer" not in d


@pytest.mark.asyncio
async def test_quality_gate_goal_retests_with_defect_polarity_after_fix():
    """After a successful file_ops, a quality_gate functional goal re-tests with
    defect-resolution framing (end-to-end through the sweep, not just the helper)."""
    g = _fgoal(
        "quality_gate",
        reports=[
            DirectiveReport(flow="file_ops", status="success", summary="removed line")
        ],
    )
    out = await action_functional_sweep_next(_si(_mission([g])))
    assert out.result.get("needs_test") is True
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "interact"
    assert "no longer" in dc["flow_directive"].lower()


# ── Verify-before-harvest: probe-verified repros ride along ───────────────


def _verified_task(issue: str, repro: list, evidence: str = "use printed nothing"):
    return {
        "issue": issue,
        "description": issue,
        "class": "functional",
        "repro": repro,
        "verification": "confirmed",
        "verification_evidence": evidence,
    }


@pytest.mark.asyncio
async def test_harvest_stores_repro_and_evidence_on_created_goal():
    m = _mission()
    await action_harvest_quality_findings(
        _si(m, **_qg(_verified_task("`use` has no effect", ["take map", "use map"])))
    )
    (g,) = [x for x in m.goals if x.origin == "quality_gate"]
    assert g.repro_commands == ["take map", "use map"]
    assert g.verification_evidence == "use printed nothing"


@pytest.mark.asyncio
async def test_harvest_reopen_refreshes_repro_from_fresh_probe():
    m = _mission()
    await action_harvest_quality_findings(
        _si(m, **_qg(_verified_task("`use` has no effect", ["use map"], "old")))
    )
    (g,) = [x for x in m.goals if x.origin == "quality_gate"]
    g.status = "complete"
    # Next gate round re-reports the same signature with a fresh probe.
    await action_harvest_quality_findings(
        _si(
            m,
            **_qg(
                _verified_task("`use` has no effect", ["take map", "use map"], "new")
            ),
        )
    )
    assert g.status == "incomplete"
    assert g.repro_commands == ["take map", "use map"]
    assert g.verification_evidence == "new"


@pytest.mark.asyncio
async def test_diagnose_directive_carries_verified_repro():
    g = _fgoal("quality_gate")
    g.repro_commands = ["take map", "use map"]
    g.verification_evidence = "nothing happened"
    out = await action_functional_sweep_next(_si(_mission([g])))
    directive = out.context_updates["dispatch_config"]["flow_directive"]
    assert "Verified reproduction" in directive
    assert "1. take map" in directive and "2. use map" in directive
    assert "nothing happened" in directive


def test_retest_directive_replays_verified_repro():
    g = _fgoal("quality_gate")
    g.repro_commands = ["take map", "use map"]
    d = _functional_retest_directive(g, after="fix")
    assert "1. take map" in d
    assert "Re-run this exact sequence" in d


def test_directives_unchanged_without_repro():
    g = _fgoal("quality_gate")
    assert "Verified reproduction" not in _functional_retest_directive(g, after="fix")


@pytest.mark.asyncio
async def test_harvest_preserves_notes_pushed_mid_gate():
    """Lost-update regression: the gate pushes notes (refuted-claim
    telemetry) via effects.push_note, persisting them to disk — but the
    cycle's context mission predates the gate, and harvest's save was
    clobbering them. Harvest must freshen notes from disk first."""
    from agent.persistence.models import NoteRecord

    context_mission = _mission()  # what mission_control loaded at cycle start
    disk_mission = _mission()  # what push_note persisted mid-gate
    disk_mission.notes.append(
        NoteRecord(
            content="Quality gate claimed: save broken — probe REFUTED it",
            category="failure_analysis",
            tags=["refuted-finding"],
            source_flow="quality_gate",
        )
    )
    effects = MockEffects(mission=disk_mission)
    si = StepInput(
        context={
            "mission": context_mission,
            **_qg({"issue": "use broken", "class": "functional"}),
        },
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="harvest"),
        effects=effects,
    )
    await action_harvest_quality_findings(si)
    saved = effects._state["mission"]
    assert any("REFUTED" in n.content for n in saved.notes)
    assert any(g.origin == "quality_gate" for g in saved.goals)


def test_untested_prefix_is_framing_not_identity():
    """Live-observed duplicate class: 'take command was not exercised' and
    'untested: take command was not exercised' spawned separate goals across
    gate rounds. The prefix is framing — signatures must collapse it."""
    plain = _quality_finding_signature(
        {"issue": "take command was not exercised by the UX session"}
    )
    prefixed = _quality_finding_signature(
        {"issue": "untested: take command was not exercised by the UX session"}
    )
    assert plain == prefixed


# ── Fix 3b: harvested shape goal carries its data file ────────────────


@pytest.mark.asyncio
async def test_harvested_shape_goal_is_data_linked():
    """A shape fix_task carrying its data file (from _deterministic_shape_tasks)
    → the harvested goal is associated with that file, so the diagnose seed can
    surface the data instead of losing the link (files=[] → code-only)."""
    m = _mission()
    shape_task = {
        "issue": "rooms[2].exits: key 'west' is not in the declared example",
        "description": "rooms[2].exits: key 'west' is not in the declared example",
        "class": "functional",
        "repro": [],
        "file": "world/rooms.yaml",
        "signature": "shape|undeclared_key|rooms[2].exits|west",
    }
    await action_harvest_quality_findings(_si(m, **_qg(shape_task)))
    created = [x for x in m.goals if x.origin == "quality_gate"]
    assert len(created) == 1
    assert created[0].associated_files == ["world/rooms.yaml"]
    assert created[0].type == "functional"
