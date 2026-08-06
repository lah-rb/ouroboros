"""Evidenced warnings — the repair loop's second evidence channel.

Everything else in the repair cycle is driven by a PTY session: a failure is
fixed because a session observed it. A deterministic check has no session
behind it, so before 2026-08-06 its findings were logged and dropped.

Found the hard way. An 8-hour hy3 run (`tier_20260805-235509`) wrote
`save.json` every behavioural session with no `transient_files` declaration,
so every test inherited the previous test's state. The flush tripwire named
the file correctly on every cycle AND prescribed the right fix — and no reader
could ever see it: the note was tagged `['save.json']`, and
`_filter_notes_for_file` surfaces a note only to whoever is working on that
file. No goal targets a runtime artifact. Worse, `diagnose_issue` strips notes
from its session seed entirely, so even a correctly-addressed note would not
have arrived.

Hence two hard requirements, both tested below:

* the queue has a TERMINAL state — without one, a warning that cannot be
  cleared diverts every goal boundary forever, which is the acceptance-check
  permanent-veto bug in a new hat;
* the warning reaches diagnose_issue as flow INPUTS, verified through the real
  runtime rather than by reading the CUE. Today's `meta.attempt` defect proved
  that a unit test either side of a seam says nothing about the seam.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.mission_actions import (
    WARNING_MAX_ATTEMPTS,
    action_warning_sweep_next,
)
from agent.effects.mock import MockEffects
from agent.flow_sets import PHASE_RANKS, evaluate_phases, get_flow_set
from agent.models import StepInput
from agent.persistence.models import (
    ArchitectureState,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
    WarningRecord,
)

PHASES = get_flow_set("code_core").phases


def _mission(*, top_phase: str = "quality", goal_type: str = "functional"):
    m = MissionState(
        objective="t",
        config=MissionConfig(working_directory="/tmp/x", top_phase=top_phase),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=["a.py"],
            modules=[ModuleSpec(file="a.py", responsibility="x")],
        ),
        goals=[GoalRecord(description="g", type=goal_type, status="incomplete")],
    )
    m.environment_verified = True
    return m


def _raise(
    m, subject="save.json", evidence="save.json appeared and nothing accounts for it"
):
    return m.raise_warning(
        kind="unaccounted_runtime_file",
        subject=subject,
        evidence=evidence,
        prescribed_fix="Declare it in architecture.transient_files.",
        source_flow="flush_transient_files",
        max_attempts=WARNING_MAX_ATTEMPTS,
    )


class TestPersistence:
    def test_a_mission_without_the_field_still_loads(self):
        """Additive default. manager.py does no version gating, so an old
        mission.json must not fail to parse."""
        raw = _mission().model_dump()
        raw.pop("pending_warnings", None)
        assert MissionState.model_validate(raw).pending_warnings == []

    def test_an_unknown_status_coerces_rather_than_raising(self):
        """A strict Literal makes a whole archived mission unreadable —
        load_mission raises on the FIRST bad record. Same rationale as
        NoteRecord.category."""
        w = WarningRecord.model_validate({"kind": "k", "status": "from_the_future"})
        assert w.status == "pending"

    def test_round_trip(self):
        m = _mission()
        _raise(m)
        again = MissionState.model_validate(json.loads(m.model_dump_json()))
        assert again.pending_warnings[0].subject == "save.json"
        assert again.pending_warnings[0].status == "pending"


class TestTheReArmProtocol:
    def test_a_repeated_raise_does_not_enqueue_twice(self):
        """A producer runs every session; it must not queue every session.
        The tripwire this replaces was PUSHED ONCE for exactly this reason."""
        m = _mission()
        assert _raise(m) is True
        assert _raise(m) is False
        assert len(m.pending_warnings) == 1

    def test_dispatch_stops_the_divert(self):
        m = _mission()
        _raise(m)
        assert evaluate_phases(m, PHASES)[0] == "warning"
        m.dispatch_warning(m.next_pending_warning().id)
        assert evaluate_phases(m, PHASES)[0] == "functional"

    def test_re_sighting_after_a_fix_re_arms(self):
        """The producer firing again IS the evidence the fix did not work —
        nothing else has to re-check anything."""
        m = _mission()
        _raise(m)
        m.dispatch_warning(m.next_pending_warning().id)
        assert _raise(m) is True
        assert evaluate_phases(m, PHASES)[0] == "warning"

    def test_it_gets_K_genuine_repair_attempts_then_stands_down(self):
        """The abandon decision lives at RE-ARM, not dispatch. Deciding at
        dispatch would spend one attempt on bookkeeping and leave K-1 real
        repairs."""
        m = _mission()
        _raise(m)
        for _ in range(WARNING_MAX_ATTEMPTS):
            w = m.next_pending_warning()
            assert w is not None, "should still have attempts left"
            m.dispatch_warning(w.id)
            _raise(m)  # it came back
        assert m.pending_warnings[0].attempts == WARNING_MAX_ATTEMPTS
        assert m.pending_warnings[0].status == "abandoned"
        assert evaluate_phases(m, PHASES)[0] == "functional"

    def test_abandoned_is_terminal_and_never_re_arms(self):
        """Without this the queue starves the functional loop forever."""
        m = _mission()
        _raise(m)
        for _ in range(WARNING_MAX_ATTEMPTS):
            m.dispatch_warning(m.pending_warnings[0].id)
            _raise(m)
        assert _raise(m) is False
        assert evaluate_phases(m, PHASES)[0] == "functional"

    def test_an_abandoned_warning_stays_on_the_record(self):
        """Silent abandonment would be worse than the silence we started with."""
        m = _mission()
        _raise(m)
        for _ in range(WARNING_MAX_ATTEMPTS):
            m.dispatch_warning(m.pending_warnings[0].id)
            _raise(m)
        assert len(m.pending_warnings) == 1
        assert m.pending_warnings[0].evidence

    def test_distinct_subjects_queue_independently(self):
        m = _mission()
        assert _raise(m, subject="save.json") is True
        assert _raise(m, subject="cache.db") is True
        assert len(m.pending_warnings) == 2


class TestPhasePrecedence:
    def test_it_preempts_functional(self):
        m = _mission(goal_type="functional")
        _raise(m)
        assert evaluate_phases(m, PHASES)[0] == "warning"

    def test_structural_still_wins(self):
        """Clearing a warning must not jump the queue ahead of a half-built
        project."""
        m = _mission(goal_type="structural")
        _raise(m)
        assert evaluate_phases(m, PHASES)[0] == "structural"

    def test_it_is_capped_by_top_phase(self):
        """Rank 25, deliberately not rank 0. A run that stops short of
        functional executes no behavioural sessions, so there is nothing
        observed worth diverting for."""
        assert (
            PHASE_RANKS["environment"]
            < PHASE_RANKS["warning"]
            < PHASE_RANKS["functional"]
        )
        m = _mission(top_phase="structural", goal_type="functional")
        _raise(m)
        assert evaluate_phases(m, PHASES)[0] != "warning"

    def test_no_warnings_is_the_status_quo(self):
        assert evaluate_phases(_mission(), PHASES)[0] == "functional"


class TestEveryControllerMapsThePhase:
    """All four controllers share CODE_CORE_PHASES, so check_phase can return
    'warning' in any of them. An unmapped phase falls to the catch-all and
    loops on planning."""

    @pytest.mark.parametrize(
        "flow",
        [
            "mission_control",
            "mission_control_contracted",
            "mission_control_integrated",
            "mission_control_swarm",
        ],
    )
    def test_wired(self, flow):
        from pathlib import Path

        compiled = json.loads(
            (
                Path(__file__).resolve().parents[1] / "flows" / "compiled.json"
            ).read_text()
        )
        steps = compiled[flow]["steps"]
        rules = steps["check_phase"]["resolver"]["rules"]
        assert any("'warning'" in r["condition"] for r in rules), "phase not routed"
        assert "warning_sweep_next" in steps
        assert (
            steps["dispatch_warning_diagnosis"]["tail_call"]["flow"] == "diagnose_issue"
        )


class TestTheWarningReachesTheDiagnostician:
    """THE SEAM TEST. diagnose_issue strips notes from its seed, so a warning
    has to travel as flow INPUTS. Asserting the CUE names the keys proves
    nothing — this drives the real action and checks the payload."""

    @pytest.mark.asyncio
    async def test_the_dispatch_carries_the_evidence_verbatim(self):
        m = _mission()
        _raise(
            m,
            evidence="save.json appeared after the session and nothing accounts for it",
        )
        out = await action_warning_sweep_next(
            StepInput(context={"mission": m}, effects=MockEffects(mission=m))
        )
        cfg = out.context_updates["dispatch_config"]
        assert cfg["flow"] == "diagnose_issue"
        assert (
            "save.json appeared after the session" in cfg["flow_directive"]
        ), "a thin payload recreates the blind-diagnose trap"
        assert "transient_files" in cfg["flow_directive"], "prescribed fix must survive"
        assert "flush_transient_files" in cfg["flow_directive"], "name the observer"
        assert cfg["what_happened"]

    @pytest.mark.asyncio
    async def test_dispatching_consumes_the_warning(self):
        """Otherwise the same warning re-dispatches every cycle."""
        m = _mission()
        _raise(m)
        await action_warning_sweep_next(
            StepInput(context={"mission": m}, effects=MockEffects(mission=m))
        )
        assert m.pending_warnings[0].status == "dispatched"
        assert evaluate_phases(m, PHASES)[0] == "functional"

    @pytest.mark.asyncio
    async def test_an_empty_queue_returns_to_the_phase_check(self):
        m = _mission()
        out = await action_warning_sweep_next(
            StepInput(context={"mission": m}, effects=MockEffects(mission=m))
        )
        assert out.result["sweep_complete"] is True
        assert "dispatch_config" not in (out.context_updates or {})
