"""Interactive terminal actions — MCP-based PTY sessions.

Replaces the old terminal_actions.py. Uses the MCP terminal server for
all terminal interaction, supporting both shell commands and interactive
programs (games, REPLs, CLI tools that read from stdin).

Actions:
  - action_start_interactive_session: connect to MCP terminal, create PTY session
  - action_send_interaction: parse model's structured action, dispatch to MCP
  - action_close_interactive_session: close PTY session, produce transcript
  - action_execute_commands_batch_mcp: run a list of commands sequentially (for run_commands flow)
"""

from __future__ import annotations

import hashlib
import json
import logging

from agent.models import StepInput, StepOutput
from agent.loader import load_prompt_text

logger = logging.getLogger(__name__)

# RPC ceiling for send_input / read_output. The PTY settle's container-activity
# gate DEFERS the no-output backstop while a container is still working (a long
# --quiet download), up to CONTAINER_HARD_MAX_MS (~7min) server-side. This RPC
# timeout MUST exceed that ceiling or the client aborts the call mid-download
# (the original failure: settle deferred correctly, but the 60s RPC default cut
# it off). Local sessions return at their settle (~30s), so this is just an
# unused ceiling for them.
_INTERACT_RPC_TIMEOUT_S = 450.0


# Operator persona — hoisted into the session CHARTER (sent once, turn 1) rather
# than re-rendered as a per-turn `role` section. The persona is invariant across
# turns, so re-prefilling its ~370 tokens on every one of a session's turns was
# ~11% of the run's total fresh prefill for no benefit (plan_interaction is 78% of
# fresh prefill; this scaffolding is invariant). In the charter it lives in the
# session KV from turn 1 and is never re-prefilled. Mirrors the established pattern
# in diagnosis_session_actions.SYSTEM_PROMPT (persona in the session seed, §7).
# The BUILD-vs-OBSERVE distinction here is canonical — the formerly-duplicate
# `## Rules` block in plan_interaction_rules.yaml was dropped; its one unique
# nuance ("deprecation warnings are not failures") is folded in below.
OPERATOR_PERSONA = load_prompt_text("personas/operator")


# ── start_interactive_session ─────────────────────────────────────────


async def action_start_interactive_session(step_input: StepInput) -> StepOutput:
    """Start a PTY session via the MCP terminal server.

    Also starts a memoryful inference session so the model retains
    context across plan → execute → evaluate turns.

    Publishes: mcp_connection_id, mcp_session_id, inference_session_id,
               session_history
    """
    effects = step_input.effects
    params = step_input.params

    working_dir = params.get("working_directory") or "."
    env_vars = params.get("environment_vars")

    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects interface — cannot start terminal",
            context_updates={
                "mcp_connection_id": "",
                "mcp_session_id": "",
                "inference_session_id": "",
                "session_history": [],
            },
        )

    # Parse env_vars if string
    if isinstance(env_vars, str):
        try:
            env_vars = json.loads(env_vars)
        except (json.JSONDecodeError, ValueError):
            env_vars = None

    # Connect to the terminal MCP server (reuses existing connection)
    try:
        conn_id = await effects.mcp_connect("terminal")
    except Exception as e:
        logger.error("Failed to connect to terminal MCP server: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to connect to terminal MCP server: {e}",
            context_updates={
                "mcp_connection_id": "",
                "mcp_session_id": "",
                "inference_session_id": "",
                "session_history": [],
            },
        )

    # Create a PTY session. Activate the project's uv venv (if one exists) so the
    # program runs under the per-project interpreter — not whatever bare `python`
    # resolves to on the ambient PATH. The PTY merges these over its base env.
    pty_env: dict = dict(env_vars) if isinstance(env_vars, dict) else {}
    try:
        pty_env.update(effects.venv_env_overrides() or {})
    except Exception:  # noqa: BLE001 - activation is best-effort
        pass

    expected_prompt = params.get("expected_prompt", "")
    try:
        create_args = {
            "working_directory": working_dir,
            "env": pty_env or None,
        }
        if expected_prompt:
            create_args["expected_prompt"] = expected_prompt
        result = await effects.mcp_call_tool(
            conn_id,
            "create_session",
            create_args,
        )
        session_id = result.get("session_id", "")
    except Exception as e:
        logger.error("Failed to create PTY session: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to create PTY session: {e}",
            context_updates={
                "mcp_connection_id": conn_id,
                "mcp_session_id": "",
                "inference_session_id": "",
                "session_history": [],
            },
        )

    # Start memoryful inference session
    inference_session_id = ""
    if hasattr(effects, "start_inference_session"):
        try:
            # Pin the interact OPERATOR_PERSONA head across sessions (it leads
            # every charter seed) — later interact sessions skip re-prefilling it.
            _interact_key = f"interact:plan:{hashlib.md5(OPERATOR_PERSONA.encode('utf-8')).hexdigest()[:10]}"
            inference_session_id = await effects.start_inference_session(
                {"ttl_seconds": 300},
                static_prefix=OPERATOR_PERSONA,
                flow_key=_interact_key,
            )
        except Exception:
            inference_session_id = ""

    context_updates: dict = {
        "mcp_connection_id": conn_id,
        "mcp_session_id": session_id,
        "inference_session_id": inference_session_id,
        "session_history": [],
    }

    # Queue the operator persona + session charter (execution_persona) as a
    # session injection so the FIRST plan_interaction turn receives them prepended
    # to its prompt; runtime's _execute_turn_inference consumes session_injections
    # before its first inference call, after which they live in the session KV.
    # The persona is hoisted here (sent once) rather than re-rendered per turn —
    # the plan_interaction turn no longer carries a `role` section, so this is the
    # only place the model sees it. Both are invariant, so they cost prefill once
    # instead of every turn.
    session_goal = params.get("session_goal", "")
    if isinstance(session_goal, str) and session_goal.strip() and inference_session_id:
        from agent.session_injections import queue as queue_injection

        seed = (
            f"{OPERATOR_PERSONA}\n\n"
            f"---TEST CHARTER---\n{session_goal.strip()}\n---END CHARTER---"
        )
        queue_injection(context_updates, step_input.context, seed)
        logger.info(
            "Queued persona + session charter for inference session %s (%d chars)",
            inference_session_id,
            len(seed),
        )

    return StepOutput(
        result={
            "session_started": True,
            "session_id": session_id,
        },
        observations=f"Interactive session started: pty={session_id}"
        + (f" (memoryful: {inference_session_id})" if inference_session_id else ""),
        context_updates=context_updates,
    )


# ── send_interaction ──────────────────────────────────────────────────

# Above this many chars, a turn's FULL output is persisted to a workspace file
# (Guard G3) so the agent can grep it via its shell — the bytes survive even for
# one-shot commands the bounded prompt view can't reproduce. Under /tmp so it
# never pollutes the graded workspace.
_SAVE_OUTPUT_THRESHOLD = 4000


async def _save_full_output(effects, output: str, turn) -> str | None:
    """Persist a large turn's full output to /tmp/.ouro_out/t<turn>.log so the
    agent can `grep` it. Best-effort; returns the path or None."""
    if effects is None or not output or len(output) <= _SAVE_OUTPUT_THRESHOLD:
        return None
    path = f"/tmp/.ouro_out/t{turn}.log"
    try:
        await effects.write_file(path, output)
        return path
    except Exception:
        return None


# Identical (input, output) exchanges required before the next identical send
# is called stuck. Operator-set (2026-08-07): 5 — genuine circling, not the
# 2-3 repeats navigation legitimately needs.
_STUCK_IDENTICAL_RUNS = 4  # 4 identical priors → the 5th send trips


