"""design_gate — the adversarial blueprint-coherence gate.

Pins: the grounding action (achievability filter, evidence-based over-block
guard, tighten-only union, fail-open budget), the critic step's fresh-but-
sufficient context, and the compiled flow wiring incl. the BLOCK-on-exhaustion
edge (→ failed) — the user's decision that no incoherent build ever proceeds.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.mission_actions import action_ground_design_gate_verdict
from agent.effects.mock import MockEffects
from agent.formatters import PRE_COMPUTE_FORMATTERS, format_architecture_listing
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    MissionConfig,
    MissionState,
    ModuleSpec,
)
from tests.conftest import compiled_flows as _compiled


def _incoherent_arch() -> ArchitectureState:
    # The thompson blueprint: package import scheme + `python -m` from root, but
    # modules under src/ + run-from-source (no install) — unrunnable by construction.
    return ArchitectureState(
        import_scheme="package",
        run_command="python -m regex_engine.cli",
        working_directory="project root",
        modules=[
            ModuleSpec(file="src/regex_engine/cli.py", responsibility="cli entry"),
            ModuleSpec(file="src/regex_engine/parser.py", responsibility="parser"),
        ],
    )


def _coherent_arch() -> ArchitectureState:
    return ArchitectureState(
        import_scheme="flat",
        run_command="python main.py",
        working_directory="project root",
        modules=[ModuleSpec(file="main.py", responsibility="entry")],
    )


def _mission(arch) -> MissionState:
    m = MissionState(
        objective="build a regex engine with a CLI",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="code_core"),
    )
    m.architecture = arch
    return m


def _si(mission, response) -> StepInput:
    return StepInput(
        context={"mission": mission, "inference_response": response},
        params={},
        meta=FlowMeta(flow_name="design_and_plan", step_id="design_gate_ground"),
        effects=MockEffects(),
    )


def _fenced(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


# ── grounding action behaviour ────────────────────────────────────────


@pytest.mark.asyncio
async def test_incoherent_verdict_grounds_persists_and_loops():
    m = _mission(_incoherent_arch())
    resp = _fenced(
        {
            "coherent": False,
            "reason": "python -m regex_engine.cli needs regex_engine at root but modules are under src/",
            "criteria": [
                "move modules from src/regex_engine/ to a top-level regex_engine/ package for python -m"
            ],
        }
    )
    out = await action_ground_design_gate_verdict(_si(m, resp))
    assert out.result["coherent"] is False
    a = m.architecture
    assert a.coherence_criteria and any(
        "src" in c.lower() for c in a.coherence_criteria
    )
    assert a.coherence_attempts == 1 and a.coherence_grounded is True
    assert any(
        n.category == "architecture_blueprint" and "rejected" in n.content
        for n in m.notes
    )


@pytest.mark.asyncio
async def test_coherent_verdict_passes_and_clears():
    m = _mission(_coherent_arch())
    resp = _fenced(
        {
            "coherent": True,
            "reason": "flat main.py at root matches python main.py",
            "criteria": [],
        }
    )
    out = await action_ground_design_gate_verdict(_si(m, resp))
    assert out.result["coherent"] is True
    assert m.architecture.coherence_criteria == []
    assert m.architecture.coherence_grounded is True


@pytest.mark.asyncio
async def test_grounding_drops_hallucinated_and_flips_when_all_hallucinated():
    # A real (blueprint-anchored) criterion survives; a hallucinated one is dropped.
    m = _mission(_incoherent_arch())
    resp = _fenced(
        {
            "coherent": False,
            "reason": "layout mismatch",
            "criteria": [
                "flatten src/ modules to root for python -m",
                "add caching to nonexistent_widget.py",
            ],
        }
    )
    out = await action_ground_design_gate_verdict(_si(m, resp))
    assert out.result["coherent"] is False
    assert m.architecture.coherence_criteria == [
        "flatten src/ modules to root for python -m"
    ]

    # ALL criteria hallucinated → no concrete incoherence survives → flip to
    # coherent (evidence-based over-block guard: never BLOCK on a vague critique).
    m2 = _mission(_incoherent_arch())
    resp2 = _fenced(
        {
            "coherent": False,
            "reason": "vague",
            "criteria": ["improve error handling in nonexistent_widget.py"],
        }
    )
    out2 = await action_ground_design_gate_verdict(_si(m2, resp2))
    assert out2.result["coherent"] is True


@pytest.mark.asyncio
async def test_unparseable_loops_then_fails_open_after_budget():
    # attempt 1: unparseable → loop (coherent False, gives the critic another pass)
    m = _mission(_incoherent_arch())
    out = await action_ground_design_gate_verdict(_si(m, "not json at all"))
    assert out.result["coherent"] is False and m.architecture.coherence_attempts == 1
    # attempt 3 (budget spent): unparseable → fail OPEN (a parse glitch must not BLOCK)
    m2 = _mission(_incoherent_arch())
    m2.architecture.coherence_attempts = 2
    out2 = await action_ground_design_gate_verdict(_si(m2, "still not json"))
    assert out2.result["coherent"] is True and m2.architecture.coherence_attempts == 3


@pytest.mark.asyncio
async def test_no_architecture_passes():
    m = MissionState(
        objective="x",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="code_core"),
    )
    out = await action_ground_design_gate_verdict(
        _si(m, _fenced({"coherent": False, "reason": "x", "criteria": ["y"]}))
    )
    assert out.result["coherent"] is True


# ── compiled wiring ───────────────────────────────────────────────────


def _rules(steps, step):
    return {r["condition"]: r["transition"] for r in steps[step]["resolver"]["rules"]}


def test_compiled_design_gate_wiring():
    steps = _compiled()["design_and_plan"]["steps"]
    assert "check_drift" not in steps  # renamed to design_gate_route
    assert _rules(steps, "build_repomap")["true"] == "design_gate_route"
    # route step keeps the pre-design routing (design / reconcile / derive)
    route = _rules(steps, "design_gate_route")
    assert route["result.has_architecture == false"] == "design_initial"
    assert route["result.drift_detected == true"] == "design_reconcile"
    assert route["result.has_tasks == true"] == "derive_goals"
    # both parse paths funnel through the gate
    assert (
        _rules(steps, "parse_architecture")["result.architecture_parsed == true"]
        == "design_gate_facts"
    )
    assert (
        _rules(steps, "parse_architecture_reconcile")[
            "result.architecture_parsed == true"
        ]
        == "design_gate_facts"
    )
    assert _rules(steps, "design_gate_facts")["true"] == "design_gate_critique"
    crit = _rules(steps, "design_gate_critique")
    assert crit["result.tokens_generated > 0"] == "design_gate_ground"
    assert crit["true"] == "derive_goals"  # fail-open on inference error
    # ground: pass / loop / BLOCK
    ground = _rules(steps, "design_gate_ground")
    assert ground["result.coherent == true"] == "design_gate_pass"
    assert (
        ground["result.coherent == false and meta.attempt <= 2"] == "design_reconcile"
    )
    assert ground["true"] == "failed"  # BLOCK on budget exhaustion (user decision)
    # pass preserves the web_research fork
    assert (
        _rules(steps, "design_gate_pass")["context.mission.config.web_research == true"]
        == "domain_research"
    )


def test_critique_context_is_fresh_and_bundle_is_sufficient():
    steps = _compiled()["design_and_plan"]["steps"]
    ctx = steps["design_gate_critique"]["context"]
    declared = set(ctx.get("required", []) + ctx.get("optional", []))
    # FRESH: the design step's reasoning + repomap are NOT declared → _build_step_input strips them.
    assert "inference_response" not in declared and "repo_map_formatted" not in declared
    # SUFFICIENT (starvation guard): the rendered blueprint carries the src path + run/smoke/wd,
    # so the critic can actually see the src/-vs-`python -m` mismatch.
    listing = format_architecture_listing({"source": _incoherent_arch()}, {})
    assert (
        "src/regex_engine/cli.py" in listing and "python -m regex_engine.cli" in listing
    )
    assert "Smoke command" in listing and "Working directory" in listing
    # the new formatters are registered
    for f in (
        "format_tooling_convention",
        "format_drift_facts",
        "format_prior_rejection",
    ):
        assert f in PRE_COMPUTE_FORMATTERS


class TestBarrenGenerationRetries:
    """A degenerate abort on the FIRST inference must not kill the mission.

    THE BUG (observed 2026-07-30, tier_20260730-220500 arm 7): gemma-4-31b hit
    a run-length degeneration abort at `design_initial`. A server-side abort
    returns 0 tokens, `design_initial` had only a `true -> failed` fallback, and
    the whole arm ended one minute in with zero files written.

    The asymmetry that made it wrong is in this same flow: `design_gate_critique`
    fails OPEN on the identical condition, with a comment saying a critic that
    could not run must not BLOCK. `design_initial` cannot fail open — there is no
    architecture yet to carry forward — so the equivalent is a bounded retry.
    That a retry is sufficient was shown the same night by glm-4.7-flash, whose
    degenerate abort at a CONTENT step retried on the same prompt and completed:
    lethality was positional, not intrinsic.
    """

    def test_zero_token_generation_retries_before_failing(self):
        rules = _rules(_compiled()["design_and_plan"]["steps"], "design_initial")
        assert rules["result.tokens_generated > 0"] == "parse_architecture"
        assert (
            rules["meta.attempt <= 2"] == "design_initial"
        ), "a barren generation must retry the step, not end the mission"

    def test_the_retry_is_bounded_and_still_ends_in_failure(self):
        """Bounded: the budget must remain, and `failed` must remain reachable.
        An unbounded self-loop would trade a dead arm for a spinning one."""
        rules = _rules(_compiled()["design_and_plan"]["steps"], "design_initial")
        assert rules["true"] == "failed"
        retry = [c for c, t in rules.items() if t == "design_initial"]
        assert retry == ["meta.attempt <= 2"], f"retry must stay budgeted: {retry}"

    def test_retry_is_ordered_before_the_failure_fallback(self):
        """`true` matches everything, so a retry rule placed after it is dead."""
        order = [
            r["condition"]
            for r in _compiled()["design_and_plan"]["steps"]["design_initial"][
                "resolver"
            ]["rules"]
        ]
        assert order.index("meta.attempt <= 2") < order.index("true")

    def test_the_critic_still_fails_OPEN_not_closed(self):
        """The counterpart this fix was reasoned from. If someone ever
        'harmonises' these two steps by making the critic fail closed, an
        unreachable critic would start blocking missions."""
        rules = _rules(_compiled()["design_and_plan"]["steps"], "design_gate_critique")
        assert rules["true"] == "derive_goals"
