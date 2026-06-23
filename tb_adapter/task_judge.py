"""Per-task flow-set router + capability profile (the M3 task judge).

terminal-bench is not one task type. ~half the suite is software authoring/repair
(write or fix source code — code_core's diagnose→file_ops→patch→quality_gate is
built for this), the rest is terminal-accomplish (install/configure/run/extract/
CTF — ops's single-pass operator brief fits). Routing each task to the flow set
that fits is the M3 step of the container-target plan.

The judge emits TWO labels in ONE LLMVP completion: the ``flow_set``
(ops|code_core — the routing decision) AND a capability ``profile``
(service|data_transform|invertible|repair|answer|plain) that GATES which
completion oracles fire downstream (the oracle_actions rungs read it off the
mission). The LLM is the sole classifier: on an unreachable/unparseable answer it
RETRIES up to a limit (canonical run_session style — no keyword-heuristic shadow
classifier to drift out of sync), then defaults to (ops, plain) on exhaustion. An
explicit ``OURO_FLOW_SET`` env override is a hard escape hatch for the flow set.
The decision is returned with the method that produced it so the adapter can log it.

Policy (current): default to ops; choose code_core only when the core deliverable
is multi-file source the agent must write or fix.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

VALID_FLOW_SETS = ("ops", "code_core")

# Capability profiles — gate which oracle rung fires (agent/actions/oracle_actions):
#   service        a running service/daemon       → smoke/liveness rung
#   data_transform in→out data reshape            → conservation rung
#   invertible     reversible op (compress/encrypt)→ round-trip rung
#   repair         fix/debug existing code        → regression / no-collateral rung
#   answer         a specific answer value to file → sanity rung (built) + differential
#   plain          none of the above              → no extra oracle (configure/install/CTF)
VALID_PROFILES = ("service", "data_transform", "invertible", "repair", "answer", "plain")

# t*0.3 at the 0.7 model default (PROMPTING_CONVENTIONS §11). temp 0.0 is NOT
# deterministic here (FP/concurrency noise flipped near-identical swe-bench tasks to
# different routes), so a small positive temp costs nothing and is canonical.
_JUDGE_TEMP = 0.7 * 0.3
# Retry the classifier on an unreachable/unparseable answer (run_session style),
# then default to (ops, plain) on exhaustion — no keyword-heuristic shadow.
_MAX_RETRIES = 3

# Static-first prompt (PROMPTING_CONVENTIONS): role, the buckets, examples, task last.
_JUDGE_PROMPT = """\
You are a task router for an autonomous software agent. Read the task and emit \
TWO labels as a JSON object: the execution flow and the task's capability profile.

flow_set — which flow handles the task:
  - ops: the lightweight single-pass DEFAULT — install/configure software, manage \
files/permissions, extract/compress archives, create resources, run a tool, CTF, \
AND single-file software work (author/implement/fix ONE file, convert data). ops \
can read, write, and run code; it fits whenever the work is one file or one goal.
  - code_core: heavy, many-step (whole-repo ingest, multi-file diagnose/patch). \
SLOW; choose ONLY on an EXPLICIT multi-file signal: "the scripts/files/tests" \
(plural), a repository/project-wide change, a refactor across modules, or several \
named source files. A SINGLE file — even authored from scratch — is ops. When \
unsure, choose ops.

profile — what KIND of end state the task produces (for completion verification):
  - service: starts a running service/daemon/server (listens on a port, serves).
  - data_transform: reshapes data from input to output (convert, reshard, count).
  - invertible: a reversible transform (compress, encrypt, encode, archive).
  - repair: fixes or debugs EXISTING code so it works.
  - answer: produces a specific answer VALUE written to a file (a count, a result).
  - plain: none of the above (configure, install, set permissions, a CTF flag).

