"""Oracle rungs — independent completion oracles that append to validation_results.

Formalizes code_core's gate discipline as reusable rungs any flow (ops included)
can stand on. Every rung follows the asym-probe contract
(operations_actions.action_run_property_probe):

  GATE      deterministic eligibility — skip cleanly when the rung doesn't apply
            (zero cost on non-eligible cycles).
  CHECK     produce a verdict from the LIVE artifact, not the agent's self-report.
  APPEND    push a {required:true} dict onto validation_results so the existing
            judge/decide loop treats a violation as "not done" and loops with the
            finding as feedback (action_judge_task_completion:
            done = checks_passed AND judge_done) — NO new control flow.
  FAIL-SAFE on the rung's OWN error, append NOTHING — never a manufactured FAIL,
            never a silent PASS. Only a genuine finding appends a FAIL.
  CLEANUP   remove any probe artifacts so the graded workspace stays clean.

Rung 0 here is non-degeneracy / sanity: the produced answer artifact is not
obviously garbage (empty / error-trace / bare 0 / placeholder). It backstops the
credulous ops judge that passed a literal "0" for count-dataset-tokens. The
deterministic floor runs at zero inference; a light-inference plausibility pass
(action_record_output_sanity, fed by a small turn) catches wrong type/magnitude
the floor cannot, and only fires when the floor passes on an answer task.
"""

from __future__ import annotations

import logging
import re

from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# ── Lifted liveness predicate (D3) ────────────────────────────────────────
# Error-strings an exit-0 result (or a written artifact) can still hide. Kept in
# sync with pipeline_actions._FAILURE_PATTERNS (interact's deterministic eval);
# lifted here so any flow can reuse the predicate via liveness_scan().
_FAILURE_PATTERNS = [
    "Traceback (most recent call last)",
    "ImportError:", "ModuleNotFoundError:", "SyntaxError:", "FileNotFoundError:",
    "NameError:", "TypeError:", "AttributeError:", "ValueError:", "KeyError:",
    "IndentationError:", "OSError:", "PermissionError:", "RuntimeError:",
]


def liveness_scan(text: str) -> list[str]:
    """The lifted liveness predicate: error-patterns present in `text` (empty
    list == clean). Shared by the sanity rung; reusable by code_core's
    deterministic interact eval."""
    return [p.rstrip(":") for p in _FAILURE_PATTERNS if p in (text or "")]


# Placeholder / non-answer tokens an LLM emits when it stubs or gives up.
_PLACEHOLDER_RE = re.compile(
    r"(?i)^(todo|tbd|fixme|placeholder|your answer here|n/?a|none|null|undefined|"
    r"unknown|<[^>]*>)$"
)
# A produced-artifact path named by a completion-criteria command.
_PATH_RE = re.compile(
    r"(?:test\s+-[a-z]+\s+|cat\s+|grep\b[^\n]*?\s|\[\s+-[a-z]+\s+)"
    r"(/?[\w./\-]*\.\w+|/[\w][\w./\-]+)"
)
_SYS_PREFIXES = ("/bin", "/usr", "/etc", "/proc", "/sys", "/dev", "/lib", "/sbin")


def _extract_artifact_path(criteria: list) -> str | None:
    """The single produced-artifact path named by the completion criteria, or
    None (skip — never guess between several)."""
    paths: list[str] = []
    for c in criteria or []:
        cmd = c.get("command") if isinstance(c, dict) else None
        if not isinstance(cmd, str):
            continue
        for m in _PATH_RE.finditer(cmd):
            p = m.group(1)
            if p.startswith(_SYS_PREFIXES):
                continue
            paths.append(p)
    uniq = list(dict.fromkeys(paths))
    return uniq[0] if len(uniq) == 1 else None


def _degenerate_reason(content: str, criteria: list) -> str | None:
    """Deterministic non-degeneracy floor. Returns a reason if the content is
    UNAMBIGUOUSLY garbage, else None. Conservative — a legitimately terse answer
    must not be refused."""
    s = (content or "").strip()
    if not s:
        return "answer artifact is empty"
    errs = liveness_scan(content)
    if errs:
        return f"answer artifact contains an error trace ({', '.join(errs)})"
    # Bare 0 — but only when NO criterion explicitly names that literal (some
    # correct answers genuinely are 0; defer to the existing check then).
    crit_text = " ".join(
        str(c.get("command", "")) for c in (criteria or []) if isinstance(c, dict)
    )
    if s == "0" and not re.search(r"(?<![\d.])0(?![\d.])", crit_text):
        return ("answer artifact is a bare 0 — a degenerate default; the producing "
                "step likely matched or computed nothing")
    if _PLACEHOLDER_RE.match(s) or (len(s) <= 24 and _PLACEHOLDER_RE.match(s.lower())):
        return f"answer artifact looks like a placeholder/non-answer ({s[:40]!r})"
    return None