async def action_send_interaction(step_input: StepInput) -> StepOutput:
    """Parse the model's structured interaction and dispatch to MCP.

    The model outputs a JSON action:
      {"action": "shell_command", "command": "python main.py"}
      {"action": "send_input", "text": "go north\\n"}
      {"action": "read_output"}
      {"action": "close", "reason": "goal achieved"}

    Dispatches to the appropriate MCP tool, appends to session_history.
    """
    effects = step_input.effects
    conn_id = step_input.context.get("mcp_connection_id", "")
    session_id = step_input.context.get("mcp_session_id", "")
    session_history = list(step_input.context.get("session_history", []))
    inference_response = step_input.context.get("inference_response", "")

    if not effects or not conn_id or not session_id:
        return StepOutput(
            result={"command_sent": False},
            observations="No effects, connection, or session",
            context_updates={
                "mcp_session_id": session_id,
                "session_history": session_history,
            },
        )

    # Extract the action. Runtime's menu_compound path already
    # published the chosen option key as `planned_action` and the arg
    # as `planned_action_arg` — prefer those (single source of truth
    # with the schema). Fall back to re-parsing the raw response for
    # cases where this action is called outside a menu_compound turn
    # (tests, legacy paths, read_output extras like settle_ms that
    # aren't modeled in the single-arg schema).
    #
    # Prior to this (pre-76d-round), the action unconditionally re-
    # parsed `inference_response` with its own `_parse_interaction_action`,
    # running a second parser in parallel with the runtime's menu_arg
    # extractor. Both parsers agreed by convention (schema arg.name
    # "command" matched hardcoded "command" here) but drift between
    # schema and this code would be silent. The static-analysis audit
    # flagged `planned_action` and `planned_action_arg` as published-
    # but-never-consumed, which is how we found this.
    planned_action = step_input.context.get("planned_action")
    planned_action_arg = step_input.context.get("planned_action_arg")

    action_data: dict | None = None
    if planned_action:
        action_data = {"choice": planned_action}
        if planned_action_arg is not None:
            # For the three canonical actions, the arg is the option's
            # declared `arg.name` from the schema — "command", "text",
            # or "reason". Hydrate under all three names so the branch
            # logic below (which reads by specific key) finds the value
            # regardless of which action fired. The branches are
            # mutually exclusive, so double-writing is safe.
            for arg_key in ("command", "text", "reason"):
                action_data[arg_key] = planned_action_arg

    # Still may need to parse raw for read_output timing extras (settle_ms,
    # timeout_ms) that the schema doesn't model, or for test/legacy paths
    # where the runtime hasn't published these keys.
    if action_data is None:
        action_data = _parse_interaction_action(str(inference_response))

    if not action_data:
        return StepOutput(
            result={"command_sent": False, "parse_failed": True},
            observations=f"Could not parse interaction from: {str(inference_response)[:200]}",
            context_updates={
                "mcp_session_id": session_id,
                "session_history": session_history,
            },
        )

    action_type = action_data.get("choice", "")

    # ── Close action: signal session done ──────────────────────
    if action_type == "close":
        reason = action_data.get("reason", "session complete")
        return StepOutput(
            result={
                "command_sent": False,
                "session_done": True,
                "close_reason": reason,
            },
            observations=f"Model requested close: {reason}",
            context_updates={
                "mcp_session_id": session_id,
                "session_history": session_history,
            },
        )

    # ── Read output: poll without sending ──────────────────────
    if action_type == "read_output":
        try:
            result = await effects.mcp_call_tool(
                conn_id,
                "read_output",
                {
                    "session_id": session_id,
                    # Settle / timeout defaults raised after 2d7 round —
                    # 10s timeout was tight for Python subprocesses doing
                    # post-IO work (YAML load, state serialization). A
                    # timed-out read was substituting the empty-output
                    # annotation into the transcript, the evaluator saw
                    # "no prompt detected" instead of the subsequent
                    # real output, and a correct fix was scored failing.
                    # 30s covers typical interpreter warm-up + world-load
                    # without impacting genuinely-hung detection (which
                    # needs multiple consecutive no-output reads, not a
                    # single short timeout).
                    "settle_ms": action_data.get("settle_ms", 750),
                    "timeout_ms": action_data.get("timeout_ms", 30000),
                },
                timeout=_INTERACT_RPC_TIMEOUT_S,
            )
        except Exception as e:
            logger.error("read_output failed: %s", e)
            result = {"output": f"ERROR: {e}", "status": "error"}

        entry = {
            "turn": len(session_history),
            "action": "read_output",
            "output": result.get("output", ""),
            "status": result.get("status", "error"),
        }

        # ── Prompt-aware annotation for read_output ──────────
        # 6c2 round — like send_input, use flow-profile metadata to
        # distinguish "program responded, no prompt tag" from
        # "nothing arrived in read window". read_output has no
        # command echo to hide behind, so empty output is common;
        # the question is whether the stream PROFILE suggests the
        # program is active (bytes arrived, just without a prompt
        # tag) vs genuinely idle.
        child_alive = result.get("interactive_child", False)
        prompt_found = result.get("prompt_detected", False)
        status = result.get("status", "error")
        settled_cleanly = result.get("settled_cleanly", False)
        total_bytes = result.get("total_bytes_received", 0)

        if settled_cleanly and entry["output"].strip():
            # Response complete with output — don't annotate, the
            # output speaks for itself. This is the 6c2 primary
            # failure case: the game DID respond, we DID capture
            # bytes, old logic annotated "no prompt detected" on
            # top.
            pass
        elif status == "no_new_output" and child_alive:
            # Buffer was empty the entire wait — nothing arrived.
            # Either we drained the response on the previous turn
            # (and the program is idle) or the program truly has
            # nothing to say. Signal-level annotation.
            entry["output"] = (
                "[signal: no bytes arrived during read window — "
                "the previous response may already be captured, or "
                "the program is idle without a prompt tag]"
            )
            logger.info(
                "Turn %d: no bytes arrived on read_output (total_bytes=%d)",
                entry["turn"],
                total_bytes,
            )
        elif (
            child_alive
            and not entry["output"].strip()
            and status in ("settled", "timeout")
        ):
            # Output arrived (or status is post-wait) but there is
            # no printable content after stripping. With
            # settled_cleanly False here (handled above), this is
            # the case where either output came in but stripped to
            # nothing, or timeout fired with output still in
            # flight.
            if total_bytes > 0 and not settled_cleanly:
                entry["output"] = (
                    "[signal: output still arriving at timeout "
                    "— program is producing but did not idle "
                    "within the read window]"
                )
            else:
                entry["output"] = (
                    "[signal: read window elapsed with no "
                    "capturable output — program state undetermined]"
                )
            logger.info(
                "Turn %d: read_output empty output " "(bytes=%d, clean=%s, status=%s)",
                entry["turn"],
                total_bytes,
                settled_cleanly,
                status,
            )

        entry["output_file"] = await _save_full_output(
            effects, entry.get("output", ""), entry["turn"]
        )
        session_history.append(entry)

        return StepOutput(
            result={
                "command_sent": True,
                "status": result.get("status", "error"),
                "process_exited": result.get("status") == "process_exited",
            },
            observations=f"Read output: status={result.get('status')}, "
            f"{len(result.get('output', ''))} chars",
            context_updates={
                "mcp_session_id": session_id,
                "session_history": session_history,
            },
        )

    # ── Send input or shell command ────────────────────────────
    # Both are line-oriented: the receiving program (shell, or an input()/
    # readline REPL) blocks until it sees the newline that terminates the line.
    # Guarantee one trailing newline rather than trusting the model — or the
    # response pipeline — to preserve it. (A dropped trailing \n was the
    # input-side "phantom": the program sat in read() forever, producing no
    # output, and the harness reported a hang that wasn't one. Root-fixed in
    # parse_llm_json; this is the belt-and-suspenders guarantee at the boundary.)
    if action_type == "shell_command":
        text = action_data.get("command", "")
        # apply_patch guard: `apply_patch` is a codex-harness verb, NOT a shell
        # command — in these containers it no-ops with "command not found",
        # silently leaving the file unchanged. The fsspec/astropy testers
        # burned turns "patching" via apply_patch heredocs that never applied,
        # then reported the unchanged file as a failure. Intercept it, don't
        # send it, and steer back to the tester's actual role: verify + report,
        # never edit. (Editing is a separate flow's job.)
        if text.strip().startswith("apply_patch"):
            return StepOutput(
                result={"command_sent": False, "apply_patch_blocked": True},
                observations=(
                    "apply_patch is not available in this environment and does "
                    "nothing here — the file was NOT changed. You are verifying, "
                    "not editing: do not attempt to modify files. Continue "
                    "exercising the behavior and REPORT what you observe; a "
                    "defect you find is reported, not fixed, in this session."
                ),
                context_updates={
                    "mcp_session_id": session_id,
                    "session_history": session_history,
                },
            )
        if text and not text.endswith("\n"):
            text += "\n"
    elif action_type == "send_input":
        text = action_data.get("text", "")
        if text and not text.endswith("\n"):
            text += "\n"
    else:
        return StepOutput(
            result={"command_sent": False, "unknown_action": action_type},
            observations=f"Unknown action type: {action_type}",
            context_updates={
                "mcp_session_id": session_id,
                "session_history": session_history,
            },
        )

    if not text:
        return StepOutput(
            result={"command_sent": False},
            observations="Empty input text",
            context_updates={
                "mcp_session_id": session_id,
                "session_history": session_history,
            },
        )

    # ── Stuck detection: repeated input WITHOUT progress ───────────────
    # A bare duplicate-input check killed every post-prologue session on
    # hy3 (2026-08-07): navigation games legitimately repeat commands
    # ("go north" three times in a row walks Cave Mouth → Crossroads), and
    # the detector closed the session on the SECOND consecutive move —
    # "ended after step 2 of 10", thirty-plus times, immune to every
    # prompt fix because the persona never chose to stop. Stuck means
    # repetition with IDENTICAL output — no state change — and the operator
    # set the bar at FIVE (2026-08-07): the same input has already run
    # _STUCK_IDENTICAL_RUNS times producing byte-identical responses, and
    # the tester is sending it yet again. "At that point it really looks
    # like circling, not productive terminal time." Repeated input whose
    # output changes is progress; the turn budget bounds the rest.
    if len(session_history) >= _STUCK_IDENTICAL_RUNS:
        tail = session_history[-_STUCK_IDENTICAL_RUNS:]
        outputs = {(e.get("output", "") or "").strip() for e in tail}
        same_run = (
            all((e.get("input", "") or "").strip() == text.strip() for e in tail)
            and len(outputs) == 1
        )
        if text.strip() and same_run:
            return StepOutput(
                result={
                    "command_sent": False,
                    "stuck_detected": True,
                    "duplicate_input": text.strip(),
                },
                observations=(
                    f"Stuck: '{text.strip()[:60]}' repeated with identical "
                    f"output {_STUCK_IDENTICAL_RUNS} times — no state change"
                ),
                context_updates={
                    "mcp_session_id": session_id,
                    "session_history": session_history,
                },
            )

    # ── Send via MCP ───────────────────────────────────────────
    # Shell commands need a longer settle — the child process may take
    # time to start producing output (e.g., Python interpreter init).
    default_settle = 1000 if action_type == "shell_command" else 500
    try:
        result = await effects.mcp_call_tool(
            conn_id,
            "send_input",
            {
                "session_id": session_id,
                "text": text,
                "await_response": True,
                "settle_ms": action_data.get("settle_ms", default_settle),
                "timeout_ms": action_data.get("timeout_ms", 30000),
            },
            timeout=_INTERACT_RPC_TIMEOUT_S,
        )
    except Exception as e:
        logger.error("send_input failed: %s", e)
        result = {"output": f"ERROR: {e}", "status": "error"}

    entry = {
        "turn": len(session_history),
        "action": action_type,
        "input": text.strip(),
        "output": result.get("output", ""),
        "status": result.get("status", "error"),
        # Whether a launched program (not the shell) still owns the terminal.
        # Recorded per turn so the pre-close gate can tell "resume by driving
        # the running program" from "resume by launching it again" without a
        # second round trip — sending the launch command to a LIVE program
        # would type it into the program's own stdin.
        "child_running": bool(result.get("interactive_child", False)),
    }
    if result.get("exit_code") is not None:
        entry["exit_code"] = result["exit_code"]

    # ── Prompt-aware annotation for send_input / shell_command ────
    #
    # 6c2 round — the PTY layer now publishes flow-profile metadata
    # (total_bytes_received, peak_rate_bps, idle_duration_s,
    # settled_cleanly). We use those to decide whether an ambiguous
    # "may be processing or waiting" annotation is warranted, rather
    # than inferring from the coarse {status, prompt_found} pair.
    #
    # The old logic fired the annotation when status was settled/
    # timeout/no_new_output AND prompt wasn't found AND output was
    # empty/echo-only. That conflated two distinct cases:
    #
    #   (a) Program responded with output that lacks a prompt tag at
    #       the tail — evaluator should see the response, not a
    #       pessimistic "no prompt detected" annotation layered on
    #       top of it.
    #   (b) Program produced no output at all in the wait window —
    #       genuinely ambiguous; annotation helps the reader
    #       understand what they're looking at.
    #
    # settled_cleanly distinguishes (a) from (b) directly: True →
    # we saw a burst-then-idle profile (response happened, maybe no
    # prompt tag); False → either no burst observed or program still
    # producing at timeout. Only case (b) deserves the ambiguous
    # annotation. Case (a) gets a softer note if the output lacks a
    # prompt tag, because the evaluator still benefits from knowing
    # the interaction state is undetermined — but the note no longer
    # asserts the program "may still be processing" when we have
    # direct evidence it produced a response and went idle.
    child_alive = result.get("interactive_child", False)
    prompt_found = result.get("prompt_detected", False)
    status = result.get("status", "error")
    settled_cleanly = result.get("settled_cleanly", False)
    total_bytes = result.get("total_bytes_received", 0)

    if (
        child_alive
        and status in ("settled", "timeout", "no_new_output")
        and not prompt_found
    ):
        output_content = entry["output"].strip()

        if action_type == "shell_command":
            # The PTY echoes the command text. If the output is just
            # the echo (roughly command length + shell prompt chars),
            # the program hasn't produced meaningful output yet.
            cmd_echo_threshold = len(text.strip()) + 20
            if len(output_content) <= cmd_echo_threshold:
                entry["output"] = (
                    output_content
                    + "\n[process started — output settled but no prompt detected; "
                    "program may still be initializing or waiting for input]"
                ).strip()
                logger.info(
                    "Turn %d: interactive child running after shell_command "
                    "(no prompt detected, output is echo-only)",
                    entry["turn"],
                )
        elif action_type == "send_input":
            # 6c2 — three cases to distinguish:
            #   (1) settled_cleanly AND output present → program
            #       responded, no prompt tag. Leave the output
            #       alone; evaluator reads the response directly.
            #   (2) settled_cleanly AND output empty (rare — implies
            #       a burst that drained to whitespace after
            #       stripping). Note it as "responded without
            #       capturable output" rather than "may be
            #       processing".
            #   (3) NOT settled_cleanly AND output empty → truly
            #       ambiguous. Kept the original annotation phrasing
            #       but tag it so the evaluator knows this is a
            #       signal-level observation, not a claim about
            #       program state.
            if settled_cleanly and output_content:
                # Case (1) — no annotation needed. Response is the
                # output.
                pass
            elif settled_cleanly and not output_content:
                # Case (2) — response completed with no printable
                # content. Unusual but possible (e.g., program
                # consumed input, did work, returned to its read
                # without printing). Still a valid "responded"
                # state; don't imply ongoing processing.
                entry["output"] = (
                    "[response complete — program produced no "
                    "printable output for this input and is idle]"
                )
                logger.info(
                    "Turn %d: clean-settle with empty output after "
                    "send_input (%d bytes received)",
                    entry["turn"],
                    total_bytes,
                )
            elif not output_content:
                # Case (3) — no clean settle AND no output. True
                # ambiguity. The annotation describes what the
                # signal looked like, not what the program is
                # doing.
                if total_bytes == 0:
                    entry["output"] = (
                        "[signal: no bytes arrived during read window "
                        "— program may have consumed input silently, "
                        "or is genuinely idle without a prompt tag]"
                    )
                else:
                    # Bytes arrived but never went idle for the
                    # settle window — program still producing when
                    # timeout hit.
                    entry["output"] = (
                        "[signal: output still arriving at timeout "
                        "— program is producing but did not idle "
                        "within the read window]"
                    )
                logger.info(
                    "Turn %d: ambiguous signal after send_input "
                    "(bytes=%d, clean=%s, status=%s)",
                    entry["turn"],
                    total_bytes,
                    settled_cleanly,
                    status,
                )

    entry["output_file"] = await _save_full_output(
        effects, entry.get("output", ""), entry["turn"]
    )
    session_history.append(entry)

    context_updates = {
        "mcp_session_id": session_id,
        "session_history": session_history,
    }
    # Remember the session's first shell command — it launched the
    # program, and ask_relaunch/relaunch_program replay it verbatim when
    # the model needs another program run (e.g. to verify a save loads).
    if action_type == "shell_command" and not step_input.context.get("launch_command"):
        context_updates["launch_command"] = text.strip()

    return StepOutput(
        result={
            "command_sent": True,
            "status": result.get("status", "error"),
            "process_exited": result.get("status") == "process_exited",
            "stuck_detected": False,
        },
        observations=f"Turn {entry['turn']}: {action_type} → "
        f"status={result.get('status')}, {len(result.get('output', ''))} chars",
        context_updates=context_updates,
    )


