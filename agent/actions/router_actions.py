"""Exploratory router — investigate the task, then route (flow_set + profile).

The `classify` flow used to pick flow_set + profile from two blind menu turns
on the problem statement alone, then a deterministic floor forced repair →
code_core. Routing blind is doomed (the langcodes data: forced code_core passed
1/3, ops passed 3/3 — a small localized fix is ops's sweet spot), and
"small-local vs multi-file" is a SOFT attribute best decided by a scoped LLM
WITH context.

So the router now investigates first: a bounded read-only REACT loop (the
escalate.cue skeleton minus write_file) that runs commands and reads files in
the mission's real workspace, then concludes with {flow_set, profile, findings}.
The findings persist onto the mission (router_findings) to give the routed flow
a warm start. Config overrides (OURO_FLOW_SET / explicit flow_set) still
short-circuit classify entirely; held_out_tests stays config — those are the
hard, deterministic gates and sit first. This is the soft one, so it's informed
inference, no post-hoc override.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput
from agent.session_injections import queue as queue_injection

logger = logging.getLogger(__name__)

# ONE budget number — kept in agreement with classify.cue's check_budget rule
# and the explore instruction template. The router SCOUTS (a few reads/commands
# to understand the shape), it does not fully diagnose.
MAX_ROUTER_EXPLORE_TURNS = 5
MAX_ROUTER_CORRECTIONS = 4

VALID_FLOW_SETS = ("ops", "code_core")
VALID_PROFILES = ("service", "data_transform", "invertible", "repair", "answer", "plain")

SYSTEM_PROMPT = """\
---ACT AS---
You are the task router for an autonomous coding agent. BEFORE routing, you
investigate the task in its real workspace to understand what it actually needs.
You are READ-ONLY: you scout, you never edit. Work one action at a time:
  - run a command to see the state (ls, grep, cat, run the failing test, git status)
  - read a file to see what is actually there
  - conclude once you understand enough to route (usually only a few actions)
Be quick — you are scouting to make a good routing decision and hand the next
stage a head start, not fixing anything.
---END---"""

CONCLUDE_ROUTE_PROMPT = (
    "You have investigated enough. Route the task. Return a JSON object inside a "
    "fenced code block with these fields:\n\n"
    "  flow_set — \"ops\" or \"code_core\":\n"
    "    ops = a LOCALIZED fix (one file / one symbol) OR a produce/operate task "
    "(author a new file, install, run, configure, transform data, start a "
    "service, a CTF). ops works fast in a terminal, iterating against live "
    "output. Choose ops when the change is small and local, or the task is not "
    "about repairing existing multi-file code.\n"
    "    code_core = a change spanning MULTIPLE files/modules, a diffuse fix, or "
    "one needing cross-file understanding and a verified surgical patch. Slower "
    "and heavier; choose it only when the work is genuinely multi-file or the "
    "localization is not obvious from what you found.\n"
    "  profile — one of: service | data_transform | invertible | repair | answer "
    "| plain (the KIND of end state the task produces).\n"
    "  findings — 2-4 sentences for the NEXT stage: what the task needs, the "
    "specific file(s)/symbol(s) you identified, and whether it is localized "
    "(single-file) or diffuse (multi-file). A head start, not a full plan.\n\n"
    "Return ONLY the fenced JSON object."
)


def _flow_key() -> str:
    return f"classify:route:{hashlib.md5(SYSTEM_PROMPT.encode('utf-8')).hexdigest()[:10]}"


def _bounded(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " …[truncated]"


def _correction(step_input: StepInput, msg: str) -> StepOutput:
    """Queue a correction, bump the corrections counter (NOT the turn budget).
    Signals exhausted once the model oscillates past the cap."""
    corrections = int(step_input.context.get("router_corrections", 0) or 0) + 1
    updates: dict = {"router_corrections": corrections}
    queue_injection(updates, step_input.context, f"Action failed — {msg}")
    return StepOutput(
        result={"action_ok": False, "exhausted": corrections >= MAX_ROUTER_CORRECTIONS},
        observations=f"router correction ({corrections}): {msg[:120]}",
        context_updates=updates,
    )


def _observe(step_input: StepInput, message: str) -> StepOutput:
    """Queue an observation and bump the turn budget."""
    turn = int(step_input.context.get("router_turn", 0) or 0) + 1
    updates: dict = {"router_turn": turn}
    queue_injection(updates, step_input.context, message)
    return StepOutput(
        result={"action_ok": True},
        observations=f"router scout {turn}/{MAX_ROUTER_EXPLORE_TURNS}",
        context_updates=updates,
    )


async def action_open_router_session(step_input: StepInput) -> StepOutput:
    """Open the memoryful router session, seeded with the objective + the
    investigate-to-route brief.

    Context: mission (optional, for the objective).
    Publishes: inference_session_id, router_session_id, router_turn,
               router_corrections.
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects — cannot open router session",
        )
    # Retry the open (3×, backoff). The single-instance LLMVP pool (limit=1)
    # rejects an open when a straggler session from the previous mission's
    # teardown is still active ("All inference instances are busy … active=1,
    # limit=1") — a transient teardown/open race, since the router opens the
    # very first session of a mission right as the prior one tears down. A short
    # backoff lets the straggler release; only on real exhaustion do we fall to
    # the (ops, plain) default (session_started=False).
    session_id = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            session_id = await effects.start_inference_session(
                {"ttl_seconds": 600}, static_prefix=SYSTEM_PROMPT, flow_key=_flow_key()
            )
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("Router session open attempt %d/3 failed: %s", attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))  # 1.5s, then 3s
    if session_id is None:
        logger.error("Failed to start router session after 3 tries: %s", last_err)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to start router session (3 tries): {last_err}",
        )

    mission = step_input.context.get("mission")
    objective = str(getattr(mission, "objective", "") or "").strip() or "(no objective)"
    parts = [
        SYSTEM_PROMPT, "",
        "## The task to route",
        _bounded(objective, 4000),
        "",
        f"Investigate the workspace (up to {MAX_ROUTER_EXPLORE_TURNS} actions), then "
        "conclude with the flow_set, profile, and findings. Start by seeing what is "
        "here and where the task points.",
    ]
    updates: dict = {
        "inference_session_id": session_id,
        "router_session_id": session_id,
        "router_turn": 0,
        "router_corrections": 0,
    }
    queue_injection(updates, step_input.context, "\n".join(parts))
    logger.info("Router session started: %s", session_id)
    return StepOutput(
        result={"session_started": True},
        observations="Router exploration opened",
        context_updates=updates,
    )


