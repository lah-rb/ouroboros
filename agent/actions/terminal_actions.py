"""Terminal session actions — start, send, close persistent shell sessions.

These actions power the run_in_terminal sub-flow, enabling multi-turn
interactive terminal sessions where the LLM can run commands, observe
output, and decide whether to continue or exit.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent.actions.refinement_actions import strip_markdown_wrapper
from agent.models import StepInput, StepOutput

# ── start_terminal_session ────────────────────────────────────────────


async def action_start_terminal_session(step_input: StepInput) -> StepOutput:
    """Start a persistent terminal session and run optional setup commands.

    Also starts a memoryful inference session so subsequent LLM calls
    (plan_next_command, evaluate) can use KV-cache memory instead of
    re-serializing the full session history every turn.

    Publishes session_id, inference_session_id, and initial session_history.
    """
    effects = step_input.effects
    params = step_input.params

    working_dir = params.get("working_directory") or "."
    initial_commands = params.get("initial_commands", [])
    env_vars = params.get("environment_vars")
    session_goal = params.get("session_goal", "")
    session_context = params.get("session_context", "")

    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects interface — cannot start terminal",
            context_updates={
                "session_id": "",
                "inference_session_id": "",
                "session_history": [],
            },
        )

    # Parse initial_commands if it's a string (from template rendering)
    if isinstance(initial_commands, str):
        text = initial_commands.strip()
        if not text:
            initial_commands = []
        elif text.startswith(("{", "[")):
            # Looks like JSON — try parsing. The validation planner may
            # return a JSON object with {"commands": [...]} or a bare array.
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    initial_commands = [str(c).strip() for c in parsed if c]
                elif isinstance(parsed, dict):
                    # Extract "commands" key from validation plan JSON
                    cmds = parsed.get("commands", [])
                    if isinstance(cmds, list):
                        initial_commands = [str(c).strip() for c in cmds if c]
                    else:
                        initial_commands = []
                else:
                    initial_commands = []
            except (json.JSONDecodeError, ValueError):
                # Not valid JSON despite starting with { or [ — split on newlines
                initial_commands = [
                    cmd.strip() for cmd in text.split("\n") if cmd.strip()
                ]
        else:
            # Plain text — split on newlines (each line is a command)
            initial_commands = [cmd.strip() for cmd in text.split("\n") if cmd.strip()]

    # Parse env_vars if string
    if isinstance(env_vars, str):
        try:
            env_vars = json.loads(env_vars)
        except (json.JSONDecodeError, ValueError):
            env_vars = None

    # Start the terminal
    session_id = await effects.start_terminal(
        working_dir=working_dir,
        env=env_vars if isinstance(env_vars, dict) else None,
    )

    # Start memoryful inference session (if supported)
    inference_session_id = ""
    if hasattr(effects, "start_inference_session"):
        try:
            inference_session_id = await effects.start_inference_session(
                {"ttl_seconds": 300}
            )
        except Exception:
            # Graceful degradation — fall back to stateless inference
            inference_session_id = ""

    # Run initial setup commands
    session_history: list[dict] = []
    for cmd in initial_commands:
        if not cmd or not cmd.strip():
            continue
        result = await effects.send_to_terminal(session_id, cmd, timeout=60)
        session_history.append(
            {
                "command": result.command,
                "output": result.output,
                "return_code": result.return_code,
                "turn": result.turn,
                "timed_out": result.timed_out,
            }
        )

    return StepOutput(
        result={
            "session_started": True,
            "session_id": session_id,
            "inference_session_id": inference_session_id,
            "setup_commands_run": len(session_history),
        },
        observations=f"Terminal session {session_id} started"
        + (f" (memoryful: {inference_session_id})" if inference_session_id else "")
        + f", ran {len(session_history)} setup commands",
        context_updates={
            "session_id": session_id,
            "inference_session_id": inference_session_id,
            "session_history": session_history,
        },
    )


# ── send_terminal_command ─────────────────────────────────────────────


async def action_send_terminal_command(step_input: StepInput) -> StepOutput:
    """Parse a command from LLM inference response and send to terminal.

    Reads inference_response from context (JSON with "command" key),
    sends to the persistent terminal session, and appends result to
    session_history.
    """
    effects = step_input.effects
    session_id = step_input.context.get("session_id", "")
    session_history = step_input.context.get("session_history", [])
    inference_response = step_input.context.get("inference_response", "")
    timeout = int(step_input.params.get("command_timeout", 30))

    if not effects or not session_id:
        return StepOutput(
            result={"command_sent": False},
            observations="No effects or session_id",
            context_updates={
                "session_id": session_id,
                "session_history": session_history,
            },
        )

    # Parse the command from LLM response
    command = _parse_terminal_command(str(inference_response))

    if not command:
        return StepOutput(
            result={"command_sent": False},
            observations=f"Could not parse command from LLM response: {str(inference_response)[:200]}",
            context_updates={
                "session_id": session_id,
                "session_history": session_history,
            },
        )

    # ── Duplicate command detection ──────────────────────────────
    # If the model sends the same command as the previous turn and
    # that turn succeeded (rc=0), the model is stuck in a loop.
    # Don't waste a terminal round — signal stuck immediately.
    current_turn = len(session_history)
    if session_history:
        last_entry = session_history[-1]
        if (
            isinstance(last_entry, dict)
            and last_entry.get("command", "").strip() == command.strip()
            and last_entry.get("return_code", -1) == 0
        ):
            return StepOutput(
                result={
                    "command_sent": False,
                    "stuck_detected": True,
                    "turn_count": current_turn,
                    "duplicate_command": command,
                },
                observations=f"Stuck detected: command '{command[:60]}' is identical to previous successful turn",
                context_updates={
                    "session_id": session_id,
                    "session_history": session_history,
                },
            )

    # Send the command
    result = await effects.send_to_terminal(session_id, command, timeout=timeout)

    # Append to history
    entry = {
        "command": result.command,
        "output": result.output,
        "return_code": result.return_code,
        "turn": result.turn,
        "timed_out": result.timed_out,
    }
    updated_history = list(session_history) + [entry]

    # ── Consecutive timeout detection ────────────────────────────
    # If the last 2 commands both timed out, the subprocess is likely
    # stuck in an interactive loop (e.g., a game waiting for input()
    # that consumes our marker). Signal stuck so the session closes
    # instead of looping indefinitely.
    if result.timed_out and session_history:
        last_entry = session_history[-1]
        if isinstance(last_entry, dict) and last_entry.get("timed_out", False):
            return StepOutput(
                result={
                    "command_sent": False,
                    "stuck_detected": True,
                    "turn_count": len(updated_history),
                    "consecutive_timeouts": True,
                },
                observations=(
                    f"Stuck detected: 2 consecutive timeouts — "
                    f"subprocess likely waiting for interactive input"
                ),
                context_updates={
                    "session_id": session_id,
                    "session_history": updated_history,
                },
            )

    return StepOutput(
        result={
            "command_sent": True,
            "return_code": result.return_code,
            "timed_out": result.timed_out,
            "turn_count": len(updated_history),
        },
        observations=f"Turn {result.turn}: $ {command} → rc={result.return_code}"
        + (f" (output: {result.output[:200]})" if result.output else ""),
        context_updates={
            "session_id": session_id,
            "session_history": updated_history,
        },
    )


def _parse_terminal_command(raw: str) -> str:
    """Extract a shell command from LLM inference response.

    Tries JSON extraction first, falls back to extracting from code blocks
    or treating the whole response as a command.
    """
    raw = strip_markdown_wrapper(str(raw).strip())

    # Strip chat template delimiters that leak from local models
    # (e.g. <|im_end|>, <|im_start|>user, <|endoftext|>)
    raw = re.sub(r"<\|[^|]*\|>", "", raw)
    raw = raw.strip()

    # Try JSON: {"command": "...", "rationale": "..."}
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if json_match:
        try:
            obj = json.loads(json_match.group())
            cmd = obj.get("command", "")
            if cmd and isinstance(cmd, str):
                return cmd.strip()
        except json.JSONDecodeError:
            pass

    # Try code block extraction
    code_match = re.search(r"```(?:\w+)?\s*\n([\s\S]*?)```", raw)
    if code_match:
        cmd = code_match.group(1).strip()
        if cmd and "\n" not in cmd:  # Single-line command
            return cmd

    # Fallback: if it looks like a single command line, use it
    lines = raw.strip().splitlines()
    for line in lines:
        line = line.strip()
        # Skip obvious non-command lines
        if not line or line.startswith(("#", "//", "/*", "---")):
            continue
        if any(
            line.lower().startswith(p)
            for p in (
                "here",
                "i ",
                "the ",
                "this ",
                "let me",
                "now ",
                "next",
            )
        ):
            continue
        # If it looks like a command (starts with common command prefixes or has no spaces suggesting prose)
        if len(line) < 200:
            return line

    return ""


# ── close_terminal_session ────────────────────────────────────────────


async def action_close_terminal_session(step_input: StepInput) -> StepOutput:
    """Close a persistent terminal session and produce a summary.

    Closes both the shell subprocess and the memoryful inference session
    (if one was started). Produces a session_summary from the accumulated
    session_history.
    """
    effects = step_input.effects
    session_id = step_input.context.get("session_id", "")
    inference_session_id = step_input.context.get("inference_session_id", "")
    session_history = step_input.context.get("session_history", [])

    if effects and session_id:
        await effects.close_terminal(session_id)

    # End memoryful inference session if one exists
    if effects and inference_session_id and hasattr(effects, "end_inference_session"):
        try:
            await effects.end_inference_session(inference_session_id)
        except Exception:
            pass  # Best-effort cleanup

    # Build summary
    total_turns = len(session_history)
    failures = sum(
        1
        for entry in session_history
        if isinstance(entry, dict) and entry.get("return_code", 0) != 0
    )
    timeouts = sum(
        1
        for entry in session_history
        if isinstance(entry, dict) and entry.get("timed_out", False)
    )

    summary = (
        f"Terminal session completed: {total_turns} turns, "
        f"{failures} failures, {timeouts} timeouts"
    )

    # Build full terminal transcript — no truncation.
    # Downstream consumers (quality_gate summarize, interact result formatters)
    # receive the complete output and are responsible for summarization.
    transcript_parts = []
    for entry in session_history:
        if isinstance(entry, dict):
            cmd = entry.get("command", "")
            output = entry.get("output", "")
            rc = entry.get("return_code", "?")
            turn = entry.get("turn", "?")
            transcript_parts.append(f"[Turn {turn}] $ {cmd}")
            if output:
                transcript_parts.append(output)
            if rc != 0:
                transcript_parts.append(f"(exit code: {rc})")
    terminal_output = "\n".join(transcript_parts) if transcript_parts else ""

    terminal_status = f"{total_turns} turns, {failures} failures, {timeouts} timeouts"

    return StepOutput(
        result={
            "session_closed": True,
            "total_turns": total_turns,
            "failures": failures,
            "timeouts": timeouts,
        },
        observations=summary,
        context_updates={
            "terminal_output": terminal_output,
            "terminal_status": terminal_status,
        },
    )


# ── execute_commands_batch ─────────────────────────────────────────────


async def action_execute_commands_batch(step_input: StepInput) -> StepOutput:
    """Execute a list of commands sequentially in a terminal session.

    Used by the run_commands flow for deterministic command execution.
    Unlike send_terminal_command (which parses a single command from LLM
    inference output), this takes a list of commands directly from params.

    Params:
        commands: list of shell command strings
        stop_on_error: if true, stop on first non-zero exit code (default true)
        command_timeout: per-command timeout in seconds (default 30)

    Publishes:
        terminal_output: concatenated output from all commands
        exit_codes: list of exit codes (one per command)
        all_passed: bool — true if all commands exited 0
    """
    effects = step_input.effects
    session_id = step_input.context.get("session_id", "")
    commands = step_input.params.get("commands", [])
    stop_on_error = step_input.params.get("stop_on_error", True)
    timeout = int(step_input.params.get("command_timeout", 30))

    if not effects or not session_id:
        return StepOutput(
            result={"command_sent": False},
            observations="No effects or session_id",
            context_updates={
                "terminal_output": "",
                "exit_codes": [],
                "all_passed": False,
            },
        )

    # Normalize commands — could be a list or a single string
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
            result = await effects.send_to_terminal(
                session_id=session_id,
                command=cmd,
                timeout=timeout,
            )
            output_parts.append(f"$ {cmd}")
            output_parts.append(result.output if result.output else "(no output)")
            exit_codes.append(result.return_code)

            if result.return_code != 0:
                all_passed = False
                if stop_on_error:
                    output_parts.append(f"(exit code: {result.return_code} — stopping)")
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
            "session_id": session_id,
            "terminal_output": terminal_output,
            "exit_codes": exit_codes,
            "all_passed": all_passed,
            "command_count": len(exit_codes),
        },
    )