def _parse_interaction_action(raw: str) -> dict | None:
    """Extract a structured interaction action from model response.

    Expects the Step C menu_compound schema: ``{"choice": "...", ...}``
    where ``choice`` is the option key (``shell_command``, ``send_input``,
    ``close``) and the sibling key is the option's argument
    (``command``, ``text``, ``reason``).

    Uses parse_llm_json for robustness against malformed LLM output
    (trailing commas, missing quotes, incomplete objects, thinking preamble).
    Returns None if no valid action can be extracted — the caller
    handles retries or session close.
    """
    from agent.llm_json import parse_llm_json

    obj = parse_llm_json(raw)
    if isinstance(obj, dict) and "choice" in obj:
        return obj
    return None


# ── close_interactive_session ─────────────────────────────────────────


async def action_close_interactive_session(step_input: StepInput) -> StepOutput:
    """Close the PTY session, optionally preserving the inference session.

    When params.keep_inference_session is True, the inference session
    is left alive so the calling flow can run evaluation inside the
    same KV cache context. The caller must end the session explicitly
    via action_end_inference_session.

    Publishes: terminal_output, terminal_status, session_summary,
               inference_session_id (when kept alive)
    """
    effects = step_input.effects
    conn_id = step_input.context.get("mcp_connection_id", "")
    session_id = step_input.context.get("mcp_session_id", "")
    inference_session_id = step_input.context.get("inference_session_id", "")
    session_history = step_input.context.get("session_history", [])
    session_summary = step_input.context.get("session_summary", "")
    keep_inference = step_input.params.get("keep_inference_session", False)

    # Close PTY session
    close_result = {"success": False, "total_turns": 0, "transcript": ""}
    if effects and conn_id and session_id:
        try:
            close_result = await effects.mcp_call_tool(
                conn_id, "close_session", {"session_id": session_id}
            )
        except Exception as e:
            logger.warning("Failed to close PTY session: %s", e)

    # End inference session ONLY if not keeping it for caller evaluation
    if (
        not keep_inference
        and effects
        and inference_session_id
        and hasattr(effects, "end_inference_session")
    ):
        try:
            await effects.end_inference_session(inference_session_id)
        except Exception:
            pass

    # Build terminal output transcript from session history
    transcript_parts = []
    for entry in session_history:
        if isinstance(entry, dict):
            turn = entry.get("turn", "?")
            action = entry.get("action", "?")
            inp = entry.get("input", "")
            output = entry.get("output", "")
            transcript_parts.append(f"[Turn {turn}] ({action})")
            if inp:
                transcript_parts.append(f"  > {inp}")
            if output:
                transcript_parts.append(output)
    terminal_output = "\n".join(transcript_parts) if transcript_parts else ""

    # Preserve existing terminal_output from context if the transcript is empty.
    # This handles the run_commands path where execute_commands_batch publishes
    # terminal_output directly (not via session_history), and close_session
    # would otherwise overwrite it with an empty transcript.
    if not terminal_output:
        terminal_output = step_input.context.get("terminal_output", "")

    total_turns = close_result.get("total_turns", len(session_history))
    terminal_status = f"{total_turns} turns"

    summary = session_summary or f"Terminal session completed: {total_turns} turns"

    context_out = {
        "terminal_output": terminal_output,
        "terminal_status": terminal_status,
        "session_summary": summary,
        "command_count": total_turns,
    }

    # Pass the inference session ID back so the caller can evaluate
    # inside the same KV cache context.
    if keep_inference and inference_session_id:
        context_out["inference_session_id"] = inference_session_id

    return StepOutput(
        result={
            "session_closed": True,
            "total_turns": total_turns,
        },
        observations=summary,
        context_updates=context_out,
    )