def _sanity_result(passed: bool, path: str, reason: str) -> dict:
    """A validation_results dict in the shared contract shape."""
    return {
        "name": f"output_sanity: {path}",
        "command": f"sanity-check {path}",
        "passed": passed,
        "required": True,
        "stdout": "" if passed else reason[:500],
        "stderr": "",
        "return_code": 0 if passed else 1,
    }


async def action_check_output_sanity(step_input: StepInput) -> StepOutput:
    """Rung 0 (deterministic floor). For an answer-producing ops task, read the
    produced artifact and append a REQUIRED fail if its content is obviously
    degenerate. On a clean floor, publish the excerpt + request the plausibility
    pass. Fail-safe: on its own error, append nothing; routes onward regardless.

    Context: mission (required); validation_results, terminal_output (optional).
    Result: check_plausibility (route flag), sanity_eligible, sanity_passed.
    Publishes: validation_results, sanity_artifact_excerpt.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    results = list(step_input.context.get("validation_results") or [])
    updates: dict = {"validation_results": results}

    td = getattr(mission, "task_definition", None) if mission else None
    criteria = list(getattr(td, "completion_criteria", None) or [])
    path = _extract_artifact_path(criteria)

    def _skip(why: str) -> StepOutput:
        return StepOutput(
            result={"check_plausibility": False, "sanity_eligible": False},
            observations=f"output-sanity skipped ({why})",
            context_updates={},
        )

    # GATE — no single produced artifact named (configure/run tasks). Skip.
    if not path or effects is None:
        return _skip("no single produced artifact in criteria")

    # CHECK — read the live artifact.
    try:
        fc = await effects.read_file(path)
    except Exception as exc:  # FAIL-SAFE: own error → append nothing
        return _skip(f"read error: {exc}")
    if not getattr(fc, "exists", False):
        return _skip(f"{path} absent — left to the existence check")

    content = getattr(fc, "content", "") or ""
    updates["sanity_artifact_excerpt"] = f"{path}:\n{content.strip()[:1500]}"

    reason = _degenerate_reason(content, criteria)
    if reason:
        results.append(_sanity_result(False, path, reason))
        updates["validation_results"] = results
        return StepOutput(
            result={"check_plausibility": False, "sanity_eligible": True,
                    "sanity_passed": False},
            observations=f"output-sanity FAIL: {reason}",
            context_updates=updates,
        )

    # Floor clean — ask for the light-inference plausibility pass.
    return StepOutput(
        result={"check_plausibility": True, "sanity_eligible": True,
                "sanity_passed": True},
        observations=f"output-sanity floor passed for {path}; plausibility next",
        context_updates=updates,
    )


async def action_record_output_sanity(step_input: StepInput) -> StepOutput:
    """Record the light-inference plausibility verdict (rung 0, second half).
    Parses the plausibility turn ({"plausible": bool, "reason": str}); appends a
    REQUIRED fail only on a confident implausible verdict. Fail-safe: an
    unparseable/empty verdict appends nothing (inconclusive != fail).

    Context: inference_response (optional); validation_results (optional);
             sanity_artifact_excerpt (optional, for the path label).
    Publishes: validation_results.
    """
    results = list(step_input.context.get("validation_results") or [])
    updates: dict = {"validation_results": results}
    excerpt = str(step_input.context.get("sanity_artifact_excerpt", "") or "")
    path = excerpt.split(":\n", 1)[0] or "answer"

    parsed = parse_llm_json(str(step_input.context.get("inference_response", "")))
    if not isinstance(parsed, dict) or "plausible" not in parsed:
        return StepOutput(  # FAIL-SAFE: inconclusive verdict → append nothing
            result={"sanity_passed": True},
            observations="output-sanity plausibility inconclusive — no finding",
            context_updates={},
        )
    plausible = bool(parsed.get("plausible"))
    reason = str(parsed.get("reason", ""))[:300]
    if plausible:
        return StepOutput(
            result={"sanity_passed": True},
            observations=f"output-sanity plausible: {reason[:120]}",
            context_updates={},
        )
    results.append(_sanity_result(
        False, path, f"answer implausible for the task: {reason}"))
    updates["validation_results"] = results
    return StepOutput(
        result={"sanity_passed": False},
        observations=f"output-sanity IMPLAUSIBLE: {reason[:120]}",
        context_updates=updates,
    )


# ── Verify-before-harvest: completion re-probe (D4) ───────────────────────
# Don't harvest the judge's "done" on its word. When the judge claims complete,
# RE-PROBE the completion criteria against the live container + re-read the
# produced artifact, then an LLM verify turn confirms the end state is GENUINELY
# done (not a placeholder/transient). The verdict is appended as a REQUIRED check
# so the existing decide loop derives done from the survivor — mirroring
# quality_gate's verify-before-harvest, adapted to ops (no architecture launch
# command, so the repro IS the completion criteria, not launch+repro).


def _bounded(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " …[truncated]"


async def action_reprobe_completion(step_input: StepInput) -> StepOutput:
    """Gate + re-probe. If the judge claimed done, re-run the completion criteria
    and re-read the produced artifact into a FRESH, context-bounded transcript for
    the verify turn, and stash the judge's raw verdict (judge_response) so decide
    still sees it after the verify turn overwrites inference_response. Skips
    straight to decide when the judge didn't claim done, there are no criteria, or
    effects are unavailable (never blocks on infra).

    Context: mission (required); inference_response (optional).
    Result: do_verify (route flag).
    Publishes: vbh_transcript, judge_response.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    judge_raw = str(step_input.context.get("inference_response", "") or "")
    parsed = parse_llm_json(judge_raw)
    judge_done = isinstance(parsed, dict) and bool(parsed.get("task_complete"))

    def _skip(why: str) -> StepOutput:
        return StepOutput(result={"do_verify": False},
                          observations=f"completion re-probe skipped ({why})",
                          context_updates={})

    if not judge_done:
        return _skip("judge did not claim done")
    if effects is None or mission is None:
        return _skip("no effects/mission")
    td = getattr(mission, "task_definition", None)
    criteria = list(getattr(td, "completion_criteria", None) or [])
    if not criteria:
        return _skip("no completion criteria to re-probe")

    lines: list[str] = []
    try:
        for c in criteria[:8]:
            cmd = c.get("command") if isinstance(c, dict) else None
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            res = await effects.run_command(["/bin/sh", "-c", cmd], timeout=20)
            rc = getattr(res, "return_code", None)
            out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
            lines.append(f"$ {cmd}\n[exit {rc}] {_bounded(out, 400)}")
        path = _extract_artifact_path(criteria)
        if path:
            fc = await effects.read_file(path)
            if getattr(fc, "exists", False):
                lines.append(
                    f"$ cat {path}\n{_bounded(getattr(fc, 'content', '') or '', 800)}")
    except Exception as exc:  # FAIL-SAFE: re-probe infra error → don't block
        return _skip(f"re-probe error: {exc}")

    if not lines:
        return _skip("re-probe produced no transcript")
    return StepOutput(
        result={"do_verify": True},
        observations=f"re-probed {len(lines)} completion signal(s) for verify",
        context_updates={"vbh_transcript": "\n\n".join(lines),
                         "judge_response": judge_raw},
    )


