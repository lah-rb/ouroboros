"""Ops flow set — a single terminal task, worked until done.

The ops mission promotes `run_session` to a mission type: the objective IS
the task statement; the agent drives a terminal to accomplish it and judges
completion against a "definition of done" — a list of observable shell checks
derived once up front (terminal-bench grades by final state, so the agent's
self-checks mirror the grader's shape).

Composed: the done-checker is `action_run_validation_checks` (reused
verbatim); the criteria/check/judge pipeline mirrors the quality gate's
plan_checks → execute_checks → summarize.
"""

from __future__ import annotations

import logging
import re

from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput


def _record_workspace_ledger(
    mission, step_input, *, session_status: str, session_desc: str
) -> None:
    """Append durable workspace effects to the mission ledger (best-effort).

    Deterministic provision entries from ``setup_results`` (one per setup action
    that did something) plus one coarse per-cycle ``session`` entry from the judge.
    Successful provisions are de-duped against prior entries so a cycle that
    re-reports the same install doesn't bloat the ledger; the window is bounded.
    """
    from agent.persistence.models import WorkspaceLedgerEntry

    led = getattr(mission, "workspace_ledger", None)
    if led is None:  # non-ops mission or old state — nothing to record onto
        return
    cycle = int(step_input.context.get("cycle", 0) or 0)
    seen = {(e.kind, e.description) for e in led}
    for r in step_input.context.get("setup_results") or []:
        desc = str(r.get("name", "")).strip()
        if not desc:
            continue
        status = (
            "success"
            if r.get("passed")
            else ("skipped" if r.get("skipped") else "failed")
        )
        if ("provision", desc) in seen and status != "failed":
            continue  # already have a non-failed provision of this
        led.append(
            WorkspaceLedgerEntry(
                cycle=cycle, kind="provision", description=desc, status=status
            )
        )
        seen.add(("provision", desc))
    if session_desc:
        led.append(
            WorkspaceLedgerEntry(
                cycle=cycle,
                kind="session",
                description=session_desc[:200],
                status=session_status,
            )
        )
    if len(led) > 60:  # bound growth — keep the most recent
        del led[:-60]

logger = logging.getLogger(__name__)

TASK_GOAL_SIGNATURE = "ops-task"

# ── asym-probe applicability gate ─────────────────────────────────────────
# The asym-probe (a property-based differential test) only makes sense for
# rule-inference tasks: implement a function/callable whose behaviour is pinned
# by WORKED EXAMPLES that may under-determine the rule (grid-pattern-transform is
# the archetype — its symmetric example hides rot90-vs-fliplr). Everywhere else
# (install/extract/bucket, or a plain deterministic conversion with no example
# ambiguity) the probe adds an inference for no signal, so it stays gated OFF.
_SOLVER_FN_RE = re.compile(
    r"(?i)\b(implement|complete|write)\b.{0,80}?\b(function|method|solver?)\b"
    r"|\bdef\s+\w+\s*\(|\b\w+\s*\([^)]*\)\s*(?:->|:)\s*"  # a def / typed signature
)
_EXAMPLE_RE = re.compile(r"(?i)\bexamples?\b|input.{0,8}?output|=>|->")


async def action_detect_solver_task(step_input: StepInput) -> StepOutput:
    """Gate the asym-probe. Fire (run_probe=True) iff ALL hold:
      - the objective asks to implement a callable AND shows worked examples
        (the rule-inference shape the probe disambiguates),
      - the deterministic completion checks already pass (a COMPLETE candidate —
        don't waste the probe on a half-written file).
    RE-VERIFY each complete candidate (not once): a wrong first solution gets a
    property counterexample as feedback, and the NEXT candidate is re-probed so a
    still-wrong fix can't be certified unverified (the single-shot version let
    that through). The extra probe turn is cheap now that swa_full keeps the
    static prefill cached. Deterministic — zero inference; incomplete/non-eligible
    cycles still pay nothing.
    """
    mission = step_input.context.get("mission")
    obj = str(getattr(mission, "objective", "") or "") if mission else ""
    is_solver = bool(_SOLVER_FN_RE.search(obj) and _EXAMPLE_RE.search(obj))

    results = step_input.context.get("validation_results") or []
    checks_passed = bool(results) and all(
        r.get("passed") for r in results if r.get("required", True)
    )

    run_probe = is_solver and checks_passed
    return StepOutput(
        result={"run_probe": run_probe, "is_solver_task": is_solver},
        observations=(
            "asym-probe firing (complete candidate — re-verify)"
            if run_probe
            else f"asym-probe skipped (solver={is_solver}, checks_passed={checks_passed})"
        ),
    )


