"""S3 of τ piece 4: ToolBridge (HTTP + the real ./tau CLI) + MissionWorker
reply plumbing. Offline — a stub EpisodeHandle, no server, no missions run
(the mission run itself is monkeypatched)."""

from __future__ import annotations

import subprocess
import sys



# ── ToolBridge via the real workspace CLI ─────────────────────────────


class StubHandle:
    RESPOND = "respond"

    def __init__(self, max_steps=60):
        self.done = False
        self.steps = 0
        self._max_steps = max_steps
        self.calls = []

    def call_tool(self, name, **kwargs):
        self.calls.append((name, kwargs))
        self.steps += 1
        if name == "boom":
            raise ValueError("tool blew up")
        return f"ok:{name}:{sorted(kwargs.items())}"


def _write_cli(tmp_path, bridge_url):
    from adapters.tau.bridge import TAU_CLI_TEMPLATE

    cli = tmp_path / "tau"
    cli.write_text(TAU_CLI_TEMPLATE.format(bridge_url=bridge_url))
    cli.chmod(0o755)
    return str(cli)


def _run_cli(cli, *args):
    return subprocess.run(
        [sys.executable, cli, *args], capture_output=True, text=True, timeout=30
    )


def test_bridge_roundtrip_via_cli_json(tmp_path):
    from adapters.tau.bridge import ToolBridge

    handle = StubHandle()
    bridge = ToolBridge(handle)
    try:
        cli = _write_cli(tmp_path, bridge.start())
        out = _run_cli(cli, "get_order_details", "--json", '{"order_id": "#W1"}')
        assert out.returncode == 0
        assert "ok:get_order_details" in out.stdout
        assert handle.calls == [("get_order_details", {"order_id": "#W1"})]
    finally:
        bridge.shutdown()


def test_bridge_roundtrip_via_cli_kv_flags(tmp_path):
    from adapters.tau.bridge import ToolBridge

    handle = StubHandle()
    bridge = ToolBridge(handle)
    try:
        cli = _write_cli(tmp_path, bridge.start())
        out = _run_cli(cli, "find_user", "--email", "a@b.com", "--limit", "3")
        assert out.returncode == 0
        name, kwargs = handle.calls[0]
        assert name == "find_user"
        assert kwargs == {"email": "a@b.com", "limit": 3}  # 3 parsed as int
    finally:
        bridge.shutdown()


def test_bridge_refuses_respond(tmp_path):
    from adapters.tau.bridge import ToolBridge

    handle = StubHandle()
    bridge = ToolBridge(handle)
    try:
        cli = _write_cli(tmp_path, bridge.start())
        out = _run_cli(cli, "respond", "--content", "hello customer")
        assert out.returncode == 1
        assert "reply.txt" in out.stderr
        assert handle.calls == []  # never reached the env
    finally:
        bridge.shutdown()


def test_bridge_refuses_when_done_and_near_step_cap(tmp_path):
    from adapters.tau.bridge import ToolBridge

    handle = StubHandle(max_steps=10)
    bridge = ToolBridge(handle, reserve_steps=6)
    try:
        cli = _write_cli(tmp_path, bridge.start())
        handle.steps = 5  # 10 - 6 = 4 threshold → 5 >= 4 → refuse
        out = _run_cli(cli, "get_order_details", "--json", "{}")
        assert out.returncode == 1 and "budget" in out.stderr

        handle.steps = 0
        handle.done = True
        out = _run_cli(cli, "get_order_details", "--json", "{}")
        assert out.returncode == 1 and "ended" in out.stderr
    finally:
        bridge.shutdown()


def test_bridge_tool_error_is_returned_not_raised(tmp_path):
    from adapters.tau.bridge import ToolBridge

    handle = StubHandle()
    bridge = ToolBridge(handle)
    try:
        cli = _write_cli(tmp_path, bridge.start())
        out = _run_cli(cli, "boom", "--json", "{}")
        # A tool exception is a 200 observation ("Error: ...") the agent reads.
        assert out.returncode == 0 and "Error: tool blew up" in out.stdout
    finally:
        bridge.shutdown()


# ── MissionWorker reply plumbing (mission run monkeypatched) ──────────


def _mk_worker(monkeypatch, run_effect):
    """Build a MissionWorker without a real venv/mission; run_effect(worker,
    objective) simulates one mission run (writes reply.txt or not)."""
    import adapters.tau.worker as w

    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: None)  # skip venv

    worker = w.MissionWorker.__new__(w.MissionWorker)
    import tempfile

    worker.workspace = tempfile.mkdtemp(prefix="tau-test-")
    worker.llmvp_endpoint = "http://x/graphql"
    worker.max_cycles = 1
    worker.wall_clock_s = 5
    worker.mission_runs = 0

    async def fake_run_mission(objective):
        worker.mission_runs += 1
        return run_effect(worker, objective)

    worker._run_mission = fake_run_mission
    return worker


def test_worker_returns_written_reply(monkeypatch):
    def effect(worker, objective):
        worker._write("reply.txt", "  Your order #W1 is on its way.  ")
        return True

    worker = _mk_worker(monkeypatch, effect)
    import asyncio

    rep = asyncio.run(worker.execute("look up #W1", [{"role": "user", "text": "hi"}]))
    assert rep.ok and rep.reply == "Your order #W1 is on its way."
    assert worker.mission_runs == 1
    worker.cleanup()


def test_worker_retries_once_then_holding_reply(monkeypatch):
    def effect(worker, objective):
        return True  # mission "succeeds" but never writes reply.txt

    worker = _mk_worker(monkeypatch, effect)
    import asyncio

    rep = asyncio.run(worker.execute("do a thing", [{"role": "user", "text": "hi"}]))
    assert not rep.ok and "patience" in rep.reply
    assert worker.mission_runs == 2  # initial + one retry
    assert "technical fault" in rep.notes
    worker.cleanup()


def test_worker_rotates_stale_reply(monkeypatch):
    # A leftover reply.txt from a prior turn must not be re-sent when the
    # new mission writes nothing.
    calls = {"n": 0}

    def effect(worker, objective):
        calls["n"] += 1
        if calls["n"] == 1:
            worker._write("reply.txt", "first-turn reply")
        # subsequent runs write nothing
        return True

    worker = _mk_worker(monkeypatch, effect)
    import asyncio

    r1 = asyncio.run(worker.execute("turn1", [{"role": "user", "text": "hi"}]))
    assert r1.reply == "first-turn reply"
    r2 = asyncio.run(worker.execute("turn2", [{"role": "user", "text": "more"}]))
    assert not r2.ok and "patience" in r2.reply  # stale reply was rotated out
    worker.cleanup()


def test_worker_strips_agent_prefix(monkeypatch):
    def effect(worker, objective):
        worker._write("reply.txt", 'Agent: "How can I help?"')
        return True

    worker = _mk_worker(monkeypatch, effect)
    import asyncio

    rep = asyncio.run(worker.execute("greet", [{"role": "user", "text": "hi"}]))
    assert rep.reply == "How can I help?"
    worker.cleanup()