# ── execute_commands_batch_mcp ────────────────────────────────────────


async def action_execute_commands_batch_mcp(step_input: StepInput) -> StepOutput:
    """Execute a list of commands sequentially via MCP terminal.

    Drop-in replacement for the old execute_commands_batch. Used by
    the run_commands flow for deterministic command execution.

    Params:
        commands: list of shell command strings
        stop_on_error: if true, stop on first non-zero exit code (default true)
        command_timeout: per-command timeout in seconds (default 30)

    Publishes: terminal_output, exit_codes, all_passed, command_count
    """
    effects = step_input.effects
    conn_id = step_input.context.get("mcp_connection_id", "")
    session_id = step_input.context.get("mcp_session_id", "")
    commands = step_input.params.get("commands", [])
    stop_on_error = step_input.params.get("stop_on_error", True)
    timeout_ms = int(step_input.params.get("command_timeout", 30)) * 1000

    if not effects or not conn_id or not session_id:
        return StepOutput(
            result={"command_sent": False},
            observations="No effects, connection, or session",
            context_updates={
                "terminal_output": "",
                "exit_codes": [],
                "all_passed": False,
            },
        )

    # Normalize commands
    if isinstance(commands, str):
        commands = [commands]
    if not isinstance(commands, list):
        commands = [str(commands)]

    output_parts = []
    exit_codes = []
    all_passed = True

    for cmd in commands:
        cmd = str(cmd).strip()
        if not cmd:
            continue

        try:
            result = await effects.mcp_call_tool(
                conn_id,
                "send_input",
                {
                    "session_id": session_id,
                    "text": cmd + "\n",
                    "await_response": True,
                    "settle_ms": 500,
                    "timeout_ms": timeout_ms,
                },
            )

            output = result.get("output", "")
            status = result.get("status", "error")
            exit_code = result.get("exit_code")

            output_parts.append(f"$ {cmd}")
            output_parts.append(output if output else "(no output)")

            # For shell commands, try to detect exit code from status
            # If process exited, use the exit_code. Otherwise assume
            # success if settled, failure if timeout/error.
            if exit_code is not None:
                exit_codes.append(exit_code)
                if exit_code != 0:
                    all_passed = False
            elif status == "settled":
                exit_codes.append(0)
            else:
                exit_codes.append(-1)
                all_passed = False

            if not all_passed and stop_on_error:
                output_parts.append(f"(stopping — status: {status})")
                break

        except Exception as e:
            output_parts.append(f"$ {cmd}")
            output_parts.append(f"ERROR: {e}")
            exit_codes.append(-1)
            all_passed = False
            if stop_on_error:
                break

    terminal_output = "\n".join(output_parts)

    return StepOutput(
        result={
            "command_sent": True,
            "commands_run": len(exit_codes),
            "all_passed": all_passed,
        },
        observations=f"Executed {len(exit_codes)} commands, all_passed={all_passed}",
        context_updates={
            "mcp_session_id": session_id,
            "terminal_output": terminal_output,
            "exit_codes": exit_codes,
            "all_passed": all_passed,
            "command_count": len(exit_codes),
        },
    )