async def action_record_completion_verify(step_input: StepInput) -> StepOutput:
    """Derive the verdict from the verify turn and RESTORE the judge's verdict for
    decide. Append a REQUIRED fail only on a confident not-done verdict;
    inconclusive (unparseable / missing key) appends nothing — defer to the prior
    checks+judge pass, never loop forever on an infra/parse miss. decide then sees
    any fail and loops with the reason surfaced in the validation summary.

    Context: inference_response (required, the verify output); validation_results,
             judge_response (optional).
    Publishes: validation_results, inference_response (restored judge verdict).
    """
    results = list(step_input.context.get("validation_results") or [])
    judge_raw = str(step_input.context.get("judge_response", "") or "")
    updates: dict = {"validation_results": results}
    if judge_raw:
        updates["inference_response"] = judge_raw  # restore for decide

    parsed = parse_llm_json(str(step_input.context.get("inference_response", "")))
    if not isinstance(parsed, dict) or "genuinely_done" not in parsed:
        return StepOutput(result={"verified_done": True},
                          observations="completion verify inconclusive — no finding",
                          context_updates=updates)
    if bool(parsed.get("genuinely_done")):
        return StepOutput(result={"verified_done": True},
                          observations="completion verified genuinely done",
                          context_updates=updates)
    reason = str(parsed.get("reason", ""))[:400]
    results.append({
        "name": "completion_verify",
        "command": "verify-before-harvest re-probe",
        "passed": False,
        "required": True,
        "stdout": reason[:500],
        "stderr": "",
        "return_code": 1,
    })
    updates["validation_results"] = results
    return StepOutput(result={"verified_done": False},
                      observations=f"completion REFUTED on re-probe: {reason[:120]}",
                      context_updates=updates)


