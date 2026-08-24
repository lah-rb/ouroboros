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

    findings = _findings(step_input.context.get("polish_findings"))
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

    mission.polish_entries = int(getattr(mission, "polish_entries", 0) or 0) + 1
    landed = created + reopened
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
            "entry": mission.polish_entries,
        },
        observations=(
            f"Polish entry {mission.polish_entries}/{allowed}: "
            + (
                f"{created} new + {reopened} reopened goal(s)"
                f"{f', {skipped} already open' if skipped else ''}"
                " — quality_verified cleared for re-gate"
                if landed
                else "the consumer asked for no changes"
            )
        ),
        context_updates={"mission": mission},
    )