# ── end_inference_session ────────────────────────────────────────────


async def action_end_inference_session(step_input: StepInput) -> StepOutput:
    """End a memoryful inference session, releasing the pinned pool instance.

    Used by interact after evaluation completes inside the session that
    was kept alive by run_session's close_session step.

    Safe to call when no session exists (no-ops gracefully).
    """
    effects = step_input.effects
    session_id = step_input.context.get("inference_session_id", "")

    if not effects or not session_id:
        return StepOutput(
            result={"session_ended": False},
            observations="No inference session to end",
        )

    if not hasattr(effects, "end_inference_session"):
        return StepOutput(
            result={"session_ended": False},
            observations="Effects interface lacks end_inference_session",
        )

    try:
        await effects.end_inference_session(session_id)
        # Clear the ambient inference_session_id so DOWNSTREAM inference steps
        # don't route through the now-dead session. inference_session_id is on
        # the ambient-context whitelist (runtime _AMBIENT_CONTEXT_KEYS), so it
        # flows into every later step automatically; without this, the quality
        # gate's `summarize` step (which runs right after end_ux_session) was
        # inheriting the released UX session and getting an instant empty
        # response (tokens_out=0), routing the gate to pass_empty and producing
        # NO findings — silently neutering the whole gate verdict.
        return StepOutput(
            result={"session_ended": True},
            observations=f"Inference session {session_id} ended",
            context_updates={"inference_session_id": ""},
        )
    except Exception as e:
        logger.warning("Failed to end inference session %s: %s", session_id, e)
        return StepOutput(
            result={"session_ended": False},
            observations=f"Failed to end session: {e}",
            context_updates={"inference_session_id": ""},
        )


# ══════════════════════════════════════════════════════════════════════
# Pre-close confirmation — multi-run test sessions, one menu
# ══════════════════════════════════════════════════════════════════════


# How many pre-close NOTICES a session gets. One: the first close draws the
# brief-check notice and returns to the plan menu; any later close is
# honoured without comment, so a close → notice → close ping-pong cannot
# loop. (v1 asked through a second menu with a cap of 2 — see the notice
# rationale on action_confirm_close_gate for why that design was replaced.)
_MAX_CLOSE_NOTICES = 1

CLOSE_NOTICE_PROMPT = load_prompt_text("run_in_terminal/close_notice")


def _last_child_running(session_history: list) -> bool:
    """Was an interactive program still on the terminal at the last turn?

    Read from the session's own history rather than probed live: the MCP
    result already carries it (``interactive_child`` in the terminal
    server's response), and re-probing from here would need another round
    trip to answer a question we were just told the answer to.
    """
    for entry in reversed(session_history or []):
        if not isinstance(entry, dict):
            continue
        if "child_running" in entry:
            return bool(entry.get("child_running"))
        status = str(entry.get("status", "") or "")
        if status == "process_exited":
            return False
        if status:
            # Any other completed turn without an explicit flag: fall back
            # to "still running", the pre-existing assumption.
            return True
    return False


def _render_close_notice(
    child_running: bool, launch_command: str, close_reason: str
) -> str:
    """Substitute the notice by literal token replacement (never str.format —
    the prompt text is free prose and one brace would KeyError at runtime,
    the same trap author_test's renderer documents)."""
    if child_running:
        state_line = "The program is still RUNNING and waiting for your input."
        resume_line = "Continue driving it with send_input, exactly as you have been."
    else:
        state_line = "The program has EXITED."
        resume_line = (
            f"Start it again with shell_command: `{launch_command}`"
            if launch_command
            else "Start it again with the shell_command you launched it with."
        )
    reason = (close_reason or "").strip()
    reason_line = (
        f'Your stated reason for closing was: "{reason[:200]}"'
        if reason
        else "You gave no reason for closing."
    )
    text = CLOSE_NOTICE_PROMPT
    for token, value in {
        "{state_line}": state_line,
        "{resume_line}": resume_line,
        "{reason_line}": reason_line,
    }.items():
        text = text.replace(token, value)
    return text


async def action_confirm_close_gate(step_input: StepInput) -> StepOutput:
    """First close: inject the brief-check notice and loop back to the plan
    menu. Any later close: honour it.

    EVERY model-chosen close routes here (operator, 2026-08-09). The
    original trigger (``process_exited``) was true ZERO times in 5,162
    interactions across the hy3 run, so the save → quit → relaunch → load →
    verify arc had never once been offered.

    WHY A NOTICE AND NOT A MENU (the v1 → v2 change, same day): v1 asked
    through a second menu (ask_resume: resume/conclude) and reproduced the
    779 menu-confusion class within an hour of first firing — 2 of 3 turns
    came back in PLAN vocabulary ({"choice": "send_input", ...}),
    unparseable, vs 0 of 66 at plan_interaction; and the fallout was
    perverse, because a plan-shape answer plainly means "keep testing" but
    fell through no_answer → close_session. One menu shape per session is
    the rule the 779 round already paid for. The notice rides
    session_injections (zero extra inference) into the next plan turn, and
    the model answers in the one vocabulary it never fumbles: shell_command
    to relaunch, send_input to keep driving, close again to confirm.

    WORDING HAZARD the notice text must carry: the runtime auto-injects
    "[Your previous selection of 'close' ... was accepted and executed.]"
    on every menu turn. The notice is queued behind it and must LEAD by
    contradicting it — "The session has NOT closed yet."

    Context: session_history, close_confirmations, inference_session_id,
    launch_command, planned_action_arg.  Result: should_notice.
    Publishes: close_confirmations.
    """
    from agent.session_injections import queue as queue_injection

    ctx = step_input.context
    noticed = int(ctx.get("close_confirmations", 0) or 0)
    child_running = _last_child_running(list(ctx.get("session_history") or []))

    # LOGGED, not just observed. Step observations do not reach the run log,
    # and that blind spot is exactly how the predecessor of this step sat
    # dead for 5,162 interactions without anyone noticing: "never fired" and
    # "fired and declined" read identically from outside. The decision rate
    # is the first number to check on any run.
    if noticed >= _MAX_CLOSE_NOTICES:
        logger.info(
            "confirm_close: notice already given (%d/%d) — honouring the close",
            noticed,
            _MAX_CLOSE_NOTICES,
        )
        return StepOutput(
            result={"should_notice": False, "child_running": child_running},
            observations=(
                f"confirm_close: notice already given ({noticed}x) — closing"
            ),
        )

    notice = _render_close_notice(
        child_running,
        str(ctx.get("launch_command", "") or "").strip(),
        str(ctx.get("planned_action_arg", "") or ""),
    )
    updates: dict = {"close_confirmations": noticed + 1}
    queue_injection(updates, ctx, notice)
    logger.info(
        "confirm_close: notice #%d queued, program %s — returning to the plan menu",
        noticed + 1,
        "running" if child_running else "exited",
    )
    return StepOutput(
        result={"should_notice": True, "child_running": child_running},
        observations=(
            f"confirm_close: notice #{noticed + 1} queued "
            f"(program {'running' if child_running else 'exited'})"
        ),
        context_updates=updates,
    )


