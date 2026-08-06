"""Flow-set registry: which controller a mission runs and how its phases derive.

A flow set is a directory of CUE flows (flows/<set>/) plus the Python-side
spec registered here: the controller flow the agent loop enters, and the
ordered phase rules its check-phase action evaluates. A mission selects a
set via ``MissionConfig.flow_set``; the entry flow derives from the
registry (never persisted per-mission, so a controller rename cannot
strand old missions).

The phase NAMES in a set's spec are the contract with its controller
flow's check_phase resolver — the CUE rules route on exactly these
strings, so spec and resolver must stay in sync (documented in
IMPLEMENTATION.md's "Flow sets" section).

This module imports nothing from agent.* — it is consumed by
ouroboros.py (CLI validation), agent/mission_config.py (YAML
validation), and agent/actions/mission_actions.py (phase evaluation)
without cycle risk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhaseRule:
    """One ordered rule in a flow set's phase derivation.

    kinds:
      requires_planning   — mission missing, the plan object named by
                            ``attr`` missing, or goals missing -> ``phase``
                            (observation is derived from which precondition
                            failed, not from ``observation``)
      goal_type_incomplete — any incomplete goal of ``goal_type`` -> ``phase``;
                            ``observation`` may use {incomplete}/{total}
                            (counts goals OF THAT TYPE)
      flag_unset          — ``getattr(mission, flag, False)`` falsy -> ``phase``
      attr_truthy         — ``getattr(mission, flag, None)`` truthy -> ``phase``
                            (inverse of flag_unset; for a pending field like
                            ``pending_directive`` that, when set, must intercept
                            before the normal phases)
      regression_pending  — a file changed since the last regression sweep
                            (``last_edit_cycle > last_regression_cycle``) AND
                            either a grounded completed goal exists (protect it)
                            OR a sweep-reopened grounded incomplete goal exists
                            (re-clear it) -> ``phase`` (runs the bidirectional
                            cross-goal acceptance-check regression suite)
      warnings_pending    — any ``mission.pending_warnings`` entry with
                            ``status == "pending"`` -> ``phase`` (the second
                            evidence channel: deterministic findings with no
                            PTY session behind them). Self-limiting — a
                            dispatch marks the entry ``dispatched`` and,
                            once attempts are spent, ``abandoned``
      terminal            — always matches (the spec's final rule)
    """

    kind: Literal[
        "requires_planning",
        "goal_type_incomplete",
        "flag_unset",
        "attr_truthy",
        "regression_pending",
        "warnings_pending",
        "terminal",
    ]
    phase: str
    goal_type: str = ""
    flag: str = ""
    observation: str = ""
    # requires_planning: the mission attribute holding the set's plan
    # object — "architecture" for the code pipeline, "research_plan"
    # for the scraper.
    attr: str = "architecture"
    # Stackable-phase ladder position (see PHASE_RANKS). 0 = always applies
    # (planning preconditions, regression protection, pending directives).
    # A ranked rule is SKIPPED when its rank exceeds the mission's
    # config.top_phase ceiling — evaluate_phases then returns 'complete'
    # once every applicable rule is satisfied, so a mission can be run "up
    # to structural" / "up to functional" etc. Default 0 keeps every
    # existing rule set (scraper, ingest) byte-identical in behavior.
    rank: int = 0


# Canonical phase ladder for the code pipeline: config.top_phase names one of
# these; rules ranked above it are skipped. "quality" is today's terminal, so
# the default ceiling reproduces existing behavior exactly. (The polish tier
# reserves 60 — the gate itself lands in a later change.)
PHASE_RANKS: dict[str, int] = {
    "structural": 10,
    "environment": 20,
    # Evidenced warnings sit between the environment existing and the next
    # functional goal — a deterministic finding is cheap to fix and its cost
    # compounds if the functional phase builds on top of it. Deliberately
    # RANKED (not rank 0 like replan/regression, which are uncappable): a
    # top_phase that stops short of functional runs no behavioural sessions,
    # so there is nothing observed worth diverting for.
    "warning": 25,
    "functional": 30,
    "test_suite": 40,
    "quality": 50,
    "polish": 60,
}


@dataclass(frozen=True)
class FlowSetSpec:
    """A registered flow set: its controller flow and phase derivation."""

    name: str
    entry_flow: str
    phases: tuple[PhaseRule, ...]


# The code pipeline — semantics identical to the original hardcoded
# action_check_pipeline_phase (observation strings included; tests pin them).
CODE_CORE_PHASES: tuple[PhaseRule, ...] = (
    # Brownfield re-entry: a directive added on reopen must be decomposed into
    # goals before anything else. First so it intercepts a reopened-and-complete
    # mission (which would otherwise route straight to the quality gate). Cleared
    # by action_derive_directive_goals, after which this falls through.
    PhaseRule(
        kind="attr_truthy",
        phase="replan",
        flag="pending_directive",
        observation="Pending directive — decomposing into goals (brownfield replan)",
    ),
    # Cross-goal regression suite (bidirectional): after an edit lands, run
    # every completed goal's acceptance checks (auto-reopen on a break) AND
    # re-run sweep-reopened goals' checks (auto-complete on a fix — re-clear a
    # root fix's whole blast radius in one wave). High priority (right after
    # replan) so a break is caught on the next cycle; inert until a grounded
    # goal exists (complete to protect, or sweep-reopened to re-clear), so it
    # never fires during the structural batch phase. Cleared by
    # action_regression_sweep advancing last_regression_cycle. See
    # evaluate_phases (regression_pending).
    PhaseRule(
        kind="regression_pending",
        phase="regression",
        observation="Edit since last sweep — running cross-goal regression suite",
    ),
    PhaseRule(kind="requires_planning", phase="plan"),
    PhaseRule(
        kind="goal_type_incomplete",
        phase="structural",
        goal_type="structural",
        observation="Structural phase: {incomplete}/{total} incomplete",
        rank=PHASE_RANKS["structural"],
    ),
    PhaseRule(
        kind="flag_unset",
        phase="environment",
        flag="environment_verified",
        observation="All structural goals complete — environment needs verification",
        rank=PHASE_RANKS["environment"],
    ),
    # Evidenced warnings — the second evidence channel. A deterministic check
    # (unaccounted runtime file, unreachable room graph, cross-module type
    # mismatch) has no PTY session behind it and therefore no route into the
    # repair loop; before 2026-08-06 such findings were logged and dropped.
    # Sits BEFORE functional so a finding is cleared before more work is built
    # on it, and AFTER environment so the workspace exists first. Self-limiting
    # via WarningRecord.attempts -> abandoned; see evaluate_phases
    # (warnings_pending).
    PhaseRule(
        kind="warnings_pending",
        phase="warning",
        observation="Evidenced warning pending — diverting to diagnose before the next goal",
        rank=PHASE_RANKS["warning"],
    ),
    PhaseRule(
        kind="goal_type_incomplete",
        phase="functional",
        goal_type="functional",
        observation="Functional phase: {incomplete}/{total} incomplete",
        rank=PHASE_RANKS["functional"],
    ),
    # Test-suite gate (Phase B.5): after functional goals complete, run the
    # repo's OWN suite before the quality gate. Fires until tests_verified is
    # set (the gate sets it when the suite passes / stands down; failures
    # harvest functional fix goals, which the functional rule above works
    # first, then this re-fires). Positioned after functional so harvested
    # fixes route back through the fix loop, not into this gate.
    PhaseRule(
        kind="flag_unset",
        phase="test_suite",
        flag="tests_verified",
        observation="Functional complete — running the repo's test suite",
        rank=PHASE_RANKS["test_suite"],
    ),
    # Quality goals are harvested from gate findings (origin="quality_gate")
    # for issues with no clean interact re-test; they're worked AFTER
    # functional so the build is otherwise sound. functional/structural
    # quality-origin goals are caught by the rules above and ride those
    # sweeps.
    PhaseRule(
        kind="goal_type_incomplete",
        phase="quality_fix",
        goal_type="quality",
        observation="Quality-fix phase: {incomplete}/{total} incomplete",
        rank=PHASE_RANKS["quality"],
    ),
    PhaseRule(
        kind="terminal",
        phase="quality",
        observation="All goals complete — ready for quality gate",
        rank=PHASE_RANKS["quality"],
    ),
)

DEFAULT_FLOW_SET = "code_core"

# The scraper pipeline: research-paper harvesting. Goals are per-aspect
# (discovery) plus one corpus-level catalog goal (type "extraction");
# papers themselves live in the workspace databank worklist, never as
# goals. Registered in FLOW_SETS by the scraper-flows commit — a set is
# creatable only once its flows exist.
SCRAPER_PHASES: tuple[PhaseRule, ...] = (
    PhaseRule(kind="requires_planning", phase="plan", attr="research_plan"),
    PhaseRule(
        kind="goal_type_incomplete",
        phase="discovery",
        goal_type="discovery",
        observation="Discovery phase: {incomplete}/{total} aspect(s) incomplete",
    ),
    PhaseRule(
        kind="goal_type_incomplete",
        phase="catalog",
        goal_type="extraction",
        observation="Catalog phase: {incomplete}/{total} corpus goal(s) incomplete",
    ),
    PhaseRule(
        kind="terminal",
        phase="gate",
        observation="All aspects discovered and cataloged — ready for research gate",
    ),
)

# The extractor set: scraper v2's stage-pipeline sibling. Operates on an
# EXISTING databank (working_dir shared with a completed scraper
# mission): one corpus-level pdf_extract goal sweeps OA PDFs through the
# Paddle-MLX toolchain in batches, then a fully deterministic gate
# verifies every record reached a terminal extraction state. Contains
# ZERO LLM turns — deterministic findings stay deterministic end-to-end.
EXTRACTOR_PHASES: tuple[PhaseRule, ...] = (
    PhaseRule(
        kind="goal_type_incomplete",
        phase="pdf_extract",
        goal_type="pdf_extract",
        observation="Extraction phase: {incomplete}/{total} corpus goal(s) incomplete",
    ),
    PhaseRule(
        kind="terminal",
        phase="extract_gate",
        observation="All extraction goals complete — ready for extraction gate",
    ),
)

# The curator pipeline — stage 3 of the corpus pipeline (scrape ->
# extract -> CURATE). fig_review sweeps VLM figure readings (sidecar
# tool batches); curate reviews + packs one paper per dispatch (a paper
# is a session lifecycle: ingest once, snapshot, review, pack, purge).
# The gate is derived: every extracted record terminal for both passes.
CURATOR_PHASES: tuple[PhaseRule, ...] = (
    PhaseRule(
        kind="goal_type_incomplete",
        phase="fig_review",
        goal_type="fig_review",
        observation="Figure-review phase: {incomplete}/{total} corpus goal(s) incomplete",
    ),
    PhaseRule(
        kind="goal_type_incomplete",
        phase="curate",
        goal_type="curate",
        observation="Curation phase: {incomplete}/{total} corpus goal(s) incomplete",
    ),
    PhaseRule(
        kind="terminal",
        phase="curate_gate",
        observation="All curation goals complete — ready for curation gate",
    ),
)

# The ops pipeline — a single terminal task worked until done. The task goal
# (one, type "task_exec") is incomplete until the completion judge marks it
# complete; then the run finishes. No multi-phase sweep.
OPS_PHASES: tuple[PhaseRule, ...] = (
    PhaseRule(
        kind="goal_type_incomplete",
        phase="task_exec",
        goal_type="task_exec",
        observation="Task in progress: {incomplete}/{total} task goal(s) incomplete",
    ),
    PhaseRule(
        kind="terminal",
        phase="complete",
        observation="Task complete",
    ),
)

# The `auto` router: entry is the in-graph `classify` flow, which picks the
# real flow_set (ops|code_core) + profile via menu turns and rewrites
# mission.config.flow_set to the concrete choice BEFORE any phase evaluation or
# handoff. These phases are therefore never consulted (classify has no
# check_phase step and tail-calls into the chosen controller); the lone terminal
# rule is a safety net so evaluate_phases never returns None if reached.
AUTO_PHASES: tuple[PhaseRule, ...] = (
    PhaseRule(kind="terminal", phase="complete", observation="Routing"),
)

FLOW_SETS: dict[str, FlowSetSpec] = {
    "auto": FlowSetSpec(
        name="auto",
        entry_flow="classify",
        phases=AUTO_PHASES,
    ),
    "code_core": FlowSetSpec(
        name="code_core",
        entry_flow="mission_control",
        phases=CODE_CORE_PHASES,
    ),
    # Contract-swarm A/B variant: identical phase contract, controller
    # retargets the parallel structural batch to build_contracts (the
    # contract → review → concurrent symbol workers → splice pipeline).
    "contract_swarm": FlowSetSpec(
        name="contract_swarm",
        entry_flow="mission_control_swarm",
        phases=CODE_CORE_PHASES,
    ),
    # Ablation variant "batch + doctest & company": identical phase contract,
    # controller retargets the parallel structural batch to
    # build_structure_contracted (author contracts front → SINGLE batch
    # completion implementing them → no worker fan-out). Isolates contract
    # rigor from the swarm's parallelism.
    "batch_contracted": FlowSetSpec(
        name="batch_contracted",
        entry_flow="mission_control_contracted",
        phases=CODE_CORE_PHASES,
    ),
    # Coordination variant "integrated": full contract-swarm (author contracts →
    # gate-review → parallel per-symbol workers → splice-assemble), then ONE
    # seam-owning integrator pass (build_structure_integrated) re-emits the
    # assembled package reconciled — replacing reliance on the diffuse
    # per-goal repair loop. Isolates the coordination fix from the parallelism.
    "integrated": FlowSetSpec(
        name="integrated",
        entry_flow="mission_control_integrated",
        phases=CODE_CORE_PHASES,
    ),
    "scraper": FlowSetSpec(
        name="scraper",
        entry_flow="research_control",
        phases=SCRAPER_PHASES,
    ),
    "extractor": FlowSetSpec(
        name="extractor",
        entry_flow="extract_control",
        phases=EXTRACTOR_PHASES,
    ),
    "ops": FlowSetSpec(
        name="ops",
        entry_flow="ops_control",
        phases=OPS_PHASES,
    ),
    "curator": FlowSetSpec(
        name="curator",
        entry_flow="curate_control",
        phases=CURATOR_PHASES,
    ),
}


def get_flow_set(name: str) -> FlowSetSpec:
    """Look up a flow set; unknown names fall back to the default.

    Runtime resilience over strictness: a persisted mission referencing a
    set this build doesn't know should still run as the code pipeline
    rather than crash the loop. Creation-time validation (CLI/YAML) is
    where unknown names fail fast.
    """
    spec = FLOW_SETS.get(name)
    if spec is None:
        logger.warning(
            "Unknown flow set %r — falling back to %r", name, DEFAULT_FLOW_SET
        )
        return FLOW_SETS[DEFAULT_FLOW_SET]
    return spec


def evaluate_phases(mission: Any, phases: tuple[PhaseRule, ...]) -> tuple[str, str]:
    """Evaluate a phase spec against mission state -> (phase, observation).

    Stackable ceiling: ``mission.config.top_phase`` names the highest ladder
    phase to pursue (PHASE_RANKS; default "quality" = the full pipeline).
    Ranked rules above the ceiling are skipped; when the walk then exhausts,
    the mission is 'complete' AT THAT CEILING — mission_control's existing
    ``phase == 'complete'`` route finalizes it. Rule sets with no ranked
    rules (scraper, ingest) never skip and keep the legacy exhaustion path.
    """
    top_phase = str(
        getattr(getattr(mission, "config", None), "top_phase", "") or "quality"
    )
    ceiling = PHASE_RANKS.get(top_phase, max(PHASE_RANKS.values()))
    skipped_above_ceiling = False
    for rule in phases:
        if rule.rank > ceiling:
            skipped_above_ceiling = True
            continue
        if rule.kind == "requires_planning":
            if not mission:
                return rule.phase, "No mission — needs planning"
            # The plan object exists to derive the FIRST goals. Once goals
            # exist, never re-enter blueprint design just because the plan
            # attr is missing: a brownfield ingest whose architecture parse
            # failed (swe-bench-langcodes) still has replan-derived goals,
            # and greenfield blueprint design on a foreign repo produces an
            # empty blueprint the design gate rightly rejects until the
            # mission dies. Work the goals; projections tolerate a missing
            # architecture (empty shells).
            if not getattr(mission, "goals", []):
                if not getattr(mission, rule.attr, None):
                    return rule.phase, f"No {rule.attr} — needs planning"
                return rule.phase, "No goals — needs planning"
            if not getattr(mission, rule.attr, None):
                # Goals exist but the plan object is missing. For a stamped
                # GREENFIELD mission this is never brownfield — it is a
                # failed/discarded design (OLMo 2026-07-23: arch parse
                # rejected → this skip functional-tested an empty repo for
                # 80 min, then spun on an unaddressable structural goal).
                # Route back to planning. Ingest and legacy ("") missions
                # keep the inference: brownfield goals legitimately outlive
                # a rejected blueprint (swe-bench-langcodes).
                origin = str(
                    getattr(getattr(mission, "config", None), "origin", "") or ""
                )
                if origin == "greenfield":
                    return (
                        rule.phase,
                        f"Greenfield mission with goals but no {rule.attr} — "
                        f"design incomplete, re-planning",
                    )
                logger.info(
                    "No %s but goals exist — skipping plan phase (brownfield)",
                    rule.attr,
                )
            continue

        if rule.kind == "goal_type_incomplete":
            of_type = [
                g for g in getattr(mission, "goals", []) if g.type == rule.goal_type
            ]
            incomplete = [g for g in of_type if g.status == "incomplete"]
            if incomplete:
                return rule.phase, rule.observation.format(
                    incomplete=len(incomplete), total=len(of_type)
                )
            continue

        if rule.kind == "flag_unset":
            if not getattr(mission, rule.flag, False):
                return rule.phase, rule.observation
            continue

        if rule.kind == "attr_truthy":
            # Fires when the named attr is set (truthy). Safe on a None
            # mission (getattr -> None -> falsy), so a pre-mission state
            # falls through to the planning precondition rather than erroring.
            if getattr(mission, rule.flag, None):
                return rule.phase, rule.observation
            continue

        if rule.kind == "regression_pending":
            # A file changed since the last sweep AND there is something for the
            # bidirectional sweep to act on: either a grounded COMPLETE goal to
            # protect (auto-reopen direction) OR a sweep-reopened grounded
            # INCOMPLETE goal to re-clear (auto-complete direction). The second
            # clause is required so a root fix that leaves ONLY reopened goals
            # still fires the wave (else they fall to functional and re-test one
            # interact cycle at a time). Grounding only happens after a genuine
            # pass, so this is inert during the structural batch phase. Safe on a
            # None mission. Fires at most once per edit (the sweep clears
            # regression_dirty) → cannot loop. The flag replaced the
            # restart-fragile cycle comparison (loop cycles reset per process;
            # the persisted watermark left the suite dormant after resumes).
            if getattr(mission, "regression_dirty", False) and any(
                (g.status == "complete" and getattr(g, "acceptance_checks", None))
                or (
                    g.status == "incomplete"
                    and getattr(g, "regression_reopened", False)
                    and getattr(g, "acceptance_grounded", False)
                    and getattr(g, "acceptance_checks", None)
                )
                for g in getattr(mission, "goals", []) or []
            ):
                return rule.phase, rule.observation
            continue

        if rule.kind == "warnings_pending":
            # Any evidenced warning still awaiting a fix attempt. `dispatched`
            # and `abandoned` both read as false here, so the divert fires at
            # most once per raise and a warning that cannot be cleared stops
            # diverting after WARNING_MAX_ATTEMPTS instead of starving the
            # functional queue forever. Safe on a None mission and on an old
            # mission.json that predates the field.
            if any(
                getattr(w, "status", "") == "pending"
                for w in getattr(mission, "pending_warnings", None) or []
            ):
                return rule.phase, rule.observation
            continue

        # terminal
        return rule.phase, rule.observation

    # A spec without a terminal rule is a registration bug; fail safe to
    # planning rather than raising mid-loop.
    if skipped_above_ceiling:
        # Every applicable rule is satisfied and the only unmet ones sit
        # above the mission's declared ceiling — complete AT the ceiling.
        return (
            "complete",
            f"top_phase '{top_phase}' satisfied — mission complete at ceiling",
        )
    logger.error("Phase spec exhausted without a terminal rule")
    return "plan", "Phase spec exhausted — re-planning"
