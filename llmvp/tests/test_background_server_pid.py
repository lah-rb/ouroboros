"""Background-server PID handling — a pid is not an identity.

Two hazards lived in `stop_background_server`, both in uncovered code:

* PID REUSE. The stop path read an int from PID_FILE, checked only
  `psutil.Process(pid).is_running()` — true for ANY live process holding that
  number — then SIGTERMed it and, ten seconds later, SIGKILLed it. After a
  reboot or pid wraparound that is a SIGKILL aimed at an unrelated program.
  Nothing verified the target was actually our server.

* UNSTOPPABLE ORPHAN. A blanket `finally` removed PID_FILE unconditionally,
  including on the `except Exception: return False` path. A server that failed
  to die was then a running process holding the GPU with no PID file, which
  the CLI reports as "no background server is running" — forever.

Both are now guarded by `_owning_server_process`, which signals only a process
whose cmdline identifies it as ours.
"""

from __future__ import annotations


import pytest

import api.main as main


class _FakeProc:
    """psutil.Process stand-in with a controllable cmdline and liveness."""

    def __init__(self, cmdline, running=True):
        self._cmdline = cmdline
        self._running = running

    def is_running(self):
        return self._running

    def cmdline(self):
        return self._cmdline


@pytest.fixture
def pid_file(tmp_path, monkeypatch):
    f = tmp_path / "llmvp.pid"
    monkeypatch.setattr(main, "PID_FILE", str(f))
    return f


# ── identity ──────────────────────────────────────────────────────────────


def test_owning_process_accepts_our_server(monkeypatch):
    monkeypatch.setattr(
        main.psutil, "Process", lambda pid: _FakeProc(["python", "api/main.py"])
    )
    assert main._owning_server_process(4321) is not None


def test_owning_process_rejects_a_recycled_pid(monkeypatch):
    """THE hazard: a live process that is NOT ours must never be signalled."""
    monkeypatch.setattr(
        main.psutil, "Process", lambda pid: _FakeProc(["/usr/bin/ssh", "prod-box"])
    )
    assert main._owning_server_process(4321) is None


def test_owning_process_rejects_a_dead_pid(monkeypatch):
    monkeypatch.setattr(
        main.psutil,
        "Process",
        lambda pid: _FakeProc(["python", "api/main.py"], running=False),
    )
    assert main._owning_server_process(4321) is None


def test_owning_process_rejects_an_unreadable_process(monkeypatch):
    def _boom(pid):
        raise main.psutil.AccessDenied(pid)

    monkeypatch.setattr(main.psutil, "Process", _boom)
    assert main._owning_server_process(4321) is None


# ── stop ──────────────────────────────────────────────────────────────────


def test_stop_never_signals_a_recycled_pid(pid_file, monkeypatch):
    """A recycled pid must be treated as stale: cleaned up, never killed."""
    pid_file.write_text("4321")
    monkeypatch.setattr(
        main.psutil, "Process", lambda pid: _FakeProc(["/usr/bin/ssh", "prod-box"])
    )
    kills = []
    monkeypatch.setattr(main.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    assert main.stop_background_server() is True
    assert kills == [], "signalled a process that was not our server"
    assert not pid_file.exists()


def test_stop_keeps_the_pid_file_when_the_server_survives(pid_file, monkeypatch):
    """The orphan guard: if the process is still alive after SIGKILL, the file
    must SURVIVE so the CLI can still reach it."""
    pid_file.write_text("4321")
    monkeypatch.setattr(
        main.psutil, "Process", lambda pid: _FakeProc(["python", "api/main.py"])
    )
    monkeypatch.setattr(main.os, "kill", lambda pid, sig: None)  # signals do nothing
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main.time, "time", _fake_clock())

    assert main.stop_background_server() is False
    assert pid_file.exists(), (
        "dropped the PID file for a server that is STILL RUNNING — it is now "
        "unreachable and the CLI will report 'not running' forever"
    )


def test_stop_removes_the_pid_file_once_the_server_is_gone(pid_file, monkeypatch):
    pid_file.write_text("4321")
    state = {"alive": True}

    def _proc(pid):
        return _FakeProc(["python", "api/main.py"], running=state["alive"])

    def _kill(pid, sig):
        state["alive"] = False  # SIGTERM lands

    monkeypatch.setattr(main.psutil, "Process", _proc)
    monkeypatch.setattr(main.os, "kill", _kill)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    assert main.stop_background_server() is True
    assert not pid_file.exists()


def test_stop_without_a_pid_file_is_a_clean_no_op(pid_file):
    assert not pid_file.exists()
    assert main.stop_background_server() is False


def test_stop_cleans_up_an_unreadable_pid_file(pid_file):
    pid_file.write_text("not-a-number")
    assert main.stop_background_server() is True
    assert not pid_file.exists()


# ── start ─────────────────────────────────────────────────────────────────


def test_start_refuses_when_a_live_server_owns_the_pid(pid_file, monkeypatch):
    pid_file.write_text("4321")
    monkeypatch.setattr(
        main.psutil, "Process", lambda pid: _FakeProc(["python", "api/main.py"])
    )
    with pytest.raises(RuntimeError, match="already running"):
        main.start_background_server()


def test_start_clears_a_stale_pid_file_instead_of_refusing(pid_file, monkeypatch):
    """A crashed server leaves the file behind. Refusing on file existence
    alone made hand-deleting it the only recovery."""
    pid_file.write_text("4321")
    monkeypatch.setattr(
        main.psutil, "Process", lambda pid: _FakeProc(["/usr/bin/ssh", "prod"])
    )
    # Stop before the heavy subprocess launch — the staleness handling is the
    # behavior under test, not the spawn.
    monkeypatch.setattr(
        main, "get_config", lambda: (_ for _ in ()).throw(RuntimeError("stop here"))
    )
    with pytest.raises(RuntimeError, match="stop here"):
        main.start_background_server()
    assert not pid_file.exists(), "stale PID file was not cleared"


def _fake_clock():
    """Monotonic-ish clock that runs past any timeout on repeated calls."""
    ticks = iter([0.0] + [i * 5.0 for i in range(1, 200)])

    def _t():
        try:
            return next(ticks)
        except StopIteration:
            return 1e6

    return _t