# ══════════════════════════════════════════════════════════════════════
# Transient-file flush — behavioral test isolation
# ══════════════════════════════════════════════════════════════════════


# Context window at or above which the evaluation joins the tester's
# memoryful session (the original design: full transcript in KV, no
# re-prefill). Below it, the stateless bounded-tail fallback runs — a 77-turn
# session at 32k lost its verdict to a 63k-token in-session eval prompt.
# Operator (2026-08-07): "the original behavior should be the default with
# bigger context models."
_BIG_CONTEXT_MIN = 65536


async def action_probe_eval_context(step_input: StepInput) -> StepOutput:
    """Choose the evaluation mode from the serving model's real context
    window (health.nCtxSeq — read from health, never config: resident is a
    request the arch can refuse). Unknown/unreported defaults to the
    stateless fallback — the mode that cannot lose a verdict."""
    effects = step_input.effects
    n_ctx = 0
    if effects is not None and hasattr(effects, "cache_health"):
        try:
            h = await effects.cache_health()
            n_ctx = int(h.get("nCtxSeq") or 0)
        except Exception:  # noqa: BLE001 - telemetry never breaks a run
            n_ctx = 0
    big = n_ctx >= _BIG_CONTEXT_MIN
    return StepOutput(
        result={"big_context": big, "n_ctx": n_ctx},
        observations=(
            f"evaluation mode: {'in-session' if big else 'stateless tail'} "
            f"(n_ctx={n_ctx or 'unknown'})"
        ),
    )


def _transient_flush_plan(mission) -> tuple[list[str], set[str], list[str]]:
    """The shared account of what counts as transient: declared fnmatch
    patterns, the protected set (canonical modules + declared data files,
    minus exact transient declarations), and the mission's OBSERVED list.
    """
    import fnmatch as _fn  # noqa: F401 - imported for callers' use

    arch = getattr(mission, "architecture", None) if mission else None
    patterns = [
        str(p).strip()
        for p in (getattr(arch, "transient_files", None) or [])
        if str(p).strip()
    ]
    protected: set[str] = set()
    if arch is not None:
        try:
            protected.update(arch.canonical_files())
        except Exception:  # noqa: BLE001 - canonical list is best-effort
            protected.update(m.file for m in getattr(arch, "modules", []) or [])
        protected.update(
            ds.file for ds in getattr(arch, "data_shapes", []) or [] if ds.file
        )
        from agent.actions.mission_actions import transient_exact_names

        protected -= transient_exact_names(arch)
    safe_patterns = [
        p for p in patterns if not p.startswith(("/", "~")) and ".." not in p
    ]
    observed = [
        str(p)
        for p in (getattr(mission, "observed_transient_files", None) or [])
        if str(p).strip() and not str(p).startswith(("/", "~")) and ".." not in str(p)
    ]
    return safe_patterns, protected, observed


async def flush_known_transients(effects, mission) -> list[str]:
    """Delete every known transient file: declared patterns + the mission's
    observed list. The DELETION half of the deferred-flush design (operator,
    2026-08-07): runs at interact ENTRY (before the pre-session snapshot),
    on mission pause, and at mission completion — never at session end, so
    a session's runtime files stay inspectable until the next test needs a
    clean floor.
    """
    import fnmatch

    if effects is None or mission is None:
        return []
    safe_patterns, protected, observed = _transient_flush_plan(mission)
    if not safe_patterns and not observed:
        return []
    try:
        listing = await effects.list_directory(".", recursive=True)
        entries = [
            getattr(e, "path", "")
            for e in getattr(listing, "entries", []) or []
            if not getattr(e, "is_dir", False)
        ]
    except Exception:  # noqa: BLE001 - flush is best-effort, never fatal
        entries = []
    observed_set = set(observed)
    victims = sorted(
        {
            path
            for path in entries
            if path
            and path not in protected
            and (
                path in observed_set
                or any(fnmatch.fnmatch(path, pat) for pat in safe_patterns)
            )
        }
    )
    flushed: list[str] = []
    for path in victims:
        try:
            result = await effects.run_command(["rm", "-f", path], timeout=10)
            if getattr(result, "return_code", 1) == 0:
                flushed.append(path)
        except Exception as e:  # noqa: BLE001 - keep flushing the rest
            logger.warning("flush_known_transients: rm failed for %s: %s", path, e)
    if flushed:
        logger.info(
            "flush_known_transients: cleared %d stale transient(s) before "
            "session/park: %s",
            len(flushed),
            ", ".join(flushed),
        )
    return flushed


async def action_snapshot_workspace(step_input: StepInput) -> StepOutput:
    """Record the workspace file listing BEFORE any session runs.

    The other half of OPEN_TASKS §11's universal fix: with a pre-session
    snapshot, the post-session flush can compute `appeared = now − snapshot`
    and OBSERVE which files the program wrote, instead of trusting a
    declaration predicted off a lossy signature listing. Observation is
    deterministic, language-agnostic, and cannot go stale — the declaration
    missed `save.json` for an entire 8-hour run because the filename lived in
    a default argument no extractor surfaced.

    Publishes: workspace_snapshot (sorted list of file paths). An EMPTY list
    is a real snapshot (empty workspace: everything later is "appeared");
    only an absent key means "no snapshot taken" and drops the flush back to
    declaration-only behaviour.
    """
    effects = step_input.effects
    if effects is None:
        return StepOutput(result={"snapshot_files": 0}, observations="no effects")
    # PRE-SESSION FLUSH (deferred-deletion design, operator 2026-08-07):
    # session end only RECORDS observed runtime files; the deletion happens
    # here, immediately before the snapshot, so this session starts on a
    # clean floor while the previous session's files stayed inspectable in
    # the gap between them.
    pre_flushed: list[str] = []
    try:
        mission = await effects.load_mission()
    except Exception:  # noqa: BLE001 - flush is best-effort, never fatal
        mission = None
    if mission is not None:
        pre_flushed = await flush_known_transients(effects, mission)
    try:
        listing = await effects.list_directory(".", recursive=True)
        entries = sorted(
            {
                getattr(e, "path", "")
                for e in getattr(listing, "entries", []) or []
                if getattr(e, "path", "") and not getattr(e, "is_dir", False)
            }
        )
    except Exception:  # noqa: BLE001 - snapshot is best-effort, never fatal
        entries = []
    obs = f"Workspace snapshot: {len(entries)} file(s) before session"
    if pre_flushed:
        obs += f" (pre-flushed {len(pre_flushed)} stale transient(s))"
    return StepOutput(
        result={"snapshot_files": len(entries), "pre_flushed": len(pre_flushed)},
        observations=obs,
        context_updates={"workspace_snapshot": entries},
    )


