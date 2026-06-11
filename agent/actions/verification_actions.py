"""Verify-before-harvest: gate findings are claims, checked before they cost cycles.

Measured on the first completed game_challenge run, ~35% of quality-gate
findings were false claims — features reported broken/untested that a
re-run would have shown working. Each false finding cost ~3 dispatches
(diagnose -> blind file edit -> play-test) and the blind edits on working
files caused real regressions. These actions drive a probe loop inside
the quality gate: each functional finding with a repro is re-run against
the live program (via the run_commands sub-flow), a judge turn reads the
transcript, and only claims that survive reach the harvester. The gate
verdict is DERIVED from the survivors, which also couples the verdict to
the findings list (previously a model assertion that could contradict it).

Fail-safe doctrine: infrastructure failures (probe didn't run, judge
unparseable) KEEP the claim — degrading to pre-feature behavior is always
preferred over silently dropping a possible defect.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Probe budget: findings beyond this pass through unverified (tagged) —
# spending dispatches on an unverified claim beats dropping it silently.
MAX_VERIFY_FINDINGS = 8
# Repro lines beyond this are truncated (mirrors _normalize_repro's cap).
MAX_REPRO_COMMANDS = 10
# Transcript tail preserved as evidence on each finding.
_EVIDENCE_TAIL_CHARS = 500


def _finding_text(task: Any) -> str:
    if isinstance(task, dict):
        return str(task.get("issue") or task.get("description") or "")
    return str(task or "")


def _usable_repro(task: Any, *launch_commands: str) -> list[str]:
    """Extract the probe-ready repro from a finding, or [] if unusable.

    Strips a leading line that duplicates a launch command (the prompt
    forbids it, but models include it anyway) and re-applies the length cap.
    """
    if not isinstance(task, dict):
        return []
    repro = task.get("repro")
    if not isinstance(repro, list):
        return []
    lines = [str(ln).strip() for ln in repro if str(ln).strip()]
    launches = {lc.strip() for lc in launch_commands if lc and lc.strip()}
    if lines and lines[0] in launches:
        lines = lines[1:]
    if len(lines) > MAX_REPRO_COMMANDS:
        logger.warning(
            "Probe repro truncated from %d to %d lines", len(lines), MAX_REPRO_COMMANDS
        )
        lines = lines[:MAX_REPRO_COMMANDS]
    return lines


def _probe_launch(step_input: StepInput) -> str:
    """The command that launches the program for a probe.

    With the run/smoke contract split (architecture declares both),
    run_command is the plain interactive launch — use it directly. For
    pre-contract architectures run_command was overloaded with the
    piped startup form (`printf "quit\n" | python main.py`, which
    self-terminates and leaves repro lines answering to the bare
    shell), detectable because the effective smoke command equals it —
    there, prefer the UX session's captured launch.
    """
    run = str(step_input.params.get("run_command") or "").strip()
    smoke = str(step_input.params.get("smoke_command") or "").strip()
    ux = str(step_input.params.get("ux_launch_command") or "").strip()
    if run and smoke and smoke != run:
        return run
    return ux or run


def _probe_keys(task: dict, launch: str, run_command: str) -> dict:
    """Context keys consumed by the run_probe and judge_finding steps."""
    repro = _usable_repro(task, launch, run_command)
    return {
        "probe_commands": [launch] + repro,
        "probe_launch": launch,
        "probe_claim": _finding_text(task),
        "probe_expected": str(task.get("expected") or ""),
        "probe_repro_block": "\n".join(
            f"  {i}. {line}" for i, line in enumerate(repro, 1)
        ),
    }


async def action_prepare_finding_verification(step_input: StepInput) -> StepOutput:
    """Partition gate findings into a probe queue and pass-throughs.

    - quality-class findings: no behavioral repro is possible — pass
      through tagged ``not-applicable``.
    - functional findings with a usable repro (and a known run command):
      queued for probing, up to MAX_VERIFY_FINDINGS.
    - functional findings without a repro: governed by
      ``params.no_repro_policy`` — ``permissive`` (default) passes them
      through tagged ``unverified-no-repro``; ``strict`` refutes them
      (note-only, never a goal).

    Also snapshots the gate's UX transcript (``terminal_output``) — the
    probe sub-flow republishes that key, and apply_verification_results
    restores it for the gate's returns.

    Result: ``has_next`` (+ queue stats)
    Publishes: verification_queue, passthrough_tasks, verified_findings,
               refuted_findings, gate_terminal_output, probe_* keys
    """
    quality_results = step_input.context.get("quality_results") or {}
    fix_tasks = (
        (quality_results.get("fix_tasks") or [])
        if isinstance(quality_results, dict)
        else []
    )
    run_command = str(step_input.params.get("run_command") or "").strip()
    launch = _probe_launch(step_input)
    policy = str(step_input.params.get("no_repro_policy") or "permissive").lower()

    queue: list[dict] = []
    passthrough: list[dict] = []
    refuted: list[dict] = []

    for task in fix_tasks:
        if not isinstance(task, dict):
            task = {"issue": _finding_text(task), "description": _finding_text(task)}
        cls = task.get("class", "functional")
        if cls == "quality":
            passthrough.append({**task, "verification": "not-applicable"})
            continue
        repro = _usable_repro(task, launch, run_command)
        if not repro or not launch:
            if policy == "strict":
                refuted.append(
                    {
                        **task,
                        "verification": "refuted",
                        "verification_evidence": "no repro provided — strict policy",
                    }
                )
            else:
                passthrough.append({**task, "verification": "unverified-no-repro"})
            continue
        if len(queue) >= MAX_VERIFY_FINDINGS:
            logger.warning(
                "Probe queue full (%d) — passing through unverified: %s",
                MAX_VERIFY_FINDINGS,
                _finding_text(task)[:80],
            )
            passthrough.append({**task, "verification": "unverified-cap"})
            continue
        queue.append(task)

    context_updates: dict[str, Any] = {
        "verification_queue": queue,
        "passthrough_tasks": passthrough,
        "verified_findings": [],
        "refuted_findings": refuted,
        "gate_terminal_output": step_input.context.get("terminal_output", ""),
    }
    if queue:
        context_updates.update(_probe_keys(queue[0], launch, run_command))

    return StepOutput(
        result={
            "has_next": bool(queue),
            "queued": len(queue),
            "passthrough": len(passthrough),
            "refuted_no_repro": len(refuted),
        },
        observations=(
            f"Finding verification: {len(queue)} claim(s) to probe, "
            f"{len(passthrough)} pass-through, {len(refuted)} refuted by policy"
        ),
        context_updates=context_updates,
    )


async def action_record_finding_verification(step_input: StepInput) -> StepOutput:
    """Record the head finding's probe outcome and advance the queue.

    Dispositions (head of ``verification_queue``):
    - ``params.probe_failed`` (PTY no-start / judge produced nothing):
      kept, tagged ``inconclusive`` — infra failure is not refutation.
    - judge JSON ``{"confirmed": true|false, "reason": ...}``:
      confirmed -> verified_findings (+ evidence); refuted ->
      refuted_findings (+ evidence).
    - judge unparseable or missing ``confirmed``: kept, tagged
      ``judge-unparseable``.

    Result: ``has_next``, ``confirmed``
    Publishes: verification_queue, verified_findings, refuted_findings,
               probe_* keys for the next finding (when has_next)
    """
    from agent.llm_json import parse_llm_json

    queue = list(step_input.context.get("verification_queue") or [])
    verified = list(step_input.context.get("verified_findings") or [])
    refuted = list(step_input.context.get("refuted_findings") or [])
    transcript = str(step_input.context.get("terminal_output") or "")
    run_command = str(step_input.params.get("run_command") or "").strip()
    launch = _probe_launch(step_input)
    probe_failed = bool(step_input.params.get("probe_failed", False))

    if not queue:
        return StepOutput(
            result={"has_next": False, "confirmed": None},
            observations="Verification queue empty — nothing to record",
        )

    task = queue.pop(0)
    evidence_tail = transcript[-_EVIDENCE_TAIL_CHARS:] if transcript else ""
    confirmed: bool | None = None

    if probe_failed:
        verified.append(
            {
                **task,
                "verification": "inconclusive",
                "verification_evidence": "probe could not run or judge gave no answer",
            }
        )
        disposition = "inconclusive (probe failed) — claim kept"
    else:
        parsed = parse_llm_json(str(step_input.context.get("inference_response", "")))
        verdict = parsed.get("confirmed") if isinstance(parsed, dict) else None
        reason = (str(parsed.get("reason") or "") if isinstance(parsed, dict) else "")[
            :300
        ]
        if not isinstance(verdict, bool):
            verified.append(
                {
                    **task,
                    "verification": "judge-unparseable",
                    "verification_evidence": evidence_tail,
                }
            )
            disposition = "judge unparseable — claim kept"
        elif verdict:
            confirmed = True
            verified.append(
                {
                    **task,
                    "verification": "confirmed",
                    "verification_evidence": (
                        f"{reason}\n--- probe transcript tail ---\n{evidence_tail}"
                    ).strip(),
                }
            )
            disposition = f"CONFIRMED — {reason[:80]}"
        else:
            confirmed = False
            refuted.append(
                {
                    **task,
                    "verification": "refuted",
                    "verification_evidence": (
                        f"{reason}\n--- probe transcript tail ---\n{evidence_tail}"
                    ).strip(),
                }
            )
            disposition = f"REFUTED — {reason[:80]}"

    context_updates: dict[str, Any] = {
        "verification_queue": queue,
        "verified_findings": verified,
        "refuted_findings": refuted,
    }
    if queue:
        context_updates.update(_probe_keys(queue[0], launch, run_command))

    return StepOutput(
        result={"has_next": bool(queue), "confirmed": confirmed},
        observations=(
            f"Probe verdict for '{_finding_text(task)[:60]}': {disposition} "
            f"({len(queue)} remaining)"
        ),
        context_updates=context_updates,
    )


async def action_apply_verification_results(step_input: StepInput) -> StepOutput:
    """Rebuild quality_results from surviving claims; derive the verdict.

    fix_tasks = confirmed/kept findings + pass-throughs; ``all_passing``
    is derived purely from that list — the model's earlier verdict is
    overridden in BOTH directions (a "pass" with a confirmed defect
    fails; a "fail" whose every claim was refuted passes). Refuted
    claims become mission notes (telemetry for the gate's false-claim
    rate), never goals. Restores the gate's UX transcript snapshot so
    the flow's returns see the session evidence, not the last probe.

    Result: ``all_passing``, counts
    Publishes: quality_results, terminal_output
    """
    effects = step_input.effects
    quality_results = step_input.context.get("quality_results") or {}
    verified = list(step_input.context.get("verified_findings") or [])
    passthrough = list(step_input.context.get("passthrough_tasks") or [])
    refuted = list(step_input.context.get("refuted_findings") or [])

    fix_tasks = verified + passthrough
    all_passing = len(fix_tasks) == 0
    confirmed_count = sum(1 for t in verified if t.get("verification") == "confirmed")

    if effects:
        for task in fix_tasks:
            issue_text = _finding_text(task)
            if not issue_text:
                continue
            tag = task.get("verification", "")
            note = f"Quality gate issue: {issue_text}"
            if tag:
                note += f" [{tag}]"
            target_file = task.get("file", "") if isinstance(task, dict) else ""
            if target_file:
                note += f" (file: {target_file})"
            await effects.push_note(
                content=note,
                category="failure_analysis",
                tags=[target_file] if target_file else [],
                source_flow="quality_gate",
            )
        for task in refuted:
            reason = str(task.get("verification_evidence") or "").split(
                "\n--- probe transcript tail ---"
            )[0]
            await effects.push_note(
                content=(
                    f"Quality gate claimed: {_finding_text(task)} — "
                    f"probe REFUTED it: {reason or 'behavior worked when re-run'}"
                ),
                category="failure_analysis",
                tags=["refuted-finding"],
                source_flow="quality_gate",
            )

    base_summary = (
        str(quality_results.get("summary") or "")
        if isinstance(quality_results, dict)
        else ""
    )
    summary = (
        f"{base_summary} (verified: {confirmed_count} confirmed, "
        f"{len(refuted)} refuted, {len(passthrough)} passthrough)"
    ).strip()

    return StepOutput(
        result={
            "all_passing": all_passing,
            "confirmed": confirmed_count,
            "refuted": len(refuted),
            "passthrough": len(passthrough),
        },
        observations=(
            f"Verification: {'PASS' if all_passing else 'FAIL'} derived from "
            f"{len(fix_tasks)} surviving claim(s) ({len(refuted)} refuted)"
        ),
        context_updates={
            "quality_results": {
                "all_passing": all_passing,
                "summary": summary,
                "fix_tasks": fix_tasks,
                # quality_gate.cue's returns read these keys; before this
                # action they never existed on quality_results.
                "verdict": "pass" if all_passing else "fail",
                "issues": [_finding_text(t) for t in fix_tasks],
            },
            "terminal_output": step_input.context.get("gate_terminal_output", ""),
        },
    )
