#!/usr/bin/env python3
"""Debate-vs-CoT A/B for GUIDING artifacts (charters/goals, not code) — v2.

Hypothesis: for judgment-heavy planning outputs with no cheap ground truth,
an adversarial debate between two divergently-seeded personas produces a
better charter than best-of-N CoT.

v2 changes (after the confounded v1 run):
  - NO truncation: generous token budgets + a hard truncation-flag guard on
    every generation, so nothing silently clips (v1's single-CoT "win" was a
    truncation/seed artifact — best-of-N should never lose to one sample).
  - Baseline is best-of-N at THREE reasoning lengths (low/medium/high) via
    the reasoning head-swap (session path), each with its own impartial judge
    pick — takes luck out as the primary factor and gives debate a strong CoT
    field to beat.
  - MEASURE time + tokens per arm; do NOT force compute equal. Read the
    cost-quality frontier.

Four arms on ONE scenario:
  BoN-low / BoN-medium / BoN-high   N samples at that reasoning length, judged
  Debate                             two seats (simplicity vs robustness lean),
                                     adopt/shift/concede, rubric observer

Writes dev/debate_ab_result.json. Blind cross-arm ranking + judge-the-judge
are done by impartial Opus subagents, NOT by the gpt-oss under test.

Run against a BATCHED W>=3 server with reasoning_head_swap (gpt-oss-120b-a5-tau).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from agent.effects.inference import InferenceEffect, InferenceError  # noqa: E402
from agent.llm_json import parse_llm_json  # noqa: E402

ENDPOINT = os.environ.get("OURO_LLMVP", "http://localhost:8008/graphql")
OUT = os.path.join(_REPO, "dev", "debate_ab_result.json")

SCENARIO = (
    "Design an in-process job queue LIBRARY for a Python service. Application "
    "code submits jobs; a pool of workers executes them. Requirements: "
    "at-least-once execution with configurable retries and backoff; optional "
    "FIFO ordering within a named queue; backpressure when the queue is "
    "saturated; graceful shutdown that either drains or abandons in-flight "
    "work per policy. It is a LIBRARY (in-process, no external broker), used "
    "by other teams."
)

DELIVERABLE = (
    "Produce a build CHARTER — NOT code. Cover ALL of: (1) the key design "
    "decisions and the tradeoffs you resolved, with your reasoning; (2) the "
    "module/component breakdown; (3) the public API shape; (4) how each "
    "requirement (retries/backoff, ordering, backpressure, shutdown) is met — "
    "including the hard cases (crash-safety of at-least-once in-process, the "
    "ordering-vs-concurrency tension, backpressure semantics under "
    "saturation); (5) acceptance criteria a reviewer would check. Be concrete "
    "and defensible; finish every section."
)

_COT = f"{SCENARIO}\n\n{DELIVERABLE}\n\nThink through the tradeoffs, then produce the complete charter."

# No seeded stances (v3 finding: assigning "simplest" vs "robust" camps made
# the players defend positions rather than converge on the ideal — and the
# simplicity pole capitulated wholesale anyway). No "debate"/"opponent"
# framing either: those priors nudge toward winning over correctness. This is
# one mind dancing toward the optimal answer, framed as a game whose only
# path to victory is being right. The anti-gold-plating lever now lives in
# the WIN CONDITION itself, not in a judge rubric.
GOAL = (
    "This is a contest and your goal is to WIN. You win ONLY with the "
    "meaningfully BETTER idea: better grounded "
    "in logic, science, and sound engineering — not bigger, not more complete, "
    "not more feature-rich. No gold-plating, no scope creep; a simpler idea "
    "that is more correct beats an elaborate one that is not.\n"
    "- Argue in SHORT, specific, defensible claims — advance one or a few at a "
    "time, not a big up-front design. A small claim that holds beats a sweeping "
    "design that does not.\n"
    "- Cover the WHOLE problem: your position has to hold across EVERY aspect "
    "the problem demands — not just the points last raised or the ones in the "
    "opening. Actively surface any aspect relevant to a correct solution that "
    "has gone unexamined; an unaddressed requirement is a hole in your "
    "position, and leaving one unexamined can lose you the contest.\n"
    "- Do NOT concede a point unless it is actually beaten. Make every claim "
    "earn its ground.\n"
    "- You may shift to a stronger idea, or take your competitor's idea and "
    "make it meaningfully better, and claim the win — but only if it truly is "
    "better, not merely different or more elaborate.\n"
    "- Concede only when it is obvious your whole position is inferior; if you "
    "concede, begin your message with 'CONCEDE:'. Be concrete and tight."
)

DEBATER_TEMP = 1.0  # t*1.0 = the model's community-default base — "heated"
SYNTH_TEMP = 0.7  # post-synthesis charter, matched to the BoN charter temp
JUDGE_TEMP = 0.2  # stable referee
COT_TEMP = 0.7
# No-truncation budgets: high-reasoning CoT + a full 5-section charter must
# both fit. The truncation guard below fails loudly if these are still tight.
CHARTER_MAX = 8000
# Debate turns need room for medium-reasoning thinking + a full argument;
# 900 clipped every turn. Context growth is bounded by the OBSERVER window
# (below), not by turn size — the debater sessions grow in KV incrementally,
# which the engine handles; it was the observer's full-transcript re-prefill
# that stalled v2, and that's now windowed.
TURN_MAX = 4000  # competitive challenge+defend turns run longer than the
# v4 collaborative ones (2500 clipped them); the observer
# window bounds context growth regardless of turn size
JUDGE_MAX = 1600  # short-claims format gives the observer more distinct
# points to weigh per window; 800 clipped its vote in v7
OBSERVER_WINDOW = 4  # observer sees only the last N messages (2 rounds) —
# judging the CURRENT position, not re-prefilling the
# whole growing transcript (v2 wedged on that).

_truncations: list[str] = []  # any clipped generation lands here (guard)


class _TurnFailed(Exception):
    """A debate turn stalled/cancelled (watchdog) — degrade gracefully."""


def _tok(r):
    return int(
        getattr(r, "generated_tokens", 0) or getattr(r, "tokens_generated", 0) or 0
    )


async def _complete(fx, prompt, max_tokens, temp, tag):
    r = await fx.run_inference(prompt, {"temperature": temp, "max_tokens": max_tokens})
    if getattr(r, "truncated", False):
        _truncations.append(f"{tag} (stateless, {_tok(r)} tok)")
    return r.text or "", _tok(r)


async def _sturn(fx, sid, prompt, max_tokens, temp, tag, reasoning=None):
    cfg = {"temperature": temp, "max_tokens": max_tokens}
    if reasoning:
        cfg["reasoning"] = reasoning
    r = await fx.session_turn(sid, prompt, cfg)
    if getattr(r, "truncated", False):
        _truncations.append(f"{tag} (session, {_tok(r)} tok)")
    return r.text or "", _tok(r)


# ── Baseline: best-of-N at a given reasoning length ─────────────────────
async def arm_best_of_n(fx, level, n):
    """N independent single-turn sessions at reasoning=<level>, judge-pick.
    Reasoning length is controlled via the session head-swap path (stateless
    completions can't set reasoning)."""
    t0 = time.monotonic()
    tokens = 0
    samples = []
    for i in range(n):
        sid = await fx.start_session({"ttl_seconds": 1800})
        try:
            text, tok = await _sturn(
                fx,
                sid,
                _COT,
                CHARTER_MAX,
                COT_TEMP,
                f"BoN-{level}#{i}",
                reasoning=level,
            )
        finally:
            try:
                await fx.end_session(sid)
            except Exception:  # noqa: BLE001
                pass
        tokens += tok
        samples.append(text)
    listing = "\n\n".join(f"[CANDIDATE {i}]\n{s}" for i, s in enumerate(samples))
    jp = (
        f"{SCENARIO}\n\n{DELIVERABLE}\n\nHere are {n} candidate charters. As an "
        f"IMPARTIAL judge, pick the SINGLE most complete and defensible one — "
        f"reward correctness and coverage of the hard cases over confident "
        f"tone or length.\n\n{listing}\n\nReply with ONE JSON object: "
        f'{{"best": <candidate index>, "reason": "<one sentence>"}}'
    )
    jtext, jtok = await _complete(fx, jp, JUDGE_MAX, JUDGE_TEMP, f"judge-BoN-{level}")
    tokens += jtok
    j = parse_llm_json(jtext) or {}
    try:
        best = max(0, min(n - 1, int(j.get("best", 0))))
    except (TypeError, ValueError):
        best = 0
    return {
        "arm": f"BoN_{level}",
        "reasoning": level,
        "charter": samples[best],
        "tokens": tokens,
        "n": n,
        "picked": best,
        "judge_reason": str(j.get("reason", ""))[:200],
        "sample_lens": [len(s) for s in samples],
        "wall_s": round(time.monotonic() - t0, 1),
    }


# ── Conversation ────────────────────────────────────────────────────────
def _render(transcript):
    return "\n\n".join(
        f"--- {e['side']} (round {e['round']}) ---\n{e['text']}" for e in transcript
    )


def _log_transcript(transcript, winner, solution, converged, conceded, stalled):
    """Persist a readable, TIMESTAMPED transcript per run (never clobbered), so
    archetypes across prompt tweaks stay reviewable side by side."""
    d = os.path.join(_REPO, "dev", "debate_runs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{time.strftime('%Y%m%dT%H%M%S')}_conversation.md")
    lines = [
        f"# Conversation run {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"winner={winner} converged={converged} conceded={conceded} stalled={stalled}",
        f"debater_temp={DEBATER_TEMP} synth_temp={SYNTH_TEMP} judge_temp={JUDGE_TEMP}",
        "",
    ]
    for e in transcript:
        lines.append(f"## {e['side']} — round {e['round']}\n\n{e['text']}\n")
    if solution:
        lines.append(
            f"## WINNING SOLUTION (crystallized by side {winner})\n\n{solution}\n"
        )
    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(f"  transcript → {path}", flush=True)


async def _observe(fx, transcript, final=False):
    # Bound the prompt to the recent window — the judge assesses the CURRENT
    # standing, and re-prefilling the whole growing transcript every round is
    # what stalled/wedged v2. Final pass gets a slightly wider window.
    window = (
        transcript[-(OBSERVER_WINDOW + 2) :] if final else transcript[-OBSERVER_WINDOW:]
    )
    convo = _render(window)
    scope = "the recent exchange below" if final else "the exchange so far"
    p = (
        f"{SCENARIO}\n\nTwo people (A and B) are working toward the single most "
        f"OPTIMAL solution to this problem. Here is {scope}:\n\n{convo}\n\nYou "
        f"are an IMPARTIAL judge. Which position is currently the more optimal "
        f"and defensible solution — grounded in logic, science, and sound "
        f"engineering, with NO credit for gold-plating, scope creep, or "
        f"unverified claims? Score each, pick the stronger, and say whether "
        f"they have CONVERGED on essentially the same solution.\nReply with ONE "
        f'JSON object: {{"winner": "A" or "B", "score_a": <0-30>, '
        f'"score_b": <0-30>, "converged": true/false, "reason": "<one sentence>"}}'
    )
    text, tok = await _complete(fx, p, JUDGE_MAX, JUDGE_TEMP, "observer")
    data = parse_llm_json(text)
    if not isinstance(data, dict) or data.get("winner") not in ("A", "B"):
        return None, tok
    return {
        "winner": data["winner"],
        "score_a": data.get("score_a"),
        "score_b": data.get("score_b"),
        "converged": bool(data.get("converged")),
        "reason": str(data.get("reason", ""))[:200],
    }, tok


def _decide(votes, final_vote):
    last3 = [v["winner"] for v in votes[-3:]]
    maj = ("A" if last3.count("A") >= last3.count("B") else "B") if last3 else None
    if final_vote is None:
        return maj or "A", "majority (no final vote)"
    if maj is None or maj == final_vote["winner"]:
        return final_vote["winner"], "majority + holistic agree"
    return final_vote["winner"], f"holistic overrode majority ({maj})"


def _judge_note(vote, side):
    """Render the observer's last-round verdict FROM this debater's perspective,
    so the running score is VISIBLE to the debaters (visible-observer probe):
    does seeing 'you're behind' collapse the argument (capitulate to the
    referee) or fuel it (dig in on the flagged weakness)? '' before round 2."""
    if not vote:
        return ""
    sa, sb = vote.get("score_a"), vote.get("score_b")
    mine, theirs = (sa, sb) if side == "A" else (sb, sa)
    lead = vote.get("winner")
    if lead == side:
        standing = "rates YOUR position ahead"
    elif lead:
        standing = "rates your competitor's position ahead"
    else:
        standing = "has not called a clear leader"
    score = (
        f" ({mine} vs {theirs})"
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float))
        else ""
    )
    return (
        f"A neutral judge scores each round. Last round it {standing}{score}. "
        f"Its note: \"{vote.get('reason', '')}\".\n\n"
    )