# ── Profile-gated oracle rungs (Phase 2) ──────────────────────────────────
# One dispatcher gated on mission.config.task_profile (set by the task judge).
# Each check is best-effort + fail-safe: appends a REQUIRED fail ONLY on an
# UNAMBIGUOUS failure (dead service / empty transform output / corrupt archive),
# never on its own inability to determine — so a rung can only tighten the gate,
# never refuse a believable result. The generic checks catch the COMMON failure
# of each class; full semantic verification (exact conservation, true round-trip)
# needs task-specific knowledge and is out of scope here.

_PROBE_RE = re.compile(
    r"(?i)\b(curl|wget|nc\b|ncat|is-active|systemctl\s+status|pgrep|\bss\b|netstat|health)")
_PORT_RE = re.compile(r"(?::|\bport\s+)(\d{2,5})\b")
_ARCHIVE_RE = re.compile(r"(\S+\.(?:tar\.gz|tgz|tar\.bz2|tar|zip|gz|bz2|xz))")
_DEAD_RE = re.compile(
    r"(?i)connection refused|could not connect|couldn't connect|not running|"
    r"\binactive\b|failed to|no such (?:file|host)|empty reply")


def _commands(criteria: list) -> list[str]:
    return [c["command"] for c in (criteria or [])
            if isinstance(c, dict) and isinstance(c.get("command"), str)]


async def _run(effects, cmd: str, timeout: int = 15):
    try:
        return await effects.run_command(["/bin/sh", "-c", cmd], timeout=timeout)
    except Exception:
        return None


def _alive(res) -> bool | None:
    """Liveness predicate on a CommandResult: True/False, or None if it couldn't
    run (unknown — caller must not treat None as failure)."""
    if res is None:
        return None
    out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
    if liveness_scan(out) or _DEAD_RE.search(out):
        return False
    return getattr(res, "return_code", 1) == 0


async def _check_liveness(effects, criteria, objective) -> str | None:
    """service: the produced service actually responds (not just configured)."""
    for cmd in _commands(criteria):
        if _PROBE_RE.search(cmd):
            v = _alive(await _run(effects, cmd))
            if v is False:
                return f"service liveness probe failed: `{cmd[:80]}`"
            if v is True:
                return None
    text = " ".join(_commands(criteria)) + " " + (objective or "")
    m = _PORT_RE.search(text)
    if m:
        port = m.group(1)
        v = _alive(await _run(
            effects,
            f"curl -sf -m 3 -o /dev/null http://localhost:{port} "
            f"|| nc -z -w 3 localhost {port}"))
        if v is False:
            return f"service on port {port} did not respond to an active probe"
    return None


async def _check_conservation(effects, criteria, objective) -> str | None:
    """data_transform: the output isn't silently empty / zero-record."""
    out = _extract_artifact_path(criteria)
    if not out:
        return None
    try:
        fc = await effects.read_file(out)
    except Exception:
        return None
    if not getattr(fc, "exists", False):
        return None
    content = getattr(fc, "content", "") or ""
    if not content.strip():
        return f"transform output {out} is empty — input data was dropped"
    rows = [ln for ln in content.splitlines() if ln.strip()]
    if out.endswith((".csv", ".tsv", ".jsonl", ".ndjson")) and len(rows) <= 1:
        return (f"transform output {out} has no data rows ({len(rows)} line) — "
                "the transform likely produced nothing")
    return None