def _strip_probe_fence(text: str) -> str:
    """Pull a python script out of a fenced block if the model fenced it."""
    s = (text or "").strip()
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", s, re.DOTALL)
    return (m.group(1) if m else s).strip()


async def action_run_property_probe(step_input: StepInput) -> StepOutput:
    """Write the generated property test into the working directory, run it,
    and APPEND its pass/fail as a required validation_result so the existing
    judge/decide loop treats a property violation as 'not done' and loops with
    the discrepancy as feedback. Self-contained — no oracle, just the spec's
    own invariants checked on discriminating inputs.

    Context: inference_response (the generated test), working_directory,
    validation_results (the checks so far, appended to).
    """
    effects = step_input.effects
    wd = step_input.params.get("working_directory") or "."
    test_src = _strip_probe_fence(str(step_input.context.get("inference_response", "")))
    results = list(step_input.context.get("validation_results") or [])
    updates: dict = {"validation_results": results}

    if not test_src or effects is None:
        # No usable probe — don't manufacture a failure; leave checks unchanged.
        return StepOutput(
            result={"probe_passed": True, "probe_ran": False},
            observations="asym-probe produced no test — skipped",
            context_updates=updates,
        )

    test_name = "asym_probe_test.py"
    detail = ""
    try:
        await effects.write_file(test_name, test_src)
        # Run from the working dir so the script's own directory is the import
        # root (`import grid_transform` resolves to the candidate).
        res = await effects.run_command(["python3", test_name], working_dir=wd, timeout=30)
        passed = res.return_code == 0
        detail = ((res.stdout or "") + (res.stderr or "")).strip()
        # Clean up so the probe artifact never pollutes the graded workspace.
        await effects.run_command(["rm", "-f", test_name], working_dir=wd, timeout=10)
    except Exception as exc:  # never crash the cycle on a probe error
        return StepOutput(
            result={"probe_passed": True, "probe_ran": False},
            observations=f"asym-probe error ({exc}) — skipped",
            context_updates=updates,
        )

    results.append(
        {
            "name": "asym_property_probe",
            "command": f"python3 {test_name}",
            "passed": passed,
            "required": True,
            "stdout": (res.stdout or "")[:500],
            "stderr": (res.stderr or "")[:500],
            "return_code": res.return_code,
        }
    )
    updates["validation_results"] = results
    return StepOutput(
        result={"probe_passed": passed, "probe_ran": True},
        observations=f"asym-probe {'PASS' if passed else 'FAIL'}: {detail[:160]}",
        context_updates=updates,
    )


