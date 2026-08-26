"""The one way code_core creates goals.

Before this module, GoalRecord was constructed at twelve call sites with
twelve different field subsets, and every gap had a measured cost on the
qwen3.8 polish campaign (2026-08-22..26):

  * polish goals carried no ``interaction_mode`` and no files — the
    sibling-constraint context (built after the 17<->19 bossgame oscillation)
    keys on ``associated_files`` and was blind to two goals in contention on
    the same death path;
  * polish goals landed without the verification fields, and 8 of 15 closed
    on an LLM verdict alone — every goal the next consumer entry re-reported
    came from those eight, and none came from the seven that carried checks;
  * coverage findings keyed into a different signature space than the design
    goals covering the same behaviour, so their duplicates survived every
    lexical dedup by construction.

The factory does not invent policy: each rule below is a contract some
consumer already enforces implicitly (the reopen path keys on
``finding_signature``; the sibling context keys on ``associated_files``; the
sweeps branch on ``interaction_mode``). It makes the contract fail at
CREATION time, loudly, instead of at consumption time, silently.

Phrasing is the creators' half of the bargain and cannot be enforced here:
a goal description is read downstream as the TARGET STATE (diagnose renders
it under "THE GOAL:"), so it must state what the product should do, never
the complaint. Design goals always did this; polish goals do since triage
(be0be9c); quality-gate findings are defect reports by design and their
consumers polarity-correct per origin (``_functional_retest_directive``).

Scraper-side goal types (discovery, extraction, curate, ...) are out of
scope on purpose — that pipeline has its own conventions and may be moving
off goals entirely.
"""

from __future__ import annotations

from agent.persistence.models import GoalRecord

# Origins code_core is allowed to mint. An unknown origin is far more likely
# a typo than a new pipeline stage — new stages add themselves here, which is
# also the audit trail of who creates goals.
KNOWN_ORIGINS = frozenset(
    {
        "design",
        "directive",
        "quality_gate",
        "polish_gate",
        "test_gate",
        "create_backfill",
    }
)

# Origins whose goals are harvested from FINDINGS rather than planned.
# Idempotency (don't re-file a finding), reopen (re-file one whose fix did
# not hold) and the coverage-equivalence check all key on finding_signature,
# so a harvest-origin goal without one is invisible to its own dedup.
HARVEST_ORIGINS = frozenset(
    {"quality_gate", "polish_gate", "test_gate", "create_backfill"}
)


def _validate(description: str, origin: str, finding_signature: str) -> str:
    # Strip, never collapse: multi-line descriptions are legitimate (the
    # directive planner appends a Placement paragraph) and no prior site
    # normalized internal whitespace.
    text = str(description or "").strip()
    if not text:
        raise ValueError("a goal needs a description — refusing an empty one")
    if origin not in KNOWN_ORIGINS:
        raise ValueError(
            f"unknown goal origin {origin!r} — code_core origins are "
            f"{sorted(KNOWN_ORIGINS)}; new pipeline stages register in "
            f"agent/goal_factory.py"
        )
    if origin in HARVEST_ORIGINS and not str(finding_signature or "").strip():
        raise ValueError(
            f"a {origin!r} goal is harvested from a finding and MUST carry "
            f"its finding_signature — without it the goal cannot dedup, "
            f"reopen, or be seen by the coverage-equivalence check"
        )
    return text


def functional_goal(
    *,
    description: str,
    origin: str,
    finding_signature: str = "",
    interaction_mode: str | None = "exploratory",
    associated_files: list[str] | None = None,
    repro_commands: list[str] | None = None,
    verification_evidence: str = "",
    capability_absent: bool = False,
) -> GoalRecord:
    """A user-facing behaviour to verify (or, capability_absent, to build).

    interaction_mode defaults to "exploratory" explicitly: every read site
    tests only for "deterministic", so None and "exploratory" are the same
    behaviour — but an explicit value keeps the next reader from having to
    prove that again. Pass "deterministic" only for goals the interact flow
    should route to run_commands (the startup goal).
    """
    text = _validate(description, origin, finding_signature)
    return GoalRecord(
        description=text,
        type="functional",
        status="incomplete",
        origin=origin,
        finding_signature=str(finding_signature or ""),
        interaction_mode=interaction_mode,
        associated_files=list(associated_files or []),
        repro_commands=[str(c) for c in (repro_commands or []) if str(c).strip()],
        verification_evidence=str(verification_evidence or ""),
        capability_absent=bool(capability_absent),
    )


def quality_goal(
    *,
    description: str,
    origin: str,
    finding_signature: str = "",
    associated_files: list[str] | None = None,
    repro_commands: list[str] | None = None,
    verification_evidence: str = "",
) -> GoalRecord:
    """A matter of feel, wording or polish — completes on patch, no interact
    re-test, so it never carries an interaction_mode."""
    text = _validate(description, origin, finding_signature)
    return GoalRecord(
        description=text,
        type="quality",
        status="incomplete",
        origin=origin,
        finding_signature=str(finding_signature or ""),
        associated_files=list(associated_files or []),
        repro_commands=[str(c) for c in (repro_commands or []) if str(c).strip()],
        verification_evidence=str(verification_evidence or ""),
    )


def structural_goal(
    *,
    description: str,
    associated_files: list[str],
    origin: str = "design",
    finding_signature: str = "",
) -> GoalRecord:
    """A file/module deliverable. Files are REQUIRED: the structural gate,
    the sibling-constraint context and the fix-target resolution all key on
    associated_files, so a structural goal without them is unreachable by
    the machinery that exists to verify it."""
    text = _validate(description, origin, finding_signature)
    files = [str(f).strip() for f in (associated_files or []) if str(f).strip()]
    if not files:
        raise ValueError(
            "a structural goal names its deliverable — associated_files "
            "must not be empty (the structural gate and sibling context "
            "key on it)"
        )
    return GoalRecord(
        description=text,
        type="structural",
        status="incomplete",
        origin=origin,
        finding_signature=str(finding_signature or ""),
        associated_files=files,
    )
