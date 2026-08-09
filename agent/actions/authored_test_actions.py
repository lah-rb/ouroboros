"""Authored regression tests — TDD at the point of repair (v13, 2026-08-09).

A goal's acceptance checks are DERIVED after a passing session, from that
session's transcript alone. That makes them session *replay*, and replay
cannot own its own preconditions. The hy3 run produced all three failure
modes of that design in one week:

  * fingerprint  — ``current_room_id=='room4' and inventory==['sword']``:
    state that session happened to reach, not the invariant.
  * transient    — ``test -f save.json``: a file the pre-session transient
    flush deletes, so the check fails forever through no fault of the code.
    Five goals reopened per sweep wave; the quit goal reached retest_count
    51 — 51 guided rechecks of a behaviour that never broke.
  * exemplar     — data compared against a frozen day-one design sketch
    (evicted separately; OPEN_TASKS §23).

A real test owns its preconditions, and the one moment a NEGATIVE CONTROL
exists is while the code is still broken. This module runs there: inside the
diagnosis session, after conclude, before the fix dispatch. The candidate is
kept only if four MECHANICAL controls pass — prompt rules alone are the lever
that already failed (rule 8 only partially fixed fingerprints):

  1. CLASSIFIED RED. An import error, a syntax error and a collection failure
     all exit non-zero and all stay red AFTER the fix, producing a goal that
     can never complete. Red means ``rc == 1`` AND named failing nodes AND a
     clean collection (``_parse_pytest_output``). Anything else is BROKEN.
  2. COLD WORKSPACE. Every known transient is flushed before the probe, so
     the red is measured from the same floor the acceptance rung will use
     later — not from whatever the last session left lying around.
  3. DOUBLE RUN, WARM SECOND. The two runs are deliberately NOT re-flushed
     between: run 1 sees a cold workspace, run 2 inherits whatever run 1
     created. A test whose classification differs between them depends on
     leftover state in one direction or the other, and is dropped. This pair
     is what kills the ORDER-DEPENDENT half of the transient class
     mechanically.

     What it does NOT catch, stated plainly: a test that asserts a runtime
     file exists is red cold and red warm, so it passes both controls and can
     be armed while still being wrong. The prompt's transient list is the
     lever there, and the quarantine in ``action_reconcile_acceptance`` is the
     backstop — it bounds that mistake at 3 contradicted rounds instead of the
     51 the derived check cost.
  4. LEFTOVERS + SPEED. A test that leaves files behind pollutes every later
     session; a slow one blows the regression sweep's budget.

Every failure path deletes the candidate, records the reason, increments the
goal's attempt counter, and passes through. The arm's single flow exit means
it can never delay or block the fix.
"""

from __future__ import annotations

import logging
import math
import re
import time

from agent.loader import load_prompt_text
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

AUTHOR_PROMPT = load_prompt_text("diagnose/author_test")
# The one bounded repair turn, taken when the candidate passed against the
# still-broken code (VACUOUS — it is testing something other than the defect).
AUTHOR_REPAIR_PROMPT = load_prompt_text("diagnose/author_test_repair")

# Probe budget. _AUTHORED_MAX_SECONDS is the GATE (the regression sweep runs
# these checks in parallel on every file-affecting cycle, so a slow test is a
# tax on the whole mission); the kill timeout sits just above it so a hang is
# observed as a timeout rather than as a gate failure we can't explain.
_AUTHORED_MAX_SECONDS = 20.0
_AUTHORED_RUN_TIMEOUT = 25
_STORED_TIMEOUT_CAP = 120
_MAX_TEST_BYTES = 20000

# Directories a test run legitimately creates and that the runner itself owns.
_LEFTOVER_IGNORE = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache)(/|$)"
)


def _parse_candidate(text: str) -> dict:
    """Pull {path, language, command, content} out of the authoring response.

    Tolerant by design: a whole Python file embedded as a JSON string is the
    single most escape-fragile thing we could ask a model for, so the header
    JSON carries the metadata and the body may arrive either inline
    (``content``) or as its own fenced block. Both forms are accepted.
    """
    from agent.llm_json import parse_llm_json

    raw = str(text or "")
    parsed = parse_llm_json(raw)
    obj = parsed if isinstance(parsed, dict) else {}
    content = str(obj.get("content", "") or "")
    if not content.strip():
        blocks = re.findall(r"```(?:python|py)\s*\n(.*?)```", raw, re.DOTALL)
        content = blocks[-1] if blocks else ""
    return {
        "path": str(obj.get("path", "") or "").strip(),
        "language": str(obj.get("language", "") or "python").strip().lower(),
        "command": str(obj.get("command", "") or "").strip(),
        "content": content.strip(),
    }