async def action_derive_task_goal(step_input: StepInput) -> StepOutput:
    """Bootstrap the single task goal + TaskState from the objective (idempotent).

    The objective is the task statement — no planning turn. Mirrors
    derive_extraction_goals' signature-idempotency. The definition-of-done is
    NOT derived here: it is derived grounded inside ops_task (post-scan,
    post-exploration) via the reground path.

    Context: mission
    Result: goals_ready, created
    Publishes: mission
    """
    from agent.persistence.models import GoalRecord, TaskState

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"goals_ready": False}, observations="No mission")

    task_spec = str(getattr(mission, "objective", "") or "").strip()
    if not task_spec:
        return StepOutput(
            result={"goals_ready": False},
            observations="No objective — nothing for the ops flow set to do",
        )

    if getattr(mission, "task_definition", None) is None:
        mission.task_definition = TaskState(task_spec=task_spec)

    created = 0
    if not any(
        getattr(g, "finding_signature", "") == TASK_GOAL_SIGNATURE
        for g in mission.goals
    ):
        mission.goals.append(
            GoalRecord(
                description=f"Accomplish: {task_spec}",
                type="task_exec",
                status="incomplete",
                interaction_mode="exploratory",
                finding_signature=TASK_GOAL_SIGNATURE,
            )
        )
        created = 1
        if effects:
            await effects.save_mission(mission)

    return StepOutput(
        result={"goals_ready": True, "created": created},
        observations=f"Ops task goal {'created' if created else 'present'}",
        context_updates={"mission": mission},
    )


def _parse_completion_criteria(text: str) -> list[dict]:
    """Parse the derived definition-of-done into [{command, name, required}].
    Accepts {"checks":[...]}, a bare list, or a lone {command} dict."""
    parsed = parse_llm_json(str(text or ""))
    # Derive-guard: an action/command emitted in place of checks is not a criterion.
    if isinstance(parsed, dict) and "action" in parsed and not (parsed.get("checks") or parsed.get("command")):
        return []
    if isinstance(parsed, dict):
        items = parsed.get("checks") or ([parsed] if parsed.get("command") else [])
    elif isinstance(parsed, list):
        items = parsed
    else:
        items = []
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and str(item.get("command") or "").strip():
            out.append({
                "command": str(item["command"]).strip(),
                "name": str(item.get("description") or item["command"])[:80],
                "required": True,
            })
    return out


async def action_store_reground_criteria(step_input: StepInput) -> StepOutput:
    """Store the grounded definition-of-done (THE derivation — the blind early
    pass was removed; "reground" is the historical name). Union-merges by command
    into any existing criteria (TIGHTEN-ONLY: never removes a stored check) and
    marks criteria grounded ONLY when the result is non-empty — the definition-
    of-done is mandatory, so an empty parse leaves the gate un-grounded and the
    next cycle re-derives (the retry, bounded by the cycle/wall-clock budget).

    Context: mission, inference_response.  Publishes: mission.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or getattr(mission, "task_definition", None) is None:
        return StepOutput(result={"criteria_count": 0}, observations="No task_definition")
    td = mission.task_definition
    new = _parse_completion_criteria(str(step_input.context.get("inference_response", "")))
    merged = list(td.completion_criteria or [])
    seen = {c.get("command") for c in merged}
    added = 0
    for c in new:
        if c["command"] not in seen:
            merged.append(c)
            seen.add(c["command"])
            added += 1
    td.completion_criteria = merged
    if merged:
        td.completion_criteria_grounded = True
    else:
        logger.warning("Definition-of-done parsed to 0 checks — will re-derive next cycle")
    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"criteria_count": len(merged)},
        observations=f"Grounded definition-of-done: {len(merged)} check(s) (+{added} this pass)",
        context_updates={"mission": mission},
    )


_FORMAT_CHECK_TYPES = {"exists", "line_count", "regex", "no_wrapping", "required_keys", "columns"}


def _parse_output_format_spec(text: str) -> dict | None:
    """Parse an LLM output-format response into {output_file, checks} or None.
    CONSERVATIVE: only a spec with >=1 valid check gates anything; an
    unparseable/empty/check-less response yields None (no format gate)."""
    parsed = parse_llm_json(str(text or ""))
    if not isinstance(parsed, dict):
        return None
    # Derive-guard: the model sometimes emits an ACTION/command ({"action": ...})
    # instead of a format spec — reject it as non-spec (the grounded reground fires).
    if "action" in parsed and "checks" not in parsed:
        return None
    raw = parsed.get("checks")
    checks = [
        c for c in (raw if isinstance(raw, list) else [])
        if isinstance(c, dict) and str(c.get("type", "")).lower() in _FORMAT_CHECK_TYPES
    ]
    if not checks:
        return None
    out_file = parsed.get("output_file")
    return {
        "output_file": str(out_file).strip() if isinstance(out_file, str) else "",
        "checks": checks,
    }


async def action_store_reground_output_format(step_input: StepInput) -> StepOutput:
    """Store the grounded output-format spec and mark the derivation done (THE
    derivation — the blind early pass was removed; "reground" is the historical
    name). Fed the grounded derivation (task + live terminal exploration), so it
    can name the conventional artifact a blind pass would miss. Sets
    output_format_grounded=True so the gate fires at most ONCE per mission — even
    when it finds no concrete artifact (the format gate is OPTIONAL; don't re-pay
    the inference every cycle). Conservative: an empty parse leaves the existing
    spec untouched (no gate) but still marks grounded.

    Context: mission, inference_response
    Result: format_check_count
    Publishes: mission
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or getattr(mission, "task_definition", None) is None:
        return StepOutput(result={"format_check_count": 0}, observations="No task_definition")

    spec = _parse_output_format_spec(str(step_input.context.get("inference_response", "")))
    td = mission.task_definition
    if spec is not None:
        td.output_format_spec = spec
    td.output_format_grounded = True  # one-shot guard regardless of parse outcome
    if effects:
        await effects.save_mission(mission)
    n = len(spec["checks"]) if spec else 0
    return StepOutput(
        result={"format_check_count": n},
        observations=(
            f"Regrounded output-format: {n} shape check(s) on "
            f"{spec.get('output_file') or 'artifact'}" if spec
            else "Regrounded output-format: no concrete artifact identified (no gate)"
        ),
        context_updates={"mission": mission},
    )


