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
      terminal            — always matches (the spec's final rule)
    """

    kind: Literal[
        "requires_planning",
        "goal_type_incomplete",
        "flag_unset",
        "attr_truthy",
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
    PhaseRule(kind="requires_planning", phase="plan"),
    PhaseRule(
        kind="goal_type_incomplete",
        phase="structural",
        goal_type="structural",
        observation="Structural phase: {incomplete}/{total} incomplete",
    ),
    PhaseRule(
        kind="flag_unset",
        phase="environment",
        flag="environment_verified",
        observation="All structural goals complete — environment needs verification",
    ),
    PhaseRule(
        kind="goal_type_incomplete",
        phase="functional",
        goal_type="functional",
        observation="Functional phase: {incomplete}/{total} incomplete",
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
    ),
    PhaseRule(
        kind="terminal",
        phase="quality",
        observation="All goals complete — ready for quality gate",
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

FLOW_SETS: dict[str, FlowSetSpec] = {
    "code_core": FlowSetSpec(
        name="code_core",
        entry_flow="mission_control",
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
    """Evaluate a phase spec against mission state -> (phase, observation)."""
    for rule in phases:
        if rule.kind == "requires_planning":
            if not mission:
                return rule.phase, "No mission — needs planning"
            if not getattr(mission, rule.attr, None):
                return rule.phase, f"No {rule.attr} — needs planning"
            if not getattr(mission, "goals", []):
                return rule.phase, "No goals — needs planning"
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

        # terminal
        return rule.phase, rule.observation

    # A spec without a terminal rule is a registration bug; fail safe to
    # planning rather than raising mid-loop.
    logger.error("Phase spec exhausted without a terminal rule")
    return "plan", "Phase spec exhausted — re-planning"