async def action_flush_transient_files(step_input: StepInput) -> StepOutput:
    """RECORD transient files at session end; deletion is deferred.

    Deferred-deletion design (operator, 2026-08-07): this end-of-session
    step observes which runtime files the session created (snapshot diff),
    records them on ``mission.observed_transient_files``, and raises the
    declaration warning — but deletes NOTHING. The deletion happens at the
    next interact entry's pre-session flush (action_snapshot_workspace), on
    mission pause, and at mission completion, so the artifact's runtime
    files stay inspectable between sessions (an acceptance check may look
    for save.json after the session that wrote it) while no session ever
    STARTS with stale state.

    Programs under test write side-effect files (saves, caches, logs)
    into the shared working directory, and those files persist into the
    NEXT behavioral session. The gemma run's poison class: quitting saved
    ``game_over: true`` to state.json, main.py auto-loaded it on launch,
    and every later test saw "game has already ended" — 25 fix rounds
    against a symptom no code change could clear.

    The architecture declares which files the program creates at runtime
    (``architecture.transient_files``, names or fnmatch globs); this
    action deterministically deletes the matches after the session ends.

    Safety: only relative patterns are honored (no absolute paths, no
    ``..``), and files that are architecture canonical modules or
    declared input data (data_shapes) are never deleted even if a glob
    matches them.

    Result: flushed (int — number of files deleted)
    """
    import fnmatch

    effects = step_input.effects
    if effects is None:
        return StepOutput(result={"flushed": 0}, observations="no effects")

    try:
        mission = await effects.load_mission()
    except Exception:  # noqa: BLE001 - flush is best-effort, never fatal
        mission = None
    arch = getattr(mission, "architecture", None) if mission else None
    patterns = [
        str(p).strip()
        for p in (getattr(arch, "transient_files", None) or [])
        if str(p).strip()
    ]
    # NO EARLY RETURN ON AN EMPTY DECLARATION.
    #
    # There used to be one here, and it made the tripwire below unreachable for
    # the case that matters MOST: nothing declared at all. A WRONG declaration
    # was reported; an ABSENT one was silent. Found while planning to move this
    # declaration to project_ops, where absent gets more likely — the new step
    # can fail, the model can omit the field, and brownfield / top_phase runs
    # never reach it. Moving the declaration onto a backstop with this hole in
    # it would have been worse than leaving the declaration where it was.
    declared = bool(patterns)

    # Never delete project source or declared input data.
    protected: set[str] = set()
    if arch is not None:
        try:
            protected.update(arch.canonical_files())
        except Exception:  # noqa: BLE001 - canonical list is best-effort
            protected.update(m.file for m in getattr(arch, "modules", []) or [])
        protected.update(
            ds.file for ds in getattr(arch, "data_shapes", []) or [] if ds.file
        )
        # An EXACT transient declaration outranks the data_shapes exemption.
        # A design that lists the same path both ways is saying "author this"
        # and "this is runtime state" at once; the operator ruling (2026-08-06)
        # is that transient wins. Without this, a save file the running program
        # writes itself is protected from the flush and ships in the artifact —
        # which is how two arms shipped a save file judges then read as
        # evidence. GLOBS still lose to the exemption, so a broad `*.json`
        # cannot silently swallow declared input data.
        from agent.actions.mission_actions import transient_exact_names

        protected -= transient_exact_names(arch)

    safe_patterns = [
        p for p in patterns if not p.startswith(("/", "~")) and ".." not in p
    ]

    try:
        listing = await effects.list_directory(".", recursive=True)
        entries = [
            getattr(e, "path", "")
            for e in getattr(listing, "entries", []) or []
            if not getattr(e, "is_dir", False)
        ]
    except Exception:  # noqa: BLE001 - flush is best-effort, never fatal
        entries = []

    victims = sorted(
        {
            path
            for path in entries
            if path
            and path not in protected
            and any(fnmatch.fnmatch(path, pat) for pat in safe_patterns)
        }
    )

    # ── OBSERVATION (OPEN_TASKS §11's universal fix, built 2026-08-06) ──
    # With a pre-session snapshot on the accumulator, the transient set is
    # OBSERVED rather than predicted: `appeared = now − snapshot`, filtered
    # through the same program-generated heuristic the tripwire uses. Files
    # the session itself created, that look like runtime state, and that no
    # declaration accounts for, get flushed regardless — the declaration
    # missed `save.json` for an entire 8-hour run because the filename lived
    # in a default argument no signature extractor surfaced, and every
    # behavioural test inherited the previous test's state.
    #
    # SAFETY comes from the conjunction: appeared-this-session AND
    # state-suffixed AND not protected AND not dot/config. A file that
    # PRE-DATES the session is never observation-flushed, however
    # state-like it looks — we did not watch it appear, so deleting it is
    # not ours to decide (it still gets the tripwire below).
    #
    # An EMPTY snapshot list is a real snapshot (empty workspace); only an
    # ABSENT key means no snapshot step ran (quality_gate today, brownfield
    # resumes, older flows) and drops back to declaration-only behaviour.
    snapshot = step_input.context.get("workspace_snapshot")
    observed: list[str] = []
    if isinstance(snapshot, list):
        snap_set = {str(p) for p in snapshot}
        appeared = [p for p in entries if p not in snap_set]
        observed = [
            p
            for p in _unaccounted_state_files(appeared, protected, safe_patterns)
            if p not in set(victims)
        ]

    # DEFERRED DELETION (operator, 2026-08-07). Session end no longer
    # deletes anything — it RECORDS. Deleting here made the artifact
    # uninspectable (a save-goal acceptance check looking for save.json
    # after the session would false-fail against our own flush) and hid
    # runtime files from the operator. The deletion now happens at the
    # NEXT interact entry (pre-snapshot, in action_snapshot_workspace),
    # on mission pause, and at mission completion — the contamination
    # guarantee is preserved (no session ever STARTS with stale state)
    # while the gap between sessions keeps the files on disk.
    flushed: list[str] = []  # nothing is deleted at session end anymore
    recorded: list[str] = []
    if mission is not None and (victims or observed):
        known = set(getattr(mission, "observed_transient_files", None) or [])
        for path in observed:
            if path not in known:
                mission.observed_transient_files.append(path)
                known.add(path)
                recorded.append(path)

    # An observed runtime file is evidence of a missing/incomplete
    # DECLARATION. The observed list above is the operational, self-healing
    # account (the pre-session flush reads it); the warning below keeps the
    # audit trail and gives the declaration repair a route. The queue
    # de-duplicates while pending, re-arms if the file keeps appearing
    # after a fix attempt, and abandons after WARNING_MAX_ATTEMPTS so it
    # can never starve the functional loop.
    observed_flushed = list(observed)
    if observed_flushed and mission is not None:
        raised = 0
        for path in observed_flushed:
            raised += bool(
                mission.raise_warning(
                    kind="unaccounted_runtime_file",
                    subject=path,
                    evidence=(
                        f"The behavioural session created `{path}` (absent from "
                        f"the pre-session snapshot, present at session close), "
                        f"it matches the program-generated state heuristic, and "
                        f"no architecture.transient_files entry accounts for it "
                        f"(declared: {safe_patterns or 'nothing'}). It was "
                        f"RECORDED by observation and will be deleted at the "
                        f"next session's pre-flush, so no later session starts "
                        f"with its state — but the declaration is still wrong, "
                        f"and any session path without a snapshot stays exposed."
                    ),
                    prescribed_fix=(
                        f"Declare `{path}` in architecture.transient_files, "
                        f"citing the write site in the source (the project_ops "
                        f"declare_artifacts step persists this)."
                    ),
                    source_flow="flush_transient_files",
                )
            )
        if raised or recorded:
            try:
                await effects.save_mission(mission)
            except Exception:  # noqa: BLE001 - flush is best-effort, never fatal
                logger.debug("flush: could not persist warnings", exc_info=True)
        logger.warning(
            "flush_transient_files: OBSERVED %d undeclared runtime file(s) "
            "created by this session and recorded them for pre-session flush: "
            "%s (%d warning(s) queued for the declaration fix)",
            len(observed_flushed),
            ", ".join(observed_flushed),
            raised,
        )
    elif recorded and mission is not None:
        try:
            await effects.save_mission(mission)
        except Exception:  # noqa: BLE001 - flush is best-effort, never fatal
            logger.debug("flush: could not persist observed list", exc_info=True)

    accounted = victims + observed
    if accounted:
        observation = (
            f"Recorded {len(accounted)} transient file(s) for pre-session "
            f"flush: {', '.join(accounted)}"
        )
        if observed_flushed:
            observation += (
                f" ({len(observed_flushed)} by observation, undeclared: "
                f"{', '.join(observed_flushed)})"
            )
    elif not declared:
        observation = "no transient_files declared — nothing to flush"
    else:
        observation = f"No transient files matched {safe_patterns}"
    logger.info("flush_transient_files: %s", observation)

    # ── TRIPWIRE ────────────────────────────────────────────────────────
    # "No transient files matched" is IDENTICAL whether the workspace is clean
    # or the declaration is aimed at filenames the program never writes. That
    # silence cost a full arm on 2026-07-29: the architecture declared
    # ['save.json', '*.autosave.json'] and the built code wrote
    # `game_state.json`, so the flush no-oped 22/22 times and 91% of behavioural
    # sessions RESUMED MID-GAME. One of them resumed in a room where its test
    # move was legitimately invalid, read the correct refusal as a parser bug,
    # burned 3 goal attempts, and triggered a 586s diagnosis that concluded the
    # code was fine.
    #
    # So: say what IS sitting there that looks generated. Advisory only —
    # nothing is deleted on a heuristic.
    #
    # NOT gated on `flushed == 0`. A PARTIAL mismatch contaminates just as
    # surely: declare `state.json`, have the program also write `progress.db`,
    # and the flush reports success while the second file survives into every
    # later session. Gating on total failure would have caught 2026-07-29 and
    # missed its narrower sibling. (Found by mutation testing — the version
    # gated on `not flushed` passed every test.)
    # Files observation just flushed (and queued warnings for) are handled —
    # re-reporting them here would double-signal. What remains is the set the
    # observation deliberately does NOT touch: state-like files that PRE-DATE
    # the session (a save shipped by the batch, a leftover from a snapshotless
    # path), which still deserve the human-facing note.
    stale = [
        p
        for p in _unaccounted_state_files(entries, protected, safe_patterns)
        if p not in set(accounted)
    ]
    if stale:
        logger.warning(
            "flush_transient_files: %s and recorded %d, but these look "
            "program-generated and nothing accounts for them: %s — if the program "
            "writes any of these, they are NOT scheduled for the pre-session "
            "flush and every later session inherits their state",
            f"declared {safe_patterns}" if declared else "NOTHING was declared",
            len(accounted),
            ", ".join(stale),
        )
        await _note_flush_mismatch(effects, mission, safe_patterns, stale)

    # `flushed` stays in the result for flow/telemetry compatibility; it is
    # always 0 now that deletion is deferred to the pre-session flush.
    return StepOutput(
        result={"flushed": len(flushed), "recorded": len(accounted)},
        observations=observation,
    )


