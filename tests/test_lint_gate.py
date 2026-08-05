"""Structural lint gate — ask once, accept whatever comes back.

THIS HAS BEEN BOTH WAYS, AND BOTH WERE WRONG.

Originally lint blocked hard: every finding had to be fixed or the file never
cleared the structural gate. A model eventually hit a ruff error it could not
actually fix and looped against the gate until the backstop.

The reaction was to make lint purely advisory — `required` is
`tier == "syntax"`, and the batch recorder scores a file passed unless a
REQUIRED check fails. That overshot. `ruff check --fix` has already applied
everything auto-fixable, so a residual finding is usually real: the 2026-07-27
poolside run recorded `F821 Undefined name 'random'` on engine.py, scored the
file as passing, closed the goal, and paid for it later in the functional phase
as a repeated diagnose loop over a one-line fix.

This is the middle path, and it is not new machinery — it is the contract the
IMPORT tier has used all along (`import_reviewed`): block for exactly one
fix-or-defer decision pass, then accept unconditionally.

WHY IT CANNOT DEADLOCK. The flag is set when the question is ASKED, not when it
is answered, and it is persisted before dispatch. An unfixable finding
therefore costs exactly one look. The mini-diagnose that receives it can also
decline outright (`confident: false`), which is a second and independent bound,
and its triaged-goal ledger prevents re-triage of the same goal id.
"""

from __future__ import annotations

from agent.actions.reporting_actions import (
    _maybe_complete_goal,
    structural_block_reason,
)
from agent.persistence.models import DirectiveReport, GoalRecord, NoteRecord


def _goal(checks_failed, *, status="success", flow="file_ops", files=("engine.py",)):
    """A structural goal whose last gate run produced `checks_failed`.

    Defaults to status="success" deliberately: a LINT-ONLY failure leaves the
    report reading successful, because only required checks drive `passed`.
    That is precisely why the burst filter has to admit lint explicitly.
    """
    g = GoalRecord(description="engine loads the world", type="structural")
    g.associated_files = list(files)
    g.reports.append(
        DirectiveReport(
            flow=flow,
            status=status,
            summary="",
            checks_failed=list(checks_failed),
            terminal_output="F821 Undefined name `random`\n --> engine.py:496:29",
        )
    )
    return g


# ── The gate ─────────────────────────────────────────────────────────


class TestTheGate:
    def test_the_flag_defaults_to_unasked(self):
        assert GoalRecord(description="d", type="structural").lint_reviewed is False

    def test_lint_blocks_once(self):
        g = _goal(["lint: engine.py"])
        assert structural_block_reason(g, g.reports[-1].checks_failed) == "lint"

    def test_and_is_accepted_forever_after(self):
        """The whole safety argument. Whatever the answer was — fixed, refused,
        or never even attempted — the goal proceeds."""
        g = _goal(["lint: engine.py"])
        g.lint_reviewed = True
        assert structural_block_reason(g, g.reports[-1].checks_failed) is None

    def test_syntax_still_outranks_it(self):
        g = _goal(["syntax: engine.py", "lint: engine.py"])
        assert structural_block_reason(g, g.reports[-1].checks_failed) == "syntax"

    def test_a_clean_run_still_does_not_block(self):
        g = _goal([])
        assert structural_block_reason(g, g.reports[-1].checks_failed) is None


class TestCompletion:
    def test_an_unasked_lint_failure_holds_the_goal_open(self):
        g = _goal(["lint: engine.py"])
        _maybe_complete_goal(g)
        assert (
            g.status == "incomplete"
        ), "this is the regression that shipped F821 into the functional phase"

    def test_an_asked_one_completes(self):
        g = _goal(["lint: engine.py"])
        g.lint_reviewed = True
        _maybe_complete_goal(g)
        assert g.status == "complete", "asked once is the bound — never a loop"


# ── Reaching the mini-diagnose ───────────────────────────────────────