async def action_exa_probe_gate(step_input: StepInput) -> StepOutput:
    """Gate the stuck-task external search. When a task has looped without
    completing (attempts >= 2) and we haven't searched yet, derive a focused web
    query from the objective and signal exa_search to run. One-shot via
    task_definition.search_findings. The hits are NEW INFORMATION surfaced into the
    next charter (§8 tool-result), NOT a creativity nudge — the static anti-give-up
    language handles persistence; this brings in what the agent can't derive alone.

    Context: mission (required).  Result: should_search.  Publishes: search_queries.
    """
    import re as _re
    mission = step_input.context.get("mission")
    td = getattr(mission, "task_definition", None) if mission else None
    if td is None:
        return StepOutput(result={"should_search": False}, observations="exa-probe: no task_definition")
    attempts = int(getattr(td, "attempts", 0) or 0)
    already = bool((getattr(td, "search_findings", "") or "").strip())
    if attempts < 2 or already:
        return StepOutput(
            result={"should_search": False},
            observations=f"exa-probe: skip (attempts={attempts}, searched={already})",
        )
    objective = str(getattr(mission, "objective", "") or "")
    cleaned = _re.sub(r"[/\\]\S+|`[^`]*`", " ", objective)   # strip paths + backticked literals
    query = _re.sub(r"\s+", " ", cleaned).strip()[:200] or objective[:200]
    return StepOutput(
        result={"should_search": True},
        observations=f"exa-probe: searching (attempts={attempts})",
        context_updates={"search_queries": [query]},
    )


async def action_store_search_findings(step_input: StepInput) -> StepOutput:
    """Format the exa hits and store them on the TaskState so the charter surfaces
    them as NEW INFORMATION next cycle. One-shot guard (sets search_findings — a
    sentinel even on zero hits so the gate stops re-searching).

    Context: mission, raw_search_results.  Publishes: mission.
    """
    import re as _re
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or getattr(mission, "task_definition", None) is None:
        return StepOutput(result={"n_hits": 0}, observations="no task_definition")
    hits = step_input.context.get("raw_search_results") or []
    lines = []
    for h in hits[:5]:
        if not isinstance(h, dict):
            continue
        url = str(h.get("url", "")).strip()
        body = _re.sub(r"\s+", " ", str(h.get("content", "") or "")).strip()[:500]
        if body:
            lines.append(f"- {url}\n  {body}")
    block = "\n".join(lines)
    mission.task_definition.search_findings = block or "(no relevant web results found)"
    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"n_hits": len(lines)},
        observations=f"stored {len(lines)} search finding(s)",
        context_updates={"mission": mission},
    )