Examples (UNRELATED tasks, for format only):
Task: "Convert /data/in.csv to /data/out.parquet." -> {"flow_set":"ops","profile":"data_transform"}
Task: "Compress /app/logs into logs.tar.gz." -> {"flow_set":"ops","profile":"invertible"}
Task: "Start an nginx server that logs requests to /var/log/app.log." -> {"flow_set":"ops","profile":"service"}
Task: "Count the science-domain tokens and write the integer to /app/answer.txt." -> {"flow_set":"ops","profile":"answer"}
Task: "The scripts in this pipeline fail; debug and fix them so the run completes." -> {"flow_set":"code_core","profile":"repair"}

Task:
{instruction}

Answer with ONLY a JSON object: {"flow_set": "...", "profile": "..."}"""

def _parse_judgment(text: str) -> tuple[str | None, str | None]:
    """(flow_set, profile) from the model's JSON answer (robust to a thinking
    preamble / code fence via the {...} extraction). Each field is validated
    against its label set; an unparseable reply yields (None, None) so the caller
    retries."""
    try:
        obj = json.loads(re.search(r"\{.*\}", text or "", re.DOTALL).group(0))
        if isinstance(obj, dict):
            fs, pr = obj.get("flow_set"), obj.get("profile")
            return (fs if fs in VALID_FLOW_SETS else None,
                    pr if pr in VALID_PROFILES else None)
    except Exception:
        pass
    return None, None


def _ask_llm(
    instruction: str, endpoint: str, timeout: float = 60.0
) -> tuple[str | None, str | None] | None:
    """One low-temp completion → (flow_set, profile) parsed from the answer, or
    None on any failure (unreachable, GraphQL error, fully unparseable)."""
    query = (
        "query Completion($request: CompletionRequest!) "
        "{ completion(request: $request) { text } }"
    )
    request = {
        "prompt": _JUDGE_PROMPT.replace("{instruction}", (instruction or "").strip()),
        "temperature": _JUDGE_TEMP,
        "maxTokens": 512,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                endpoint, json={"query": query, "variables": {"request": request}}
            )
            resp.raise_for_status()
            data = resp.json()
        if "errors" in data:
            return None
        text = data.get("data", {}).get("completion", {}).get("text", "")
        return _parse_judgment(text)
    except Exception:
        return None


def classify_flow_set(
    instruction: str,
    endpoint: str = "http://localhost:8008/graphql",
    log_path: Path | None = None,
) -> tuple[str, str, str]:
    """Decide the flow set AND capability profile for a task. Returns
    ``(flow_set, profile, method)`` where method ∈ {override, llm, default} for
    audit. Order: ask the LLM (retried up to _MAX_RETRIES on an unreachable/
    unparseable answer, run_session style) — an explicit OURO_FLOW_SET override
    wins the flow set; on retry exhaustion default to (ops, plain). Never raises.

    When ``log_path`` is given, write a one-line JSON record of the decision.
    """
    override = os.environ.get("OURO_FLOW_SET")
    # The LLM is the sole classifier; retry it for a valid flow set, keeping the
    # first valid profile we see along the way.
    llm_fs, llm_profile = None, None
    for _ in range(_MAX_RETRIES):
        result = _ask_llm(instruction, endpoint)
        if not result:
            continue
        fs, pr = result
        if pr in VALID_PROFILES and llm_profile is None:
            llm_profile = pr
        if fs in VALID_FLOW_SETS:
            llm_fs = fs
            break

    if override in VALID_FLOW_SETS:
        flow_set, method = override, "override"
    elif llm_fs in VALID_FLOW_SETS:
        flow_set, method = llm_fs, "llm"
    else:
        flow_set, method = "ops", "default"  # retry-exhausted safe default
    profile = llm_profile if llm_profile in VALID_PROFILES else "plain"

    if log_path is not None:
        try:
            rec = {
                "flow_set": flow_set,
                "profile": profile,
                "method": method,
                "instruction": (instruction or "")[:500],
            }
            Path(log_path).write_text(json.dumps(rec, indent=2))
        except Exception:
            pass
    return flow_set, profile, method