def _path_error(path: str) -> str | None:
    """Reject anything that isn't a fresh file under ``tests/``."""
    if not path:
        return "no path"
    if path.startswith(("/", "~")) or ".." in path.split("/"):
        return f"path escapes the workspace: {path}"
    if not path.startswith("tests/"):
        return f"path outside tests/: {path}"
    if not path.endswith(".py"):
        return f"not a python test file: {path}"
    base = path.rsplit("/", 1)[-1]
    if not (base.startswith("test_") or base.endswith("_test.py")):
        return f"pytest will not collect {base} (needs test_*.py)"
    return None


def _canonical_command(path: str) -> str:
    # --no-header keeps the output stable for the classifier; -q keeps it small
    # enough to carry as red_evidence.
    return f"python -m pytest -q --no-header {path}"


def _safe_command(model_command: str, path: str) -> str:
    """Use the model's command only when it is a plain pytest invocation of
    THIS file; otherwise substitute the canonical one. A stored acceptance
    check is re-run unattended on every sweep, so it may not chain, redirect
    or substitute."""
    cmd = (model_command or "").strip()
    if not cmd or path not in cmd:
        return _canonical_command(path)
    if re.search(r"[;&|><`$\n]", cmd):
        return _canonical_command(path)
    if "pytest" not in cmd:
        return _canonical_command(path)
    return cmd


def _classify(return_code: int, output: str, timed_out: bool) -> tuple[str, list[str]]:
    """RED / GREEN / BROKEN — control #1.

    The distinction that matters: a test that ERRORS is red today and red
    forever, so arming it would immortalize the goal. Only a real assertion
    failure — a clean collection, rc 1, named failing nodes — is a negative
    control.
    """
    from agent.actions.pipeline_actions import _parse_pytest_output

    if timed_out:
        return "broken", []
    nodes, collect_ok = _parse_pytest_output(output or "")
    if return_code == 0:
        return "green", nodes
    if return_code == 1 and nodes and collect_ok:
        return "red", nodes
    return "broken", nodes


async def _snapshot(effects) -> set[str]:
    try:
        listing = await effects.list_directory(".", recursive=True)
    except Exception:  # noqa: BLE001 - a missed snapshot only weakens the gate
        return set()
    return {
        getattr(e, "path", "")
        for e in getattr(listing, "entries", []) or []
        if not getattr(e, "is_dir", False) and getattr(e, "path", "")
    }


