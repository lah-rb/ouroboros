"""LocalEffects teardown sweeps — the paths whose own docstrings name the cost.

`LocalEffects` is the production effects object (the mock double sits at 84%
coverage; the real one was at 29%). The uncovered parts are not obscure —
they are the mission-teardown sweeps, and each carries its consequence in its
docstring:

* `end_open_inference_sessions` — "a wall-clock park or a hard cancel can
  leave a session pinned on the single-instance LLMVP pool; the next mission's
  first session open then waits backend_timeout (180s) and raises 'busy'".
  Cross-mission damage from one stranded id.

* `mcp_disconnect_all` — "stops PTY/server processes orphaning on the park/exit
  path". A silent no-op here leaks a terminal-server process tree per parked
  mission, cumulatively.

Both are best-effort by design (`except Exception: pass`), which is exactly
why they need tests: a sweep that silently does nothing looks identical to a
sweep that worked.
"""

from __future__ import annotations

import pytest

from agent.effects.local import LocalEffects


class _FakeInference:
    """Stands in for the LLMVP client. Records ends; can be made to fail."""

    def __init__(self, fail_on=()):
        self.ended: list[str] = []
        self.fail_on = set(fail_on)

    async def end_session(self, session_id: str) -> bool:
        self.ended.append(session_id)
        if session_id in self.fail_on:
            raise RuntimeError("server refused the close")
        return True


def _effects(tmp_path, inference):
    fx = LocalEffects(working_directory=str(tmp_path))
    fx._get_inference = lambda: inference  # type: ignore[method-assign]
    return fx


def _open(fx, *sids):
    for sid in sids:
        fx._session_started_at[sid] = 0.0


# ── inference-session drain ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_closes_every_open_session(tmp_path):
    inf = _FakeInference()
    fx = _effects(tmp_path, inf)
    _open(fx, "s1", "s2", "s3")

    assert await fx.end_open_inference_sessions() == 3
    assert sorted(inf.ended) == ["s1", "s2", "s3"]
    assert fx._session_started_at == {}, "drained sessions must not stay tracked"


@pytest.mark.asyncio
async def test_drain_is_a_noop_when_nothing_is_open(tmp_path):
    inf = _FakeInference()
    fx = _effects(tmp_path, inf)
    assert await fx.end_open_inference_sessions() == 0
    assert inf.ended == []


@pytest.mark.asyncio
async def test_one_broken_close_does_not_strand_the_rest(tmp_path):
    """A close that raises must not abort the sweep — the remaining sessions
    would stay pinned and cost the NEXT mission its first 180s."""
    inf = _FakeInference(fail_on={"s2"})
    fx = _effects(tmp_path, inf)
    _open(fx, "s1", "s2", "s3")

    closed = await fx.end_open_inference_sessions()

    assert sorted(inf.ended) == ["s1", "s2", "s3"], "sweep stopped at the failure"
    assert closed == 2  # s2 did not close cleanly
    assert fx._session_started_at == {}, (
        "a session whose close BROKE must still be untracked — otherwise the "
        "drain retries it forever and never converges"
    )


@pytest.mark.asyncio
async def test_drain_leaves_nothing_tracked_even_when_every_close_fails(tmp_path):
    inf = _FakeInference(fail_on={"s1", "s2"})
    fx = _effects(tmp_path, inf)
    _open(fx, "s1", "s2")

    assert await fx.end_open_inference_sessions() == 0
    assert fx._session_started_at == {}


# ── MCP disconnect sweep ──────────────────────────────────────────────────


class _FakeMcpClient:
    def __init__(self, fail=False):
        self.disconnect_all_calls = 0
        self.fail = fail

    async def disconnect_all(self):
        self.disconnect_all_calls += 1
        if self.fail:
            raise RuntimeError("transport already dead")


@pytest.mark.asyncio
async def test_disconnect_all_reaches_the_client(tmp_path):
    """The sweep that kills the terminal-server subprocess tree. If it never
    calls through, every parked mission leaks a process tree."""
    fx = LocalEffects(working_directory=str(tmp_path))
    client = _FakeMcpClient()
    fx._mcp_client = client
    fx._mcp_connections = {"c1": object()}

    await fx.mcp_disconnect_all()

    assert client.disconnect_all_calls == 1
    assert fx._mcp_connections == {}


@pytest.mark.asyncio
async def test_disconnect_all_still_clears_bookkeeping_when_the_client_raises(
    tmp_path,
):
    """Best-effort means the local record must not survive a failed sweep —
    stale connection ids would make a later call think it still owns them."""
    fx = LocalEffects(working_directory=str(tmp_path))
    client = _FakeMcpClient(fail=True)
    fx._mcp_client = client
    fx._mcp_connections = {"c1": object()}

    await fx.mcp_disconnect_all()  # must not raise

    assert client.disconnect_all_calls == 1
    assert fx._mcp_connections == {}


@pytest.mark.asyncio
async def test_disconnect_all_is_safe_with_nothing_open(tmp_path):
    """Called from every teardown path, including ones that never used MCP."""
    fx = LocalEffects(working_directory=str(tmp_path))
    await fx.mcp_disconnect_all()  # no _mcp_client attribute at all