class TestItReachesTheTriageBurst:
    """The burst is where the ask is cheapest AND where the module frame is
    selectable: _DIAGNOSE_WORKER_PROMPT offers `diagnosis_kind: fix|module_fix`
    with a `module_statement`, and that is carried to file_ops by
    _fileops_dispatch_from_quality_diagnosis -> check_module_fix -> the frame
    editor. A missing import is exactly the shape it can fix.
    """

    @staticmethod
    def _candidates(mission, tmp_path):
        from agent.actions.contract_swarm_actions import _diagnose_batch_candidates

        return _diagnose_batch_candidates(mission, str(tmp_path))

    @staticmethod
    def _mission(goals):
        class _M:
            pass

        m = _M()
        m.goals = list(goals)
        m.notes = []
        return m

    def test_a_lint_blocked_goal_is_admitted_despite_a_successful_report(
        self, tmp_path
    ):
        """THE FIX. The filter required status == "failed", which a lint-only
        failure never is — so the burst could not see this class at all."""
        (tmp_path / "engine.py").write_text("x = 1\n")
        g = _goal(["lint: engine.py"])
        got = self._candidates(self._mission([g]), tmp_path)
        assert [p for _, p, _ in got] == ["engine.py"]

    def test_once_asked_it_is_no_longer_a_candidate(self, tmp_path):
        (tmp_path / "engine.py").write_text("x = 1\n")
        g = _goal(["lint: engine.py"])
        g.lint_reviewed = True
        assert self._candidates(self._mission([g]), tmp_path) == []

    def test_the_import_decision_still_stays_off_the_burst(self, tmp_path):
        """It's a judgment call, not a defect investigation — it keeps the
        interactive file_ops path."""
        (tmp_path / "engine.py").write_text("x = 1\n")
        g = _goal(["import: engine.py"], status="failed")
        assert self._candidates(self._mission([g]), tmp_path) == []

    def test_a_prior_burst_is_not_repeated(self, tmp_path):
        (tmp_path / "engine.py").write_text("x = 1\n")
        g = _goal(["lint: engine.py"])
        m = self._mission([g])
        m.notes.append(
            NoteRecord(
                content=g.id,
                category="codebase_observation",
                tags=["diagnose_batch"],
                source_flow="diagnose_batch",
            )
        )
        assert self._candidates(m, tmp_path) == []

    def test_hard_gate_failures_are_still_admitted(self, tmp_path):
        """Scoping guard — the lint admission must not narrow the existing
        candidate set."""
        (tmp_path / "engine.py").write_text("x = 1\n")
        g = _goal(["syntax: engine.py"], status="failed")
        got = self._candidates(self._mission([g]), tmp_path)
        assert [p for _, p, _ in got] == ["engine.py"]


# ── Dispatch behaviour through the real sweep ────────────────────────