def _winning_position(transcript, winner):
    """The winner's accumulated claims across the session (excluding any
    CONCEDE turn). FALLBACK consolidation only — used when the in-session
    crystallize call stalls; a fidelity review showed consolidating from these
    raw argumentative turns imports rejected competitor mechanisms and
    fabricates scaffolding, so crystallize is the primary path."""
    if not winner:
        return ""
    parts = [
        e["text"]
        for e in transcript
        if e["side"] == winner and not e["text"].strip().upper().startswith("CONCEDE")
    ]
    return "\n\n".join(p for p in parts if p.strip())


async def arm_debate(
    fx, max_rounds, end_eval=False, socratic=False, prompt_centered=False
):
    # end_eval=True (Option B): NO per-round observer, NO winner, NO crystallize.
    # The debate just runs its rounds, then ONE stateless eval reads the whole
    # conversation and writes the charter as it sees fit. Motivated by v9: the
    # per-turn observer was both miscalibrated (picked the losing charter) and
    # distorting when visible (a scoreboard the debaters game away from the hard
    # requirement). Removing it also removes those pathologies + ~22% of cost.
    # socratic=True: ASYMMETRIC Socratic method (elenchus) — A OWNS one evolving
    # design; B is a pure QUESTIONER that ONLY asks probing questions, never
    # attacks or proposes. A must ANSWER each question honestly and follow the
    # reasoning even when it proves A's OWN design wrong — the correction comes
    # from the proposer refuting THEMSELVES under questioning, not from an
    # adversary landing hits. Flips the proposer's motive from defend-to-win
    # (the theater every prior run showed) to answer-honestly-and-follow-the-logic.
    # Tests whether that surfaces the hard cases the proposers kept dodging.
    # Implies end_eval (no scoreboard; charter written from the whole examination).
    # prompt_centered (the user's actual intent, a Socratic sub-mode): the owner
    # owns THE PROMPT, not a build — under questioning it reshapes the PROBLEM
    # STATEMENT (requirements, scope, hard cases, assumptions) rather than
    # constructing/expanding a design. Nothing to gold-plate; the synthesizer gets
    # a sharpened PROBLEM as fuel and writes a LEAN charter.
    if prompt_centered:
        socratic = True
    if socratic:
        end_eval = True
    t0 = time.monotonic()
    tokens = 0
    sid_a = await fx.start_session({"ttl_seconds": 1800})
    sid_b = await fx.start_session({"ttl_seconds": 1800})
    sids = {"A": sid_a, "B": sid_b}
    transcript, votes, last_vote = [], [], None
    converged = conceded = stalled = False
    charter, solution, winner, basis, final_vote, rounds_used = (
        "",
        "",
        None,
        "",
        None,
        0,
    )
    try:
        # Openings.
        if socratic:
            # A OWNS the object under examination; B is a pure Socratic questioner.
            if prompt_centered:
                pa = (
                    f"{SCENARIO}\n\nYou OWN this problem STATEMENT. Your job is NOT "
                    f"to design a solution — it is to develop the sharpest possible "
                    f"UNDERSTANDING of the problem itself: what each requirement "
                    f"truly demands, where the genuinely hard parts are, what is in "
                    f"scope and what is out, and the assumptions any correct "
                    f"solution must respect. State that understanding; you will "
                    f"refine it under questioning. Do NOT propose a design."
                )
            else:
                pa = (
                    f"{SCENARIO}\n\nYou OWN this problem. Propose your design — the "
                    f"key components and how it meets each requirement. Be concrete "
                    f"but concise; you will examine and refine it through questioning."
                )
            text, tok = await _sturn(
                fx, sids["A"], pa, TURN_MAX, DEBATER_TEMP, "own-open"
            )
            tokens += tok
            transcript.append({"round": 0, "side": "A", "text": text})
            if prompt_centered:
                pb = (
                    f"{SCENARIO}\n\nAnother engineer framed their UNDERSTANDING of "
                    f"this problem:\n\n{text}\n\nYou are a Socratic questioner. Your "
                    f"role is NOT to propose a design and NOT to attack — it is to "
                    f"ASK the probing questions that test their understanding of the "
                    f"PROBLEM: 'what does that requirement actually require when…?', "
                    f"'is that case in scope?', 'what happens to that guarantee "
                    f"under…?', 'what are you assuming here?'. Ask a few sharp "
                    f"questions that expose fuzzy requirements or unexamined "
                    f"assumptions. Only ASK — do not answer, assert, or propose a "
                    f"design."
                )
            else:
                pb = (
                    f"{SCENARIO}\n\nYou are a Socratic questioner. Another engineer "
                    f"proposed this design:\n\n{text}\n\nYour role is NOT to attack "
                    f"or to propose an alternative — it is to ASK the probing "
                    f"questions that make the proposer examine their own reasoning: "
                    f"'what happens when…?', 'how does that hold if…?', 'why is that "
                    f"guaranteed?', 'what have you assumed here?'. Ask a few sharp, "
                    f"specific questions aimed at the shaky assumptions and the cases "
                    f"the design has not examined. Only ASK — do not answer them, "
                    f"assert conclusions, or propose a design of your own."
                )
            atk, tok = await _sturn(
                fx, sids["B"], pb, TURN_MAX, DEBATER_TEMP, "ask-open"
            )
            tokens += tok
            transcript.append({"round": 0, "side": "B", "text": atk})
        else:
            # Symmetric debate: no seeded stance, no charter awareness — just the
            # problem and the win-by-optimal-solution goal. Each states a position.
            for side in ("A", "B"):
                p = (
                    f"{SCENARIO}\n\n{GOAL}\n\nOpen the working session: state your "
                    f"core position briefly — lead with the key claim(s) you will "
                    f"defend and why. Do NOT lay out a full design yet."
                )
                text, tok = await _sturn(
                    fx, sids[side], p, TURN_MAX, DEBATER_TEMP, f"open-{side}"
                )
                tokens += tok
                transcript.append({"round": 0, "side": side, "text": text})

        for rnd in range(1, max_rounds + 1):
            rounds_used = rnd
            for side in ("A", "B"):
                opp = next(e["text"] for e in reversed(transcript) if e["side"] != side)
                if socratic and side == "A" and prompt_centered:
                    # Owner: reshape the PROBLEM STATEMENT under questioning; no build.
                    p = (
                        f"The questioner asks:\n\n{opp}\n\nAnswer honestly and use "
                        f"the answers to RESHAPE your understanding of the PROBLEM — "
                        f"sharpen what each requirement means, correct any assumption "
                        f"that does not hold, draw the scope line where it honestly "
                        f"falls (say plainly what is OUT of scope and why), and name "
                        f"the hard cases precisely. Do NOT design a solution or add "
                        f"machinery — refine the PROBLEM STATEMENT itself. Then "
                        f"restate your current understanding of the problem as it "
                        f"now stands. Keep it tight."
                    )
                elif socratic and side == "A":
                    # Owner: honestly ANSWER the questions, follow the reasoning
                    # even against yourself, revise the ONE design in place.
                    p = (
                        f"Your design is under examination. The questioner asks:"
                        f"\n\n{opp}\n\nAnswer each question HONESTLY and rigorously "
                        f"— follow the reasoning wherever it leads, even when it "
                        f"exposes a flaw in your OWN design. You are seeking the "
                        f"correct answer, NOT defending your design to win: if an "
                        f"honest answer shows the design is wrong or incomplete, "
                        f"say so plainly and revise it. Then restate your design "
                        f"as it now stands. Keep it tight."
                    )
                elif socratic and prompt_centered:
                    # Questioner (prompt-centered): probe the problem understanding.
                    p = (
                        f"The owner refined their understanding:\n\n{opp}\n\nAsk the "
                        f"next probing questions — press where the understanding is "
                        f"still fuzzy, an assumption is unexamined, a scope line is "
                        f"unclear, or a requirement's hard case has not been pinned "
                        f"down. Only ASK — do not attack, assert, or propose a "
                        f"design. Sharp and specific."
                    )
                elif socratic:
                    # Questioner: only ASK, never attack or propose.
                    p = (
                        f"The proposer answered and revised:\n\n{opp}\n\nAsk the "
                        f"next probing questions — press where an answer was "
                        f"evasive or hand-wavy, or where it revealed a new "
                        f"assumption, and question any aspect of the problem still "
                        f"unexamined. Only ASK — do not attack, assert "
                        f"conclusions, or propose a design. Sharp and specific."
                    )
                else:
                    p = (
                        f"{_judge_note(last_vote, side)}Your competitor's latest "
                        f"position:\n\n{opp}\n\nAdvance "
                        f"the contest with SHORT, specific claims — engage or raise "
                        f"one or a few points, not a full redesign. Do NOT just "
                        f"react to the last message: if an aspect of the problem "
                        f"that matters to a correct solution has not been examined "
                        f"yet, raise it. Defend or sharpen your own position; "
                        f"adopt-and-improve or shift ONLY if the result is "
                        f"meaningfully better. Do not concede a point that is not "
                        f"actually beaten; concede your whole position only if it "
                        f"is obviously inferior."
                    )
                text, tok = await _sturn(
                    fx, sids[side], p, TURN_MAX, DEBATER_TEMP, f"r{rnd}-{side}"
                )
                tokens += tok
                transcript.append({"round": rnd, "side": side, "text": text})
                # In Socratic mode the interrogation always runs full rounds — the
                # owner revising a point is not a whole-position concession.
                if not socratic and text.strip().upper().startswith("CONCEDE"):
                    conceded = True
            if not end_eval:
                vote, tok = await _observe(fx, transcript)
                tokens += tok
                if vote is not None:
                    votes.append(vote)
                    last_vote = vote  # made visible to both debaters next round
                    if vote["converged"]:
                        converged = True
            if converged or conceded:
                break

        if not end_eval:
            final_vote, tok = await _observe(fx, transcript, final=True)
            tokens += tok
            winner, basis = _decide(votes, final_vote)
            # Winner crystallizes its definitive design in-session (full memory).
            # A fidelity review (2026-07-13) showed this tracks the CONVERGED
            # design far better than consolidating from raw argumentative turns —
            # the latter imported a mechanism the winner had REJECTED (present
            # only as a quoted attack) and fabricated scaffolding to fill gaps.
            # Also ask for the rejected alternatives, to keep the deliberation
            # texture a bare restatement would otherwise erase.
            cp = (
                "The session is complete and your position prevailed. State "
                "your COMPLETE, definitive design for the problem — the full "
                "solution you stake the win on, with the reasoning behind each "
                "choice. Then briefly note the main alternatives you REJECTED "
                "and why. Include only mechanisms YOU endorse."
            )
            solution, tok = await _sturn(
                fx, sids[winner], cp, CHARTER_MAX, SYNTH_TEMP, "crystallize"
            )
            tokens += tok
        else:
            if socratic and prompt_centered:
                basis = "prompt-centered socratic (owner reshapes the PROBLEM) + lean end-eval"
            elif socratic:
                basis = (
                    "socratic questioning (owner answers a pure questioner) + end-eval"
                )
            else:
                basis = "single end-eval over full conversation (no per-round judge)"
    except InferenceError as exc:
        # A turn stalled/was cancelled by the watchdog. Degrade gracefully:
        # decide from whatever we have — never crash the run or wedge on more
        # calls. Fall back to the winner's assembled turns only if crystallize
        # never produced a solution (lower fidelity, but the run always finishes).
        stalled = True
        print(
            f"  ⚠️ conversation turn stalled ({exc}) — degrading gracefully", flush=True
        )
        if not end_eval:
            if winner is None:
                winner, basis = _decide(votes, final_vote)
            if not solution.strip() and winner is not None:
                solution = _winning_position(transcript, winner)
    finally:
        for sid in sids.values():
            try:
                await fx.end_session(sid)
            except Exception:  # noqa: BLE001
                pass

    # POST-synthesis: a fresh, stateless call writes the ranked charter (the
    # debaters never knew about charters). Matched to the BoN charters in temp
    # and format.
    if end_eval:
        # Read the WHOLE session, write the charter as it sees fit.
        convo = _render(transcript)
        if socratic and prompt_centered:
            # v12 lean synthesizer, RESTORED VERBATIM (v16 = v12 rerun to test
            # reproducibility of v12's 2nd place given the loop-variance confound).
            sp = (
                f"{SCENARIO}\n\n{DELIVERABLE}\n\nBelow, engineers examined this "
                f"PROBLEM through Socratic questioning — sharpening what each "
                f"requirement means, where the hard cases lie, and what is "
                f"honestly in and out of scope. Use this refined understanding as "
                f"your fuel. Write a LEAN, honest build charter: solve exactly "
                f"what the requirements demand and NO more. Scope limitations "
                f"honestly (state what is out of scope and why). Do NOT "
                f"over-engineer, add unrequested machinery, or gold-plate.\n\n"
                f"=== EXAMINATION ===\n{convo}\n=== END EXAMINATION ===\n\n"
                f"Write the charter."
            )
        elif socratic:
            sp = (
                f"{SCENARIO}\n\n{DELIVERABLE}\n\nBelow, a design owner proposed a "
                f"solution and examined it through Socratic questioning — "
                f"answering probing questions and revising their OWN design as "
                f"honest answers exposed flaws. Using the full examination as "
                f"context — what held under questioning, what the proposer "
                f"corrected in themselves, and what remains a genuine limitation "
                f"— write the build charter for the examined design. Be honest "
                f"about weaknesses the questioning surfaced.\n\n=== SESSION ==="
                f"\n{convo}\n=== END SESSION ===\n\nWrite the charter."
            )
        else:
            # Option B: two symmetric positions — synthesize freely, no winner.
            sp = (
                f"{SCENARIO}\n\n{DELIVERABLE}\n\nTwo engineers argued toward the "
                f"best design in the working session below. Use it as context — "
                f"weigh both positions and the objections each raised — and write "
                f"the build charter as YOU judge best. You are NOT bound to either "
                f"engineer's framing or to picking a side: take what is correct, "
                f"discard what is not, and resolve the open questions yourself.\n\n"
                f"=== SESSION ===\n{convo}\n=== END SESSION ===\n\nWrite the charter."
            )
        charter, tok = await _complete(
            fx, sp, CHARTER_MAX, SYNTH_TEMP, "end-eval-charter"
        )
        tokens += tok
    elif solution.strip():
        sp = (
            f"{SCENARIO}\n\n{DELIVERABLE}\n\nAn expert working session settled "
            f"on the following as the strongest solution:\n\n{solution}\n\n"
            f"Write the build charter for THIS solution — capture its design "
            f"decisions and the rejected alternatives it notes."
        )
        charter, tok = await _complete(fx, sp, CHARTER_MAX, SYNTH_TEMP, "synth-charter")
        tokens += tok

    _log_transcript(transcript, winner, solution, converged, conceded, stalled)
    return {
        "arm": "B_debate",
        "charter": charter,
        "solution": solution,
        "tokens": tokens,
        "rounds": rounds_used,
        "winner": winner,
        "decision_basis": basis,
        "converged": converged,
        "conceded": conceded,
        "stalled": stalled,
        "votes": votes,
        "final_vote": final_vote,
        "transcript": transcript,
        "wall_s": round(time.monotonic() - t0, 1),
    }


