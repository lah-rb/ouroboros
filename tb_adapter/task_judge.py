"""Per-task flow-set router + capability profile (the M3 task judge).

terminal-bench is not one task type. ~half the suite is software authoring/repair
(write or fix source code — code_core's diagnose→file_ops→patch→quality_gate is
built for this), the rest is terminal-accomplish (install/configure/run/extract/
CTF — ops's single-pass operator brief fits). Routing each task to the flow set
that fits is the M3 step of the container-target plan.

The judge emits TWO labels in ONE cold-temperature LLMVP completion: the
``flow_set`` (ops|code_core — the routing decision) AND a capability ``profile``
(service|data_transform|invertible|repair|answer|plain) that GATES which
completion oracles fire downstream (the oracle_actions rungs read it off the
mission). Both fall back to deterministic keyword heuristics — independently —
when LLMVP is unreachable or the answer is unparseable, and an explicit
``OURO_FLOW_SET`` env override is a hard escape hatch for the flow set. The
decision is returned with the method that produced it so the adapter can log it.

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

# Heuristic fallback: code_core ONLY on an explicit MULTI-FILE signal.
_MULTIFILE_SIGNALS = re.compile(
    r"""(?ix)
      \b(the\ (?:scripts|files|modules|tests|programs)
        | (?:multiple|several|across\ (?:the\ )?)(?:files|modules|scripts)
        | repository|repo-wide|repository-wide|codebase|project-wide
        | refactor\ .{0,40}?(?:across|modules|files)
        | failing\ tests)\b
    """,
    re.VERBOSE,
)

# Profile heuristics — most-specific first; the LLM is primary, this is the net.
_PROFILE_PATTERNS = [
    ("service", re.compile(
        r"(?i)\b(server|serve|daemon|listen(?:ing)?|on\ port|nginx|jupyter|uvicorn"
        r"|gunicorn|flask|systemd|sshd|web\ ?server|start\ (?:the\ |a\ )?\w+\ ?server)\b")),
    ("invertible", re.compile(
        r"(?i)\b(compress|decompress|encrypt|decrypt|encode|decode|gzip|tarball"
        r"|\btar\b|\bzip\b|unzip|archive)\b")),
    ("repair", re.compile(
        r"(?i)\b(fix|debug|repair|broken|failing|won'?t\ (?:run|work)|make\ .{0,30}?pass"
        r"|resolve\ the\ (?:error|bug|failure))\b")),
    ("answer", re.compile(
        r"(?i)\b(how\ many|write\ the\ (?:integer|number|count|result|answer|value)"
        r"|output\ the\ (?:value|number|count)|answer\.txt|final\ answer)\b")),
    ("data_transform", re.compile(
        r"(?i)\b(convert|reshard|reshape|transform|parquet|to\ csv|tokeniz"
        r"|count\ (?:the\ )?(?:tokens|rows|records)|aggregate|resample|merge\ .{0,20}?data)\b")),
]


def _heuristic(instruction: str) -> str:
    """Deterministic flow-set fallback: code_core only on a multi-file signal."""
    return "code_core" if _MULTIFILE_SIGNALS.search(instruction or "") else "ops"


def _heuristic_profile(instruction: str) -> str:
    """Deterministic profile fallback: first matching pattern, else 'plain'."""
    s = instruction or ""
    for prof, pat in _PROFILE_PATTERNS:
        if pat.search(s):
            return prof
    return "plain"


def _parse(text: str) -> str | None:
    """Pull a flow-set label out of the model's answer (robust to a thinking
    preamble or a code fence). code_core is checked first (more specific)."""
    low = (text or "").lower()
    if "code_core" in low:
        return "code_core"
    if re.search(r"\bops\b", low):
        return "ops"
    return None


def _parse_profile(text: str) -> str | None:
    """Pull a profile label out of the answer (substring match, specific first)."""
    low = (text or "").lower()
    for prof in ("data_transform", "invertible", "service", "repair", "answer", "plain"):
        if prof in low:
            return prof
    return None


def _parse_judgment(text: str) -> tuple[str | None, str | None]:
    """(flow_set, profile) from the JSON answer; falls back to substring matching
    on each field independently so a non-JSON reply still yields what it can."""
    flow_set, profile = None, None
    try:
        obj = json.loads(re.search(r"\{.*\}", text or "", re.DOTALL).group(0))
        if isinstance(obj, dict):
            fs, pr = obj.get("flow_set"), obj.get("profile")
            flow_set = fs if fs in VALID_FLOW_SETS else None
            profile = pr if pr in VALID_PROFILES else None
    except Exception:
        pass
    return flow_set or _parse(text), profile or _parse_profile(text)


def _ask_llm(
    instruction: str, endpoint: str, timeout: float = 60.0
) -> tuple[str | None, str | None] | None:
    """One cold-temp completion → (flow_set, profile) parsed from the answer, or
    None on any failure (unreachable, GraphQL error, fully unparseable)."""
    query = (
        "query Completion($request: CompletionRequest!) "
        "{ completion(request: $request) { text } }"
    )
    request = {
        "prompt": _JUDGE_PROMPT.replace("{instruction}", (instruction or "").strip()),
        "temperature": 0.0,
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
    ``(flow_set, profile, method)`` where method ∈ {override, llm, heuristic} for
    audit. Order: explicit env override (flow_set only) → LLM judge → keyword
    heuristic. Never raises; worst case is (ops, plain, heuristic). Each label
    falls back to its own heuristic independently.

    When ``log_path`` is given, append a one-line JSON record of the decision.
    """
    override = os.environ.get("OURO_FLOW_SET")
    if override in VALID_FLOW_SETS:
        flow_set, method = override, "override"
        profile = _heuristic_profile(instruction)
    else:
        result = _ask_llm(instruction, endpoint)
        if result and result[0] in VALID_FLOW_SETS:
            flow_set, method = result[0], "llm"
            profile = result[1] if result[1] in VALID_PROFILES else _heuristic_profile(instruction)
        else:
            flow_set, method = _heuristic(instruction), "heuristic"
            profile = _heuristic_profile(instruction)

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
