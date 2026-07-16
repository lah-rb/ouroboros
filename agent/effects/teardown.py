"""Best-effort effects teardown shared by every mission runner.

Releases LLMVP sessions + disconnects MCP (the terminal-server tree) inside
the loop before it closes, so neither orphans on exit. One canonical copy:
this loop was previously duplicated across the CLI and all five adapter
runners, and the newest copy drifted back to `except Exception` — losing the
CancelledError swallow and reintroducing the teardown crash it existed to
prevent (a cancellation landing during drain is a BaseException, escapes
`except Exception`, kills asyncio.run, and costs the run its results).
"""

import asyncio


async def drain_effects(effects) -> None:
    """Release open inference sessions + MCP connections, best-effort.

    Never raises: teardown must not mask the mission's own outcome, and a
    CancelledError landing mid-drain (ambient cancel scope at loop close)
    must not kill the runner.
    """
    for teardown in ("end_open_inference_sessions", "mcp_disconnect_all"):
        fn = getattr(effects, teardown, None)
        if fn is None:
            continue
        try:
            await fn()
        except (Exception, asyncio.CancelledError):
            pass
