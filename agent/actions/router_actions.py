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

import hashlib
import logging

from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput
from agent.session_injections import queue as queue_injection
from agent.session_loop import correction, observe, tool_budget
from agent.loader import load_prompt_text

logger = logging.getLogger(__name__)

# ONE budget number — kept in agreement with classify.cue's check_budget rule
# and the explore instruction template. The router SCOUTS (a few reads/commands
# to understand the shape), it does not fully diagnose.
# The scout loop's limits live in flows/shared/classify.cue
# (_classify_budget, _classify_correction_limit), enforced by its
# tool_loop_gate step; the seed states the budget from params.tool_budget
# (agent/session_loop.py).

VALID_FLOW_SETS = ("ops", "code_core")
VALID_PROFILES = (
    "service",
    "data_transform",
    "invertible",
    "repair",
    "answer",
    "plain",
)

SYSTEM_PROMPT = load_prompt_text("personas/router")

CONCLUDE_ROUTE_PROMPT = load_prompt_text("classify/conclude_route")


def _flow_key() -> str:
    return (
        f"classify:route:{hashlib.md5(SYSTEM_PROMPT.encode('utf-8')).hexdigest()[:10]}"
    )


async def _session_used(step_input: StepInput) -> int:
    """What the router session holds — the "used" side of the
    whole-if-it-fits rule (agent/context_fit.py)."""
    from agent.context_fit import session_used

    return await session_used(
        step_input.effects, str(step_input.context.get("router_session_id", ""))
    )


def _correction(step_input: StepInput, msg: str) -> StepOutput:
    """A failed scout action (agent/session_loop.py)."""
    return correction(
        step_input, msg, corrections_key="router_corrections", label="router"
    )


def _observe(step_input: StepInput, message: str) -> StepOutput:
    """A scout action that ran (agent/session_loop.py)."""
    return observe(step_input, message, turn_key="router_turn", label="router scout")


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
    # No client-side retry here: start_inference_session already WAITS
    # backend_timeout (180s) for a free instance server-side before raising
    # "busy", so a client retry just re-waits another 180s — actively harmful.
    # A busy open means a leaked session from the previous mission is pinning
    # the single-instance pool; that's fixed at the source by draining
    # open sessions on mission exit (LocalEffects.end_open_inference_sessions,
    # called from every run_agent call site). On the rare genuine failure we
    # fall to the (ops, plain) default (session_started=False).
    try:
        session_id = await effects.start_inference_session(
            {"ttl_seconds": 600}, static_prefix=SYSTEM_PROMPT, flow_key=_flow_key()
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to start router session: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to start router session: {e}",
        )

    from agent.context_fit import estimate_tokens, output_view

    mission = step_input.context.get("mission")
    objective = str(getattr(mission, "objective", "") or "").strip() or "(no objective)"
    parts = [
        SYSTEM_PROMPT,
        "",
        "## The task to route",
        await output_view(
            effects,
            objective,
            used=estimate_tokens(SYSTEM_PROMPT),
            save_path=f".agent/outputs/router-{session_id}-objective.txt",
            label="the objective",
        ),
        "",
        "Investigate the workspace"
        + (f" (up to {budget} actions)" if (budget := tool_budget(step_input)) else "")
        + ", then conclude with the flow_set, profile, and findings. Start by "
        "seeing what is here and where the task points.",
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
    """read_file tool (read-only): the named file, or one part of it
    (`path:<Symbol>`, `path:/pointer`, `path:<first>-<last>`), WHOLE when it
    fits the session, else the file's index to read a part from."""
    from agent.context_fit import read_file_view

    ref = str(step_input.context.get("router_choice_arg", "") or "").strip()
    view, error = await read_file_view(
        step_input.effects, ref, used=await _session_used(step_input)
    )
    if error:
        return _correction(step_input, error)
    return _observe(step_input, view)


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
    from agent.context_fit import output_view

    sid = str(step_input.context.get("router_session_id", "") or "session")
    turn = int(step_input.context.get("router_turn", 0) or 0)
    shown = await output_view(
        step_input.effects,
        out.strip(),
        used=await _session_used(step_input),
        save_path=f".agent/outputs/router-{sid}-{turn}-run.txt",
        label="the command output",
    )
    return _observe(step_input, f"Observation:\n$ {cmd}\n[exit {rc}]\n{shown}")


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
                findings = str(parsed.get("findings", "") or "")
                break
            logger.warning(
                "Router conclude attempt %d/3: no valid flow_set in response",
                attempt + 1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Router conclude attempt %d/3 failed (%s)", attempt + 1, e)

    # Answer-profile override: an ANSWER end state routed to code_core is the
    # chess-best-move trap (canary 2026-07-21, reproduced on rerun) — the build
    # flow decomposes a one-answer task into a general pipeline, burns the
    # task budget mid-build, and forfeits the ops completion gates that would
    # have driven at the required artifact. Code is instrumental for answers;
    # ops writes code in the terminal when needed.
    if flow_set == "code_core" and profile == "answer":
        flow_set, method = "ops", f"{method}+answer-override"
        findings = (
            "[router override: answer-profile task routed ops — produce the "
            "required artifact; write code in the terminal as needed] " + findings
        )
        logger.warning(
            "Router: answer-profile task downgraded code_core → ops "
            "(the deliverable is an answer, not software)"
        )

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