async def _run_once(effects, command: str) -> tuple[str, list[str], str, float]:
    """Run the candidate exactly as the regression sweep later will."""
    started = time.monotonic()
    try:
        res = await effects.run_command(
            ["/bin/sh", "-c", command], timeout=_AUTHORED_RUN_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 - an infra miss is not a red test
        return "broken", [], f"run failed: {exc}", time.monotonic() - started
    elapsed = time.monotonic() - started
    out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
    verdict, nodes = _classify(
        int(getattr(res, "return_code", 1) or 0),
        out,
        bool(getattr(res, "timed_out", False)),
    )
    return verdict, nodes, out, elapsed


async def action_author_regression_test(step_input: StepInput) -> StepOutput:
    """Author a regression test for the goal under repair; keep it only if it
    probes RED from a cold workspace, twice, leaving nothing behind.

    Context: diagnosis_session_id, author_test_brief, working_directory.
    Inputs: goal_id.  Result: authored, reason.
    Publishes: nothing — deliberately (see the flow step's comment: the arm
    must not arm regression_dirty or perturb the pending diagnosis).
    """
    effects = step_input.effects
    ctx = step_input.context
    session_id = str(ctx.get("diagnosis_session_id", "") or "")
    brief = ctx.get("author_test_brief") or {}
    goal_id = str(step_input.inputs.get("goal_id", "") or "")

    def _out(authored: bool, reason: str) -> StepOutput:
        return StepOutput(
            result={"authored": authored, "reason": reason},
            observations=f"author_test: {reason}",
        )

    if effects is None or not session_id or not isinstance(brief, dict) or not brief:
        return _out(False, "no session/brief — skipped")

    try:
        return await _author(step_input, effects, session_id, brief, goal_id, _out)
    except Exception as exc:  # noqa: BLE001 - the fix dispatch is never at risk
        logger.warning("author_test: unhandled error (%s)", exc, exc_info=True)
        return _out(False, f"error ({exc}) — skipped")


async def _author(step_input, effects, session_id, brief, goal_id, _out) -> StepOutput:
    prompt = _render_prompt(brief)
    written_path = ""

    async def _cleanup(path: str) -> None:
        if path:
            try:
                await effects.run_command(["rm", "-f", path], timeout=10)
            except Exception:  # noqa: BLE001
                logger.debug("author_test: cleanup failed for %s", path, exc_info=True)

    # Two turns at most: the first authors, the second gets ONE bounded repair
    # turn when the candidate passed against the broken code (VACUOUS — it is
    # testing something other than the defect).
    turn_prompt = prompt
    for turn in (1, 2):
        try:
            result = await effects.session_inference(
                session_id, turn_prompt, {"temperature": "t*0.4"}
            )
            text = result.text or ""
        except Exception as exc:  # noqa: BLE001
            return await _drop(
                effects, step_input, goal_id, "", f"inference failed ({exc})", _out
            )

        cand = _parse_candidate(text)
        path, content = cand["path"], cand["content"]
        perr = _path_error(path)
        if perr:
            return await _drop(effects, step_input, goal_id, "", perr, _out)
        if not content:
            return await _drop(
                effects, step_input, goal_id, "", "empty test body", _out
            )
        if len(content) > _MAX_TEST_BYTES:
            return await _drop(
                effects,
                step_input,
                goal_id,
                "",
                f"test body too large ({len(content)} bytes)",
                _out,
            )
        existing = await effects.read_file(path)
        if getattr(existing, "exists", False) and (existing.content or "").strip():
            return await _drop(
                effects, step_input, goal_id, "", f"{path} already exists", _out
            )

        from agent.actions.file_ops_actions import guarded_write_file

        written, werr = await guarded_write_file(
            effects, path, content, min_retention_ratio=0.0
        )
        if not written:
            return await _drop(
                effects, step_input, goal_id, "", f"write rejected: {werr}", _out
            )
        written_path = path

        # Syntax floor — an unparseable test is red forever.
        try:
            comp = await effects.run_command(
                ["python", "-m", "py_compile", path], timeout=20
            )
            if int(getattr(comp, "return_code", 1) or 0) != 0:
                detail = (getattr(comp, "stderr", "") or "")[:200]
                return await _drop(
                    effects,
                    step_input,
                    goal_id,
                    written_path,
                    f"does not compile: {detail}",
                    _out,
                )
        except Exception as exc:  # noqa: BLE001
            return await _drop(
                effects,
                step_input,
                goal_id,
                written_path,
                f"syntax check failed ({exc})",
                _out,
            )

        command = _safe_command(cand["command"], path)

        # Control #2 — COLD WORKSPACE. Flush every known transient, then
        # snapshot, so a test that leans on a leftover runtime file is caught
        # here rather than by five reopened goals a day later.
        try:
            from agent.actions.interactive_actions import flush_known_transients

            mission = await effects.load_mission()
            flushed = await flush_known_transients(effects, mission)
        except Exception:  # noqa: BLE001 - a missed flush only weakens the gate
            flushed = []
        before = await _snapshot(effects)

        # Controls #1 + #3 — classified red, twice.
        v1, nodes1, out1, t1 = await _run_once(effects, command)
        v2, _nodes2, out2, t2 = await _run_once(effects, command)
        after = await _snapshot(effects)

        if v1 == "green" and v2 == "green" and turn == 1:
            # VACUOUS — one bounded repair turn, then give up.
            await _cleanup(written_path)
            written_path = ""
            turn_prompt = AUTHOR_REPAIR_PROMPT
            continue
        if v1 != v2:
            return await _drop(
                effects,
                step_input,
                goal_id,
                written_path,
                f"order-dependent ({v1} then {v2}) — not a reliable signal",
                _out,
            )
        if v1 != "red":
            reason = (
                "passed against the broken code (vacuous)"
                if v1 == "green"
                else f"not a classifiable failure: {_head(out1)}"
            )
            return await _drop(effects, step_input, goal_id, written_path, reason, _out)

        # Control #4 — leftovers + speed.
        leftovers = sorted(
            p
            for p in (after - before)
            if p != written_path and not _LEFTOVER_IGNORE.search(p)
        )
        if leftovers:
            return await _drop(
                effects,
                step_input,
                goal_id,
                written_path,
                f"leaves files behind: {', '.join(leftovers[:5])}",
                _out,
            )
        slowest = max(t1, t2)
        if slowest > _AUTHORED_MAX_SECONDS:
            return await _drop(
                effects,
                step_input,
                goal_id,
                written_path,
                f"too slow ({slowest:.1f}s > {_AUTHORED_MAX_SECONDS:.0f}s)",
                _out,
            )

        stored_timeout = min(
            _STORED_TIMEOUT_CAP, max(30, int(math.ceil(slowest * 4)) + 5)
        )
        return await _store(
            effects,
            step_input,
            goal_id,
            {
                "path": path,
                "command": command,
                "language": "python",
                "red_rc": 1,
                "red_nodes": nodes1[:8],
                "red_evidence": _head(out1, 600),
                "timeout": stored_timeout,
                "seconds": round(slowest, 2),
                "flushed_before_probe": flushed[:10],
            },
            _out,
        )

    await _cleanup(written_path)
    return await _drop(
        effects,
        step_input,
        goal_id,
        "",
        "passed against the broken code twice (vacuous)",
        _out,
    )


def _head(text: str, limit: int = 240) -> str:
    return " ".join((text or "").split())[:limit]


def _render_prompt(brief: dict) -> str:
    """Substitute the brief into the prompt by literal token replacement.

    NOT str.format: the prompt carries a fenced JSON output example, and every
    brace in it would have to be doubled — one un-doubled brace turns the whole
    authoring turn into a KeyError at run time.
    """
    transients = brief.get("transient_files") or []
    transient_line = ", ".join(str(t) for t in transients[:20]) or "(none declared)"
    fields = {
        "{goal_description}": str(brief.get("goal_description", "")),
        "{target_file}": str(brief.get("target_file", "")),
        "{target_symbol}": str(brief.get("target_symbol", "")) or "(whole file)",
        "{change_spec}": str(brief.get("change_spec", "")),
        "{root_cause}": str(brief.get("root_cause", "")),
        "{suggested_path}": str(brief.get("suggested_path", "tests/test_goal.py")),
        "{transient_files}": transient_line,
        "{max_seconds}": str(int(_AUTHORED_MAX_SECONDS)),
    }
    text = AUTHOR_PROMPT
    for token, value in fields.items():
        text = text.replace(token, value)
    return text


async def _load_goal(effects, goal_id: str):
    try:
        mission = await effects.load_mission()
    except Exception:  # noqa: BLE001
        return None, None
    goal = next(
        (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id), None
    )
    return mission, goal


async def _drop(effects, step_input, goal_id, path, reason, _out) -> StepOutput:
    """Delete the candidate, count the attempt, record why, pass through."""
    from agent.persistence.models import NoteRecord

    if path:
        try:
            await effects.run_command(["rm", "-f", path], timeout=10)
        except Exception:  # noqa: BLE001
            logger.debug("author_test: cleanup failed for %s", path, exc_info=True)
    mission, goal = await _load_goal(effects, goal_id)
    if goal is not None:
        goal.authored_test_attempts = (
            int(getattr(goal, "authored_test_attempts", 0) or 0) + 1
        )
        mission.notes.append(
            NoteRecord(
                content=(
                    f"authored regression test DROPPED for "
                    f"'{goal.description[:70]}': {reason}"
                ),
                category="failure_analysis",
                tags=["authored_test", "dropped"],
                source_flow="author_test",
            )
        )
        try:
            await effects.save_mission(mission)
        except Exception:  # noqa: BLE001 - telemetry never breaks the arm
            logger.debug("author_test: save failed", exc_info=True)
    logger.info("author_test: dropped candidate — %s", reason)
    return _out(False, f"dropped — {reason}")


async def _store(effects, step_input, goal_id, record, _out) -> StepOutput:
    """Arm the authored test and DEMOTE the derived replay checks.

    The demotion is the direct mechanical cure for the save.json reopen class:
    ``required: False`` stops a mis-grounded replay check vetoing completion
    AND drops it out of ``action_regression_sweep``, which filters on
    ``required`` in both directions. Nothing is deleted — the history stays
    readable, and a human can see what the replay guard used to assert.
    """
    mission, goal = await _load_goal(effects, goal_id)
    if goal is None:
        return _out(False, "goal vanished before store")

    authored_check = {
        "name": "authored regression test",
        "command": record["command"],
        "required": True,
        "source": "authored",
        "timeout": record["timeout"],
    }
    existing = list(getattr(goal, "acceptance_checks", None) or [])
    demoted = [
        {**c, "required": False}
        for c in existing
        if c.get("source") != "authored" and c.get("command") != record["command"]
    ]
    # Authored first: the interact runner breaks on the first REQUIRED failure,
    # so the test with a verified negative control is what the goal is judged on.
    goal.acceptance_checks = [authored_check] + demoted
    goal.authored_test = dict(record)
    goal.authored_test_attempts = (
        int(getattr(goal, "authored_test_attempts", 0) or 0) + 1
    )
    try:
        await effects.save_mission(mission)
    except Exception as exc:  # noqa: BLE001
        return _out(False, f"store failed ({exc})")
    logger.info(
        "author_test: armed %s (red, %ss) on '%s'; demoted %d derived check(s)",
        record["path"],
        record["seconds"],
        goal.description[:50],
        len(demoted),
    )
    return _out(
        True,
        (
            f"armed {record['path']} — RED against the broken code "
            f"({record['seconds']}s), {len(demoted)} derived check(s) demoted"
        ),
    )