def _save(arms, n):
    """Write results after EACH arm — a later arm's failure must never lose
    the completed ones (the v2 bug: all-at-end write lost 3 clean arms)."""
    with open(OUT, "w") as f:
        json.dump(
            {"scenario": SCENARIO, "deliverable": DELIVERABLE, "n": n, "arms": arms},
            f,
            indent=1,
        )


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    max_rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    debate_only = "debate-only" in sys.argv[3:]
    end_eval = "end-eval" in sys.argv[3:]
    socratic = "socratic" in sys.argv[3:]
    prompt_centered = "prompt-socratic" in sys.argv[3:]
    fx = InferenceEffect(endpoint=ENDPOINT, model_default_temperature=0.7)

    arms = []
    if debate_only:
        # Reuse the already-clean BoN arms on disk; rerun ONLY the debate.
        prior = json.load(open(OUT))
        n = prior.get("n", n)
        arms = [a for a in prior["arms"] if not a["arm"].startswith("B_")]
        print(f"debate-only: reusing {len(arms)} saved BoN arms", flush=True)

    try:
        if not debate_only:
            for level in ("low", "medium", "high"):
                print(f"── BoN-{level} (N={n}) ──", flush=True)
                a = await arm_best_of_n(fx, level, n)
                arms.append(a)
                _save(arms, n)  # bank it immediately
                print(
                    json.dumps(
                        {k: a[k] for k in ("arm", "tokens", "picked", "wall_s")}
                    ),
                    flush=True,
                )

        print("── B debate ──", flush=True)
        b = await arm_debate(
            fx,
            max_rounds,
            end_eval=end_eval,
            socratic=socratic,
            prompt_centered=prompt_centered,
        )
        arms.append(b)
        _save(arms, n)
        print(
            json.dumps(
                {
                    k: b[k]
                    for k in (
                        "arm",
                        "tokens",
                        "rounds",
                        "winner",
                        "converged",
                        "conceded",
                        "stalled",
                        "decision_basis",
                        "wall_s",
                    )
                }
            ),
            flush=True,
        )

        print(f"\nwrote {OUT}", flush=True)
        print(
            json.dumps(
                {
                    "cost": {
                        a["arm"]: {"tokens": a["tokens"], "wall_s": a["wall_s"]}
                        for a in arms
                    },
                    "B_winner": b["winner"],
                    "B_rounds": b["rounds"],
                    "B_converged": b["converged"],
                    "B_conceded": b["conceded"],
                    "TRUNCATIONS": _truncations or "none (clean)",
                },
                indent=1,
            ),
            flush=True,
        )
        if _truncations:
            print(
                "\n⚠️  TRUNCATION DETECTED — budgets still too small; result confounded.",
                file=sys.stderr,
                flush=True,
            )
    finally:
        await fx.close()


if __name__ == "__main__":
    asyncio.run(main())
