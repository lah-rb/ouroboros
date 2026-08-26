"""The one way code_core creates goals — creation-time contract enforcement.

Twelve creation sites, twelve field subsets, and every gap had a measured
cost on the qwen3.8 polish campaign: goals invisible to their own dedup
(no finding_signature), invisible to the sibling-constraint context (no
associated_files), and 8/15 polish goals closing on an LLM verdict alone.
The factory makes each consumer's implicit contract fail at CREATION time,
loudly, instead of at consumption time, silently.
"""

from __future__ import annotations

import pytest

from agent.goal_factory import (
    HARVEST_ORIGINS,
    KNOWN_ORIGINS,
    functional_goal,
    quality_goal,
    structural_goal,
)


def test_functional_defaults_match_the_consumer_contract():
    g = functional_goal(description="death warns before restarting", origin="design")
    assert g.type == "functional"
    assert g.status == "incomplete"
    assert g.origin == "design"
    # explicit, not None: every read site tests only for "deterministic",
    # so this is behaviour-identical — but nobody has to re-prove that.
    assert g.interaction_mode == "exploratory"


def test_deterministic_passes_through():
    g = functional_goal(
        description="Program starts cleanly and exits without errors",
        origin="design",
        interaction_mode="deterministic",
    )
    assert g.interaction_mode == "deterministic"


def test_quality_goals_carry_no_interaction_mode():
    g = quality_goal(
        description="prose is warm", origin="quality_gate", finding_signature="q:prose"
    )
    assert g.type == "quality"
    assert g.interaction_mode is None


def test_empty_description_refused():
    with pytest.raises(ValueError, match="description"):
        functional_goal(description="   ", origin="design")


def test_unknown_origin_refused():
    """More likely a typo than a new pipeline stage — new stages register
    in the factory, which doubles as the audit trail of goal creators."""
    with pytest.raises(ValueError, match="unknown goal origin"):
        functional_goal(description="x", origin="qality_gate", finding_signature="s")


@pytest.mark.parametrize("origin", sorted(HARVEST_ORIGINS - {"create_backfill"}))
def test_harvest_origins_require_a_signature(origin):
    """Idempotency, reopen, and the coverage-equivalence check all key on
    finding_signature — a harvest goal without one is invisible to its own
    dedup, which is how duplicates were minted."""
    with pytest.raises(ValueError, match="finding_signature"):
        functional_goal(description="x", origin=origin)


def test_design_origin_needs_no_signature():
    g = functional_goal(description="player can drop items", origin="design")
    assert g.finding_signature == ""


def test_structural_goals_must_name_their_deliverable():
    """The structural gate, the sibling-constraint context and fix-target
    resolution all key on associated_files."""
    with pytest.raises(ValueError, match="associated_files"):
        structural_goal(description="x", associated_files=[])
    g = structural_goal(description="Implement engine", associated_files=["engine.py"])
    assert g.associated_files == ["engine.py"]


def test_multiline_descriptions_survive_unnormalized():
    """The directive planner appends a Placement paragraph — internal
    whitespace is content, only the ends are stripped."""
    desc = "build the thing\n\nPlacement: near the gate"
    g = functional_goal(description=f"  {desc}  ", origin="directive")
    assert g.description == desc


def test_repro_commands_are_cleaned_not_dropped():
    g = functional_goal(
        description="Fix failing test: tests/test_x.py::test_y",
        origin="test_gate",
        finding_signature="test-gate:tests/test_x.py::test_y",
        repro_commands=["python -m pytest -q tests/test_x.py::test_y", "  "],
    )
    assert g.repro_commands == ["python -m pytest -q tests/test_x.py::test_y"]


def test_the_origin_registry_is_the_expected_set():
    """A new origin is a deliberate act: this pin makes adding one touch the
    factory (and this test), not just a call site."""
    assert KNOWN_ORIGINS == {
        "design",
        "directive",
        "quality_gate",
        "polish_gate",
        "test_gate",
        "create_backfill",
    }