async def action_judge_task_completion(step_input: StepInput) -> StepOutput:
    """Decide whether the task is done, conservatively.

    "Done" requires BOTH the deterministic completion checks to pass
    (all_required_passing, from run_validation_checks) AND the judge
    inference to confirm (catches criteria that were too weak). Otherwise the
    judge's one-line feedback is stored on the TaskState to seed the next
    run_session, and the task goal stays incomplete (the loop continues until
    done or the budget caps).

    The judge verdict is read from judge_response when present (the verify-
    before-harvest turn overwrote inference_response with the VERIFY output;
    reprobe_completion stashed the judge's raw verdict), else from
    inference_response (the verify path was skipped).

    Context: mission, inference_response, judge_response (optional),
             validation_results (from checks)
    Result: task_done
    Publishes: mission
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"task_done": False}, observations="No mission")

    # Deterministic signal from the completion checks (computed from the
    # published results so we don't depend on a result-only field). An ops task
    # is certified done ONLY against a derived definition-of-done — never on the
    # judge alone. So BOTH "no criteria at all" (derivation hasn't succeeded —
    # e.g. a transient inference error returned empty) and "criteria exist but
    # produced no results" (the checks didn't run — a wiring failure) are NOT
    # done: loop, and the next cycle re-derives / re-runs. This closes the
    # judge-only escape hatch that would otherwise complete a task whose
    # definition-of-done was never established.
    results = step_input.context.get("validation_results") or []
    td = getattr(mission, "task_definition", None)
    criteria_defined = bool(getattr(td, "completion_criteria", None))
    if results:
        checks_passed = all(r.get("passed") for r in results if r.get("required", True))
        no_criteria = False
    else:
        checks_passed = False
        no_criteria = not criteria_defined

    judge_raw = str(
        step_input.context.get("judge_response")
        or step_input.context.get("inference_response", "")
    )
    parsed = parse_llm_json(judge_raw)
    parsed = parsed if isinstance(parsed, dict) else {}
    judge_done = bool(parsed.get("task_complete"))
    feedback = str(parsed.get("feedback") or "").strip()

    task_goal = next(
        (
            g
            for g in mission.goals
            if getattr(g, "finding_signature", "") == TASK_GOAL_SIGNATURE
        ),
        None,
    )

    done = checks_passed and judge_done
    if done and task_goal is not None:
        task_goal.status = "complete"
        observation = "Ops task complete (checks pass + judge confirms)"
    else:
        if getattr(mission, "task_definition", None) is not None:
            # Prefer the judge's note; fall back to a reason-specific default.
            if no_criteria:
                default_fb = (
                    "The definition-of-done has not been derived yet — completion "
                    "cannot be certified. Keep working; criteria will be re-derived."
                )
            else:
                default_fb = (
                    "Some completion checks still fail — keep working toward them."
                )
            mission.task_definition.last_feedback = feedback or default_fb
            mission.task_definition.attempts += 1
        why = (
            "no definition-of-done"
            if no_criteria
            else ("checks fail" if not checks_passed else "judge: not yet done")
        )
        observation = f"Ops task not done ({why}) — looping with feedback"

    # Record this cycle's durable workspace effects (installs + a session note) so
    # the next cycle builds on them instead of re-doing work.
    _record_workspace_ledger(
        mission,
        step_input,
        session_status="success" if done else "attempt",
        session_desc=(feedback or observation),
    )

    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"task_done": done},
        observations=observation,
        context_updates={"mission": mission},
    )
