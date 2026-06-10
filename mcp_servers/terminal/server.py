"""MCP Terminal Server — interactive PTY sessions for LLM agents.

Exposes four tools via MCP (Model Context Protocol):
  - create_session: spawn a shell in a PTY
  - send_input: send text to a running session, await settled output
  - read_output: poll for output from a session
  - close_session: terminate a session

Run as a subprocess, communicates over stdio:
    python -m mcp_servers.terminal.server
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from mcp_servers.terminal.pty_session import PTYSessionManager

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "ouroboros-terminal",
    instructions=(
        "Interactive terminal server. Create PTY sessions, send input to "
        "interactive programs, read their output. Supports shells, REPLs, "
        "games, CLI tools — anything that reads from stdin."
    ),
)

manager = PTYSessionManager()


@mcp.tool(
    name="create_session",
    description=(
        "Create a new interactive terminal session. Spawns a shell (or "
        "specific command) in a pseudo-terminal. Returns a session_id "
        "for subsequent send_input/read_output calls."
    ),
)
async def create_session(
    working_directory: str = ".",
    command: str | None = None,
    env: dict[str, str] | None = None,
    expected_prompt: str = "",
) -> dict:
    """Create a new PTY session."""
    session_id = await manager.create_session(
        command=command,
        working_directory=working_directory,
        env=env,
        expected_prompt=expected_prompt,
    )
    return {
        "session_id": session_id,
        "status": "created",
    }


def _build_response(result) -> dict:
    """Build a response dict from an InteractionResult."""
    response = {
        "output": result.output,
        "status": result.status,
    }
    if result.exit_code is not None:
        response["exit_code"] = result.exit_code

    # Forward foreground process info and prompt detection
    if result.status != "error":
        response["interactive_child"] = result.interactive_child_running
        response["prompt_detected"] = result.prompt_detected
        # 6c2 round — flow-profile metadata. Consumers use this to
        # decide whether an ambiguous annotation ("may be
        # processing or waiting") is warranted. settled_cleanly is
        # the strict signal: bytes were observed AND the stream
        # went idle for the settle window. The coarse status
        # vocabulary is retained for backward compat; the
        # per-metric fields give finer resolution for distinguishing
        # "responded without a prompt tag" from "truly silent".
        response["total_bytes_received"] = getattr(result, "total_bytes_received", 0)
        response["peak_rate_bps"] = getattr(result, "peak_rate_bps", 0.0)
        response["idle_duration_s"] = getattr(result, "idle_duration_s", 0.0)
        response["settled_cleanly"] = getattr(result, "settled_cleanly", False)
        # pyte cursor-resolved screen state ("what the terminal shows now").
        # `output` is the literal per-turn byte delta; consumers wanting clean
        # repaint-resolved state can prefer this.
        response["screen_text"] = getattr(result, "screen_text", "")

    return response


@mcp.tool(
    name="send_input",
    description=(
        "Send input text to a running terminal session. The text is "
        "written directly to the terminal — it goes to whatever process "
        "is currently reading (shell, game, REPL, etc.).\n\n"
        "By default, waits for the output to 'settle' (no new output "
        "for settle_ms) before returning. Set await_response=false to "
        "send without waiting.\n\n"
        "Returns the output produced after sending, plus a status:\n"
        "  - 'settled': output stopped flowing (program likely waiting for input)\n"
        "  - 'timeout': hard timeout reached (program still producing output)\n"
        "  - 'process_exited': the program has terminated\n"
        "  - 'no_new_output': buffer was empty and no new output arrived\n"
        "  - 'error': session not found or write failed\n\n"
        "Also returns prompt_detected (bool): true when output ends with "
        "a recognized prompt character, indicating the program responded "
        "and is ready for the next command."
    ),
)
async def send_input(
    session_id: str,
    text: str,
    await_response: bool = True,
    settle_ms: int = 500,
    timeout_ms: int = 15_000,
) -> dict:
    """Send input to a PTY session."""
    result = await manager.send_input(
        session_id=session_id,
        text=text,
        await_response=await_response,
        settle_ms=settle_ms,
        timeout_ms=timeout_ms,
    )
    return _build_response(result)


@mcp.tool(
    name="read_output",
    description=(
        "Read available output from a terminal session without sending "
        "input. Use when you need to poll for output after a previous "
        "send_input with await_response=false, or when waiting for a "
        "slow program to produce more output.\n\n"
        "Same settle/timeout behavior as send_input."
    ),
)
async def read_output(
    session_id: str,
    settle_ms: int = 500,
    timeout_ms: int = 15_000,
) -> dict:
    """Read output from a PTY session."""
    result = await manager.read_output(
        session_id=session_id,
        settle_ms=settle_ms,
        timeout_ms=timeout_ms,
    )
    return _build_response(result)


@mcp.tool(
    name="close_session",
    description="Close a terminal session. Kills the process and cleans up.",
)
async def close_session(session_id: str) -> dict:
    """Close a PTY session."""
    return await manager.close_session(session_id)


def main() -> None:
    """Entry point for running as a subprocess."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
