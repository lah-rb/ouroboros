"""MCP connection cleanup — the subprocess that outlives a failed connect.

`MCPClient.connect` manually enters TWO async context managers (the stdio
transport, which spawns the server subprocess, and the ClientSession) because
"the connection outlives any single async with block". That hand-rolled
lifecycle is the whole risk: if anything after the transport's `__aenter__`
raises — the session enter, or the MCP handshake — the subprocess is already
running, and only `_cleanup_connection` can reap it.

`_cleanup_connection` exits both with bare `except: pass`, so a swallow there
is invisible. One orphaned MCP server per failed connect, cumulative over a
long mission — the same shape as the leaks that motivated
`mcp_disconnect_all`.

These drive the real cleanup logic with recording context-manager doubles: no
subprocess, but the exit calls (and their ORDER) are observable.
"""

from __future__ import annotations

import pytest

from agent.mcp_client import MCPClient, MCPConnection


class _RecordingCM:
    """Records __aexit__; can be made to raise on the way out."""

    def __init__(self, name, exits, fail=False):
        self.name = name
        self.exits = exits
        self.fail = fail

    async def __aexit__(self, *exc):
        self.exits.append(self.name)
        if self.fail:
            raise RuntimeError(f"{self.name} refused to close")
        return False


def _conn(exits, session_fail=False, transport_fail=False) -> MCPConnection:
    conn = MCPConnection(
        connection_id="c1", server_name="terminal", server_command=["x"]
    )
    conn.session = _RecordingCM("session", exits, fail=session_fail)
    conn._cm_stack = _RecordingCM("transport", exits, fail=transport_fail)
    return conn


@pytest.mark.asyncio
async def test_cleanup_closes_session_then_transport():
    """Order matters: the session rides on the transport's streams, so the
    transport (which owns the subprocess) must be torn down last."""
    exits: list[str] = []
    conn = _conn(exits)

    await MCPClient()._cleanup_connection(conn)

    assert exits == ["session", "transport"]
    assert conn.session is None
    assert conn._cm_stack is None


@pytest.mark.asyncio
async def test_a_failing_session_close_still_reaps_the_transport():
    """THE leak: the transport owns the SUBPROCESS. A session close that
    raises must not skip it, or the MCP server outlives the connection."""
    exits: list[str] = []
    conn = _conn(exits, session_fail=True)

    await MCPClient()._cleanup_connection(conn)

    assert "transport" in exits, (
        "a failed session close skipped the transport teardown — the MCP "
        "server subprocess is now orphaned"
    )
    assert conn._cm_stack is None


@pytest.mark.asyncio
async def test_a_failing_transport_close_does_not_propagate():
    """Teardown is best-effort: a transport already dead (the common case
    after a crash) must not turn cleanup into an exception."""
    exits: list[str] = []
    conn = _conn(exits, transport_fail=True)

    await MCPClient()._cleanup_connection(conn)  # must not raise

    assert exits == ["session", "transport"]
    assert conn.session is None and conn._cm_stack is None


@pytest.mark.asyncio
async def test_cleanup_is_idempotent():
    """Called from both the connect-failure path and disconnect, so it must
    tolerate running twice without re-exiting a closed manager."""
    exits: list[str] = []
    conn = _conn(exits)
    client = MCPClient()

    await client._cleanup_connection(conn)
    await client._cleanup_connection(conn)

    assert exits == ["session", "transport"], "second cleanup re-exited the managers"


@pytest.mark.asyncio
async def test_cleanup_of_a_never_connected_conn_is_a_noop():
    """A connect that failed before the transport even entered leaves both
    slots empty."""
    conn = MCPConnection(connection_id="c1", server_name="t", server_command=["x"])
    await MCPClient()._cleanup_connection(conn)  # must not raise


# ── disconnect_all ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_all_continues_past_a_broken_connection(monkeypatch):
    """One wedged connection must not strand the others.

    NOTE the fixture has to make `disconnect` ITSELF raise. A connection whose
    session merely fails to close is not enough: _cleanup_connection swallows
    that, so disconnect returns normally and disconnect_all's guard is never
    reached. The first version of this test made that mistake and could not
    fail (verified by mutation 2026-07-25).
    """
    client = MCPClient()
    reaped: list[str] = []
    for cid in ("c1", "c2", "c3"):
        client._connections[cid] = MCPConnection(
            connection_id=cid, server_name="t", server_command=["x"]
        )

    real_disconnect = client.disconnect

    async def _disconnect(cid):
        if cid == "c2":
            raise RuntimeError("transport wedged")
        reaped.append(cid)
        await real_disconnect(cid)

    monkeypatch.setattr(client, "disconnect", _disconnect)

    await client.disconnect_all()  # must not raise

    assert reaped == ["c1", "c3"], "sweep stopped at the wedged connection"