class TestTheSweepActuallyAsks:
    """End-to-end through action_structural_sweep_next, so the wiring is
    covered and not just the predicates."""

    @staticmethod
    def _mission(tmp_path, checks, *, status="success", mode="batch"):
        from agent.persistence.models import (
            ArchitectureState,
            MissionConfig,
            MissionState,
            ModuleSpec,
        )

        (tmp_path / "engine.py").write_text("x = 1\n")
        g = _goal(checks, status=status)
        return MissionState(
            objective="t",
            status="active",
            config=MissionConfig(working_directory=str(tmp_path), structural_mode=mode),
            architecture=ArchitectureState(
                run_command="python engine.py",
                creation_order=["engine.py"],
                modules=[ModuleSpec(file="engine.py", responsibility="engine")],
            ),
            goals=[g],
            notes=[],
        )

    @staticmethod
    async def _sweep(mission):
        from agent.actions.mission_actions import action_structural_sweep_next
        from agent.effects.mock import MockEffects
        from agent.models import FlowMeta, StepInput

        return await action_structural_sweep_next(
            StepInput(
                context={"mission": mission},
                params={},
                meta=FlowMeta(
                    flow_name="mission_control", step_id="structural_sweep_next"
                ),
                effects=MockEffects(mission=mission),
            )
        )

    def test_one_lint_goal_is_enough_to_fire_the_burst(self, tmp_path):
        """The >=2 threshold is an economy heuristic against the SERIAL
        diagnose a hard failure would take. For lint the counterfactual is
        spending nothing, so a lone finding still gets its one cheap look
        rather than stalling until some unrelated goal also fails."""
        import asyncio

        mission = self._mission(tmp_path, ["lint: engine.py"])
        out = asyncio.run(self._sweep(mission))
        assert out.result.get("needs_diagnose_batch") is True
        assert out.context_updates["dispatch_config"]["flow"] == "diagnose_batch"

    def test_dispatch_does_NOT_spend_the_flag(self, tmp_path):
        """REGRESSION. Marking at dispatch clears the block before the burst
        runs; swarm_diagnose_batch then recomputes candidates, finds nothing,
        and the gate triages NOTHING while still costing a cycle. Seen live on
        the APEX arm: dispatch_diagnose_batch -> fan_out_triage -> report_failed
        with the lint finding untouched. The flag is spent inside the burst,
        where a worker actually sees the finding."""
        import asyncio

        mission = self._mission(tmp_path, ["lint: engine.py"])
        asyncio.run(self._sweep(mission))
        assert (
            mission.goals[0].lint_reviewed is False
        ), "the goal must still be a candidate when the burst recomputes"

    def test_the_goal_survives_as_a_candidate_for_the_burst(self, tmp_path):
        """The property the above protects: after dispatch, the burst can still
        see the goal."""
        import asyncio

        from agent.actions.contract_swarm_actions import _diagnose_batch_candidates

        mission = self._mission(tmp_path, ["lint: engine.py"])
        asyncio.run(self._sweep(mission))
        got = _diagnose_batch_candidates(mission, str(tmp_path))
        assert [p for _, p, _ in got] == [
            "engine.py"
        ], "dispatch must not consume the very candidate it dispatched for"

    def test_a_burst_that_books_nothing_is_still_bounded(self, tmp_path):
        """THE ANTI-DEADLOCK PROPERTY, end to end.

        If the burst declines (confident: false) it books no diagnosis — but it
        does record the goal in the triaged ledger. On the next sweep the goal
        is therefore NOT a burst candidate, falls to the per-file decision, and
        that spends the flag. So an unfixable finding costs one burst plus one
        interactive ask, and then the goal proceeds forever after.
        """
        import asyncio

        from agent.actions.reporting_actions import structural_block_reason
        from agent.persistence.models import NoteRecord

        mission = self._mission(tmp_path, ["lint: engine.py"])
        g = mission.goals[0]
        # Simulate the burst having run and declined.
        mission.notes.append(
            NoteRecord(
                content=g.id,
                category="codebase_observation",
                tags=["diagnose_batch"],
                source_flow="diagnose_batch",
            )
        )
        asyncio.run(self._sweep(mission))
        assert g.lint_reviewed is True, "the per-file ask is the terminal bound"
        assert structural_block_reason(g, g.reports[-1].checks_failed) is None

    def test_serial_mode_asks_via_the_per_file_decision(self, tmp_path):
        """The burst is batch-only, so serial gets the fallback ask — framed as
        a decision, and marked reviewed just the same."""
        import asyncio

        mission = self._mission(tmp_path, ["lint: engine.py"], mode="serial")
        out = asyncio.run(self._sweep(mission))
        cfg = out.context_updates.get("dispatch_config", {})
        assert cfg.get("flow") == "file_ops"
        directive = cfg.get("flow_directive", "")
        assert "FAILS LINT" in directive
        assert "ONE pass" in directive, "the bound must be stated to the model"
        assert "make NO change" in directive, "declining must be a live option"
        assert mission.goals[0].lint_reviewed is True

    def test_a_clean_goal_just_completes(self, tmp_path):
        """Scoping guard: no lint finding, no ask, no burst."""
        import asyncio

        mission = self._mission(tmp_path, [])
        out = asyncio.run(self._sweep(mission))
        assert out.result.get("needs_diagnose_batch") is not True
        assert mission.goals[0].lint_reviewed is False
