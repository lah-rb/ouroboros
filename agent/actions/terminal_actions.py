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

    Reads initial_commands from flow input (list of setup commands like
    venv activation, cd, env setup) and runs them in the new session.
    Publishes session_id and initial session_history.
    """
    effects = step_input.effects
    params = step_input.params

    working_dir = params.get("working_directory") or "."
    initial_commands = params.get("initial_commands", [])
    env_vars = params.get("environment_vars")

    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects interface — cannot start terminal",
            context_updates={
                "session_id": "",
                "session_history": [],
            },
        )

    # Parse initial_commands if it's a string (from template rendering)
    if isinstance(initial_commands, str):
        initial_commands = [
            cmd.strip() for cmd in initial_commands.split("\n") if cmd.strip()
        ]

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
            "setup_commands_run": len(session_history),
        },
        observations=f"Terminal session {session_id} started, "
        f"ran {len(session_history)} setup commands",
        context_updates={
            "session_id": session_id,
            "session_history": session_history,
        },
    )


# ── send_terminal_command ─────────────────────────────────────────────


async def action_send_terminal_command(step_input: StepInput) -> StepOutput:
    """Parse a command from LLM inference response and send to terminal.

    Reads inference_response from context (JSON with "command" key),
    sends to the persistent terminal session, appends result to
    session_history, and checks turn count against max_turns.
    """
    effects = step_input.effects
    session_id = step_input.context.get("session_id", "")
    session_history = step_input.context.get("session_history", [])
    inference_response = step_input.context.get("inference_response", "")
    max_turns = int(step_input.params.get("max_turns", 10))
    timeout = int(step_input.params.get("command_timeout", 30))

    if not effects or not session_id:
        return StepOutput(
            result={"command_sent": False, "max_turns_exceeded": False},
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
            result={"command_sent": False, "max_turns_exceeded": False},
            observations=f"Could not parse command from LLM response: {str(inference_response)[:200]}",
            context_updates={
                "session_id": session_id,
                "session_history": session_history,
            },
        )

    # Check turn count
    current_turn = len(session_history)
    if current_turn >= max_turns:
        return StepOutput(
            result={
                "command_sent": False,
                "max_turns_exceeded": True,
                "turn_count": current_turn,
            },
            observations=f"Max turns ({max_turns}) exceeded at turn {current_turn}",
            context_updates={
                "session_id": session_id,
                "session_history": session_history,
            },
        )

    # ── Duplicate command detection ──────────────────────────────
    # If the model sends the same command as the previous turn and
    # that turn succeeded (rc=0), the model is stuck in a loop.
    # Don't waste a terminal round — signal stuck immediately.
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
                    "max_turns_exceeded": False,
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

    return StepOutput(
        result={
            "command_sent": True,
            "return_code": result.return_code,
            "timed_out": result.timed_out,
            "turn_count": len(updated_history),
            "max_turns_exceeded": len(updated_history) >= max_turns,
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

    Closes the shell subprocess and produces a session_summary from
    the accumulated session_history.
    """
    effects = step_input.effects
    session_id = step_input.context.get("session_id", "")
    session_history = step_input.context.get("session_history", [])

    if effects and session_id:
        await effects.close_terminal(session_id)

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

    return StepOutput(
        result={
            "session_closed": True,
            "total_turns": total_turns,
            "failures": failures,
            "timeouts": timeouts,
        },
        observations=summary,
        context_updates={
            "session_summary": summary,
            "session_history": session_history,
        },
    )