async def _check_roundtrip(effects, criteria, objective) -> str | None:
    """invertible: a produced archive is valid (its inverse would succeed)."""
    text = " ".join(_commands(criteria)) + " " + (objective or "")
    m = _ARCHIVE_RE.search(text)
    if not m:
        return None
    arc = m.group(1)
    try:
        if not await effects.file_exists(arc):
            return None
    except Exception:
        return None
    test = (
        f"tar -tzf {arc} >/dev/null" if arc.endswith((".tar.gz", ".tgz"))
        else f"tar -tf {arc} >/dev/null" if arc.endswith(".tar")
        else f"unzip -t {arc} >/dev/null" if arc.endswith(".zip")
        else f"gzip -t {arc}" if arc.endswith(".gz")
        else f"bzip2 -t {arc}" if arc.endswith(".bz2")
        else "")
    if not test:
        return None
    res = await _run(effects, test)
    if res is not None and getattr(res, "return_code", 1) != 0:
        return f"produced archive {arc} is corrupt/incomplete (integrity check failed)"
    return None


async def _check_regression(effects, criteria, objective) -> str | None:
    """repair: the fix didn't STRUCTURALLY break the test suite. Runs pytest
    collect-only (cheap, universal) — a fix that broke an import/syntax fails
    COLLECTION, which is unambiguous collateral damage. A pre-existing assertion
    failure does NOT fail collection, so it is never flagged (no baseline needed,
    no false-positive on unrelated pre-existing failures). Skips when pytest is
    unavailable / there are no tests."""
    res = await _run(effects, "python -m pytest --co -q 2>&1 | tail -40", timeout=60)
    if res is None:
        return None
    out = ((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")).lower()
    if "no module named pytest" in out or "no tests ran" in out or not out.strip():
        return None  # pytest absent / no suite — not a fix-induced break
    if re.search(r"errors during collection|error collecting|cannot import|"
                 r"importerror|modulenotfounderror|syntaxerror|indentationerror", out):
        m = re.search(r"(importerror|modulenotfounderror|syntaxerror|indentationerror"
                      r"|errors during collection)[^\n]*", out)
        return ("the fix broke test collection (structural collateral damage): "
                f"{(m.group(0) if m else 'collection error')[:120]}")
    return None


_PROFILE_CHECKS = {
    "service": _check_liveness,
    "data_transform": _check_conservation,
    "invertible": _check_roundtrip,
    "repair": _check_regression,
}


async def action_check_profile_oracle(step_input: StepInput) -> StepOutput:
    """Profile-gated oracle dispatcher. Runs the rung matching the mission's
    task_profile and appends a REQUIRED fail on an unambiguous failure. Skips
    cleanly for other profiles, no effects, or any own error (fail-safe).

    Context: mission (required); validation_results (optional).
    Publishes: validation_results.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    results = list(step_input.context.get("validation_results") or [])
    # ops_task has the mission; quality_gate (code_core) threads task_profile as a
    # flow input instead (it holds only working_directory + mission_id).
    profile = getattr(getattr(mission, "config", None), "task_profile", "") if mission else ""
    if not profile:
        profile = (step_input.inputs or {}).get("task_profile", "") or \
            step_input.context.get("task_profile", "") or ""
    fn = _PROFILE_CHECKS.get(profile)

    if fn is None or effects is None:
        return StepOutput(
            result={"profile_checked": False},
            observations=f"profile oracle skipped (profile={profile or 'none'})",
            context_updates={})

    td = getattr(mission, "task_definition", None)
    criteria = list(getattr(td, "completion_criteria", None) or [])
    objective = str(getattr(mission, "objective", "") or "")
    try:
        reason = await fn(effects, criteria, objective)
    except Exception as exc:  # FAIL-SAFE
        return StepOutput(
            result={"profile_checked": False},
            observations=f"profile oracle error ({exc}) — skipped",
            context_updates={})

    if not reason:
        return StepOutput(
            result={"profile_checked": True, "profile_passed": True},
            observations=f"{profile} oracle passed",
            context_updates={"validation_results": results})
    results.append({
        "name": f"{profile}_oracle",
        "command": f"{profile} verification",
        "passed": False,
        "required": True,
        "stdout": reason[:500],
        "stderr": "",
        "return_code": 1,
    })
    return StepOutput(
        result={"profile_checked": True, "profile_passed": False},
        observations=f"{profile} oracle FAIL: {reason[:120]}",
        context_updates={"validation_results": results})
