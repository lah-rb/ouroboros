"""Polish phase — the consumer pass above the quality gate.

WHAT MAKES THIS DIFFERENT FROM quality_gate. The quality gate is a REVIEWER: it
reads the source, runs checks, and judges correctness. Polish hands the finished
product to someone who did not build it, cannot see the code, and was told only
what shipped with it. A reviewer finds what is broken; a consumer finds what is
unpleasant, confusing, or missing — the axis the 2026-08-22 frontier flight
scored us lowest on (imagination, felt play, craft).

Its findings are ordinary FUNCTIONAL goals (quality when there is no clean
interact re-test), so they ride the existing fix loop rather than needing a tier
of their own. Landing them also CLEARS quality_verified: nothing a consumer
asked for ships without the gate re-passing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.models import StepInput, StepOutput
from agent.persistence.models import GoalRecord

logger = logging.getLogger(__name__)


def _mission(step_input: StepInput) -> Any:
    return step_input.context.get("mission")


# ── flag setters ──────────────────────────────────────────────────────


async def action_mark_quality_verified(step_input: StepInput) -> StepOutput:
    """Record a quality-gate pass without finalizing the mission.

    Before the polish phase this transition went straight to `completed`. It
    now sets a flag and returns to the router, so a phase ABOVE the gate can
    run. At the default ceiling nothing else is reachable and the ladder
    exhausts to 'complete' on the next pass — same end state, one more cycle.
    """
    mission = _mission(step_input)
    if not mission:
        return StepOutput(result={"ok": False}, observations="No mission")
    mission.quality_verified = True
    return StepOutput(
        result={"quality_verified": True},
        observations="Quality gate passed — quality_verified set",
        context_updates={"mission": mission},
    )


# ── brief authoring ───────────────────────────────────────────────────


def _between(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    tail = text.split(start, 1)[1]
    return (tail.split(end, 1)[0] if end in tail else tail).strip()


async def action_split_consumer_brief(step_input: StepInput) -> StepOutput:
    """Split the authoring step's output into orientation and questionnaire.

    They are written in one call so the questions come from the same reading of
    the product as the orientation, but they go to DIFFERENT places: the
    orientation is all the consumer ever sees, and the questionnaire must not
    reach them until they have finished. Splitting here is what keeps the
    questions out of the session that is supposed to be uninfluenced by them.
    """
    raw = str(step_input.context.get("inference_response") or "")
    brief = _between(raw, "---ORIENTATION---", "---END ORIENTATION---")
    questions = _between(raw, "---QUESTIONNAIRE---", "---END QUESTIONNAIRE---")

    if not brief:
        # No orientation means nothing honest to put in front of a consumer.
        # Better to spend the entry than to run a session on a broken brief and
        # harvest findings from a confused one.
        return StepOutput(
            result={"ok": False},
            observations="No orientation in the brief — skipping the consumer run",
            context_updates={"consumer_brief": "", "questionnaire": ""},
        )
    return StepOutput(
        result={"ok": True, "has_questions": bool(questions)},
        observations=(
            f"Consumer brief ready ({len(brief)} chars)"
            + ("" if questions else " — no questionnaire, reflection only")
        ),
        context_updates={"consumer_brief": brief, "questionnaire": questions},
    )


# ── questionnaire queue ───────────────────────────────────────────────


def _questions(raw: Any) -> list[str]:
    """Questions out of whatever the authoring step produced.

    Accepts a JSON list, a JSON object with a `questions` key, or a plain
    newline/numbered list — the authoring step writes free-ish text and a
    strict parser here would turn a formatting wobble into a lost phase.
    """
    if isinstance(raw, list):
        return [str(q).strip() for q in raw if str(q).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    fenced = text
    if "```" in fenced:
        parts = fenced.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith(("[", "{")):
                fenced = candidate
                break
    try:
        parsed = json.loads(fenced)
        if isinstance(parsed, dict):
            parsed = parsed.get("questions", [])
        if isinstance(parsed, list):
            return [str(q).strip() for q in parsed if str(q).strip()]
    except (json.JSONDecodeError, TypeError):
        pass
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*").strip()
        while line[:2].rstrip(".)").isdigit() and line[:1].isdigit():
            line = line.split(".", 1)[-1].split(")", 1)[-1].strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


async def action_prepare_questionnaire(step_input: StepInput) -> StepOutput:
    """Build the question queue and expose the head.

    The canonical queue-loop shape (quality_gate's finding verification): the
    consumer sees ONE question per turn and never the list — a visible list
    invites answering them all at once in a single flattened paragraph.
    """
    questions = _questions(step_input.context.get("questionnaire"))
    updates: dict[str, Any] = {
        "question_queue": questions,
        "questionnaire_answers": [],
    }
    if questions:
        updates["question_current"] = questions[0]
    return StepOutput(
        result={"has_next": bool(questions), "count": len(questions)},
        observations=(
            f"Questionnaire: {len(questions)} question(s)"
            if questions
            else "Questionnaire produced no questions — skipping the report"
        ),
        context_updates=updates,
    )


async def action_record_answer(step_input: StepInput) -> StepOutput:
    """Pop the answered question, append the answer, expose the next head."""
    queue = list(step_input.context.get("question_queue") or [])
    answers = list(step_input.context.get("questionnaire_answers") or [])
    answer = str(step_input.context.get("inference_response") or "").strip()
    asked = queue.pop(0) if queue else ""
    if asked:
        answers.append({"question": asked, "answer": answer})
    updates: dict[str, Any] = {
        "question_queue": queue,
        "questionnaire_answers": answers,
    }
    if queue:
        updates["question_current"] = queue[0]
    return StepOutput(
        result={"has_next": bool(queue), "answered": len(answers)},
        observations=f"Recorded answer {len(answers)} ({len(queue)} remaining)",
        context_updates=updates,
    )


# ── harvest ───────────────────────────────────────────────────────────


def _findings(raw: Any) -> list[dict]:
    """Findings out of polish_conclude's output, tolerant of framing."""
    if isinstance(raw, list):
        return [f for f in raw if isinstance(f, dict)]
    text = str(raw or "").strip()
    if not text:
        return []
    if "```" in text:
        for part in text.split("```"):
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith(("[", "{")):
                text = candidate
                break
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(parsed, dict):
        parsed = parsed.get("findings", parsed.get("goals", []))
    return (
        [f for f in parsed if isinstance(f, dict)] if isinstance(parsed, list) else []
    )


async def action_harvest_polish_findings(step_input: StepInput) -> StepOutput:
    """Turn the consumer's experience into goals and book the entry.

    THE ENTRY IS BOOKED EITHER WAY. A consumer who wanted nothing changed is a
    real observation, not a failed run — booking only on findings would let an
    entirely satisfied pass re-enter forever. (This is deliberately NOT the
    vacuous-verification rule, which fails a gate that had zero CHECKABLE
    items; here the checkable item was the session, and it happened.)
    """
    from agent.actions.mission_actions import _quality_finding_signature

    mission = _mission(step_input)
    if not mission:
        return StepOutput(result={"done": True}, observations="No mission")

    # Prefer the TRIAGED findings: same findings, rephrased as target state
    # and each carrying a route. Falls back to the blind conclude output when
    # triage did not run (an older mission, or a flow set without the step) —
    # those are all "fix", which is exactly the pre-triage behaviour.
    triaged = step_input.context.get("triaged_findings")
    blind = _findings(step_input.context.get("polish_findings"))
    if triaged:
        findings = _findings(triaged)
    else:
        findings = blind
        if blind:
            # The fallback is CORRECT — never drop a consumer's findings
            # because a routing step failed — but it must not be quiet. On the
            # first live entry a formatter bug fed triage an empty list, it
            # truthfully reported "the user reported nothing", and this
            # fallback filed the untriaged complaints exactly as before. The
            # gate looked healthy from every angle: the step ran, it logged,
            # the goals landed. WARNING, because "triage ran and found
            # nothing" and "triage produced nothing usable" are the same shape
            # from outside, and only one of them is fine.
            logger.warning(
                "polish harvest: triage produced no routed findings but "
                "conclude reported %d — filing them UNTRIAGED (complaint "
                "phrasing, no design routing). Check the triage step.",
                len(blind),
            )
    designed = list(getattr(mission, "polish_designed", None) or [])
    directives: list[str] = []
    existing = {
        str(getattr(g, "finding_signature", "") or ""): g
        for g in getattr(mission, "goals", [])
        if getattr(g, "finding_signature", "")
    }

    created = reopened = skipped = 0
    for finding in findings:
        text = str(finding.get("description") or finding.get("finding") or "").strip()
        if not text:
            continue
        sig = "polish:" + (_quality_finding_signature(finding) or text.lower()[:120])
        if str(finding.get("route", "") or "").lower() == "design":
            # Routed to a design pass. It becomes goals in replan, against the
            # architecture, so no goal is filed here — but the signature IS
            # recorded. Without that the finding carries no goal to dedup
            # against and every later entry would route the same complaint to
            # design again, spending a replan per entry forever.
            if sig not in designed:
                designed.append(sig)
                directives.append(text)
            else:
                skipped += 1
            continue
        prior = existing.get(sig)
        if prior is not None:
            if getattr(prior, "status", "") == "complete":
                prior.status = "incomplete"
                reopened += 1
            else:
                skipped += 1
            continue
        # "quality" only when the consumer's complaint has no clean interact
        # re-test (a matter of taste or wording); everything else is ordinary
        # functional work and rides the interact loop like any other goal.
        cls = (
            "quality"
            if str(finding.get("class", "")).lower() == "quality"
            else "functional"
        )
        mission.goals.append(
            GoalRecord(
                description=text,
                type=cls,
                status="incomplete",
                origin="polish_gate",
                finding_signature=sig,
            )
        )
        existing[sig] = mission.goals[-1]
        created += 1

    if directives:
        # replan is FIRST in CODE_CORE_PHASES, so this is decomposed before
        # any other work is dispatched, and it appends goals rather than
        # replacing them. action_derive_directive_goals clears the directive,
        # so each of these decomposes exactly once.
        joined = "\n".join(f"- {d}" for d in directives)
        prior_directive = str(getattr(mission, "pending_directive", "") or "").strip()
        preamble = (
            "A person used the finished product and reported the following. "
            "Each line states what the product SHOULD do. Decompose them into "
            "goals against the existing architecture:"
        )
        mission.pending_directive = (
            f"{prior_directive}\n\n{preamble}\n{joined}"
            if prior_directive
            else f"{preamble}\n{joined}"
        )
    mission.polish_designed = designed

    mission.polish_entries = int(getattr(mission, "polish_entries", 0) or 0) + 1
    landed = created + reopened + len(directives)
    if landed:
        # Nothing a consumer asked for ships without the gate re-passing.
        mission.quality_verified = False

    allowed = int(
        getattr(getattr(mission, "config", None), "polish_max_entries", 1) or 1
    )
    return StepOutput(
        result={
            "created": created,
            "reopened": reopened,
            "designed": len(directives),
            "entry": mission.polish_entries,
        },
        observations=(
            f"Polish entry {mission.polish_entries}/{allowed}: "
            + (
                f"{created} new + {reopened} reopened goal(s)"
                f"{f', {len(directives)} to design' if directives else ''}"
                f"{f', {skipped} already open' if skipped else ''}"
                " — quality_verified cleared for re-gate"
                if landed
                else "the consumer asked for no changes"
            )
        ),
        context_updates={"mission": mission},
    )


_VALID_ROUTES = ("fix", "design")


async def action_route_polish_findings(step_input: StepInput) -> StepOutput:
    """Parse the triage verdict into routed findings.

    Runs after the FIRST project-aware step in the polish flow. Each finding
    comes back rephrased as the target state and carrying a route:

      "fix"    — one clear change; becomes a goal exactly as before.
      "design" — compound, structural, or contradicting the architecture;
                 goes to replan for decomposition against it.

    An absent, unknown or malformed route degrades to "fix", which is the
    pre-triage behaviour: a finding that fails to route is still work, and the
    fix loop is the path that has always carried it. Failing closed here would
    silently drop a consumer's complaint, which is the one outcome this gate
    exists to prevent.

    Context: inference_response.  Publishes: triaged_findings.
    """
    raw = step_input.context.get("inference_response")
    findings = _findings(raw)
    routed: list[dict] = []
    for finding in findings:
        text = str(finding.get("description") or finding.get("finding") or "").strip()
        if not text:
            continue
        route = str(finding.get("route", "") or "").strip().lower()
        if route not in _VALID_ROUTES:
            route = "fix"
        cls = (
            "quality"
            if str(finding.get("class", "")).lower() == "quality"
            else "functional"
        )
        routed.append({"description": text, "class": cls, "route": route})

    n_design = sum(1 for f in routed if f["route"] == "design")
    logger.info(
        "polish triage: %d finding(s) — %d fix, %d design",
        len(routed),
        len(routed) - n_design,
        n_design,
    )
    return StepOutput(
        result={"count": len(routed), "design": n_design},
        observations=(
            f"triage: {len(routed) - n_design} fix, {n_design} design"
            if routed
            else "triage: no findings to route"
        ),
        context_updates={"triaged_findings": routed},
    )