async def action_router_read(step_input: StepInput) -> StepOutput:
    """read_file tool: inject a bounded view of the named file (read-only)."""
    effects = step_input.effects
    path = str(step_input.context.get("router_choice_arg", "") or "").strip()
    if not path:
        return _correction(step_input, "read_file needs a path argument.")
    try:
        fc = await effects.read_file(path)
    except Exception as e:  # noqa: BLE001
        return _correction(step_input, f"could not read {path}: {e}")
    if not getattr(fc, "exists", False):
        return _correction(step_input, f"{path} does not exist.")
    content = _bounded(getattr(fc, "content", "") or "", 6000)
    return _observe(step_input, f"Observation (read {path}):\n```\n{content}\n```")


async def action_router_run(step_input: StepInput) -> StepOutput:
    """run_command tool: /bin/sh -c the command in the mission's workspace,
    inject exit code + output. Read-only by discipline (the menu offers no
    write); a destructive command is on the model, but the router is prompted
    to scout, not mutate."""
    effects = step_input.effects
    cmd = str(step_input.context.get("router_choice_arg", "") or "").strip()
    if not cmd:
        return _correction(step_input, "run_command needs a command argument.")
    try:
        res = await effects.run_command(["/bin/sh", "-c", cmd], timeout=30)
    except Exception as e:  # noqa: BLE001
        return _correction(step_input, f"command failed to run: {e}")
    out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
    rc = getattr(res, "return_code", None)
    return _observe(step_input, f"Observation:\n$ {cmd}\n[exit {rc}]\n{_bounded(out, 4000)}")


async def action_conclude_route(step_input: StepInput) -> StepOutput:
    """One conclude turn → {flow_set, profile, findings}, informed by the whole
    exploration in the session. Fail-safe: an unparseable/invalid conclusion
    defaults to (ops, plain) with empty findings — the cheap path, and
    persist_routing + the routed flow still run.

    Context: router_session_id (required).
    Publishes: routed_flow_set, routed_profile, router_findings.
    """
    effects = step_input.effects
    session_id = step_input.context.get("router_session_id") or step_input.context.get(
        "inference_session_id"
    )
    flow_set, profile, findings, method = "ops", "plain", "", "default"
    # Retry the conclude (3×) — a bespoke session_inference doesn't inherit the
    # turn primitive's retries: 3 (that only covers steps with a `turn:` block,
    # e.g. run_session's plan_interaction). Same hand-rolled backstop as
    # task_judge: a malformed/withheld JSON conclusion retries rather than
    # failing loudly to the (ops, plain) default. Accept the first attempt whose
    # flow_set is valid.
    for attempt in range(3):
        try:
            res = await effects.session_inference(
                session_id, CONCLUDE_ROUTE_PROMPT, {"temperature": "t*0.2"}
            )
            text = getattr(res, "text", None) or str(res or "")
            parsed = parse_llm_json(text)
            if isinstance(parsed, dict) and parsed.get("flow_set") in VALID_FLOW_SETS:
                flow_set, method = parsed["flow_set"], "llm"
                pr = parsed.get("profile")
                profile = pr if pr in VALID_PROFILES else "plain"
                findings = str(parsed.get("findings", "") or "")[:1200]
                break
            logger.warning(
                "Router conclude attempt %d/3: no valid flow_set in response", attempt + 1
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Router conclude attempt %d/3 failed (%s)", attempt + 1, e)

    logger.info("Router: flow_set=%s profile=%s (%s)", flow_set, profile, method)
    return StepOutput(
        result={"flow_set": flow_set, "profile": profile, "method": method},
        observations=f"Routed to {flow_set} / {profile} ({method})",
        context_updates={
            "routed_flow_set": flow_set,
            "routed_profile": profile,
            "router_findings": findings,
        },
    )