# Extensions that mean "the program wrote this while running", minus the config
# files that merely share an extension. A false positive here costs one log
# line; a false negative costs what 2026-07-29 cost.
_STATE_SUFFIXES = (
    ".json",
    ".log",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pickle",
    ".pkl",
    ".cache",
    ".tmp",
    ".bak",
    ".out",
    ".dat",
    ".state",
    ".sav",
)
_CONFIG_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "composer.json",
        "compile_commands.json",
        "pyrightconfig.json",
        "biome.json",
        "deno.json",
        ".eslintrc.json",
        "env.json",
        "cargo.json",
        "angular.json",
        "nest-cli.json",
    }
)


def _unaccounted_state_files(
    entries: list[str], protected: set[str], patterns: list[str]
) -> list[str]:
    """Files that look program-generated and that nothing accounts for.

    Deliberately NOT "every unmatched file": the scaffolding step writes
    pyproject.toml, ruff.toml, .gitignore and friends, none of which are in
    `modules`, so flagging all unmatched files would bury the signal in exactly
    the noise that trains an operator to ignore a warning."""
    import fnmatch
    import posixpath

    out = []
    for path in entries:
        if not path or path in protected:
            continue
        if any(fnmatch.fnmatch(path, pat) for pat in patterns):
            continue  # already accounted for
        base = posixpath.basename(path)
        if base in _CONFIG_NAMES or base.startswith("."):
            continue
        if base.endswith(_STATE_SUFFIXES):
            out.append(path)
    return sorted(out)


async def _note_flush_mismatch(
    effects, mission, patterns: list[str], stale: list[str]
) -> None:
    """Hand the mismatch to whoever diagnoses the next failure.

    The whole point (operator, 2026-07-29): a diagnostician that is TOLD the
    save was never flushed fixes this in one cycle, instead of investigating
    game logic for ten PTY turns. `failure_analysis` is one of the three
    categories `_filter_notes_for_file` surfaces as `relevant_notes`.

    PUSHED ONCE. That list is capped at 8 and sorted newest-first, so a note
    after each of 22 sessions would evict the real diagnoses it is meant to sit
    beside — the warning would crowd out the findings.

    TAGGED TO THE WRITERS, NOT THE ARTIFACT (fixed 2026-08-06). This note used
    to be tagged with the stale filenames themselves — and
    `_filter_notes_for_file` surfaces a note only to whoever is working on a
    tagged file. Nobody ever works on `save.json`; it is a runtime artifact,
    not a goal target. The note spent an entire 8-hour run addressed to a
    reader who cannot exist. The files someone WILL work on are the modules
    that write the artifact, so grep the canonical sources for the filename
    and tag those."""
    marker = "TEST-HARNESS CONTAMINATION"
    try:
        existing = getattr(mission, "notes", None) or []
        if any(marker in (getattr(n, "content", "") or "") for n in existing):
            return
    except Exception:  # noqa: BLE001 - a note is never worth failing a step
        pass

    # Best-effort writer lookup: which canonical module mentions the filename?
    writers: list[str] = []
    try:
        arch = getattr(mission, "architecture", None)
        candidates = list(getattr(arch, "canonical_files", lambda: [])())
        import posixpath

        basenames = {posixpath.basename(s) for s in stale}
        for f in candidates:
            if len(writers) >= 4:
                break
            try:
                fc = await effects.read_file(f)
                content = getattr(fc, "content", "") or ""
            except Exception:  # noqa: BLE001
                continue
            if any(b in content for b in basenames):
                writers.append(f)
    except Exception:  # noqa: BLE001 - tagging is best-effort
        writers = []

    writer_line = f" Written by: {', '.join(writers)}." if writers else ""
    try:
        await effects.push_note(
            content=(
                f"{marker}: the post-session flush declared {patterns}, and {stale} "
                f"are present, look program-generated, and are matched by nothing. "
                f"If the program writes one of those, it was NOT cleared and every "
                f"later test session starts from the previous session's state "
                f"(resumed saves, retained inventory, already-defeated enemies). "
                f"Before diagnosing a behavioural failure, check whether the run "
                f"under test resumed instead of starting fresh — a 'wrong' "
                f"response can be correct for the state it was actually in. The "
                f"fix is the DECLARATION, not the program: name the file the code "
                f"actually writes.{writer_line}"
            ),
            category="failure_analysis",
            # Writer files first — those are paths a goal actually targets, so
            # the note surfaces when someone touches the code that writes the
            # artifact. Stale names stay for the content-substring match.
            tags=(writers + stale)[:4],
            source_flow="flush_transient_files",
        )
        logger.info("flush_transient_files: pushed contamination note for diagnosis")
    except Exception:  # noqa: BLE001
        logger.debug("flush_transient_files: note push failed", exc_info=True)
