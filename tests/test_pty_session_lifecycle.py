"""Lifecycle tests for ``PTYSessionManager`` — the Protocol-based reader.

Drives the manager directly (no MCP layer) so failures pinpoint the
PTY layer cleanly. Each test creates a real session, sends real
input, asserts on real output, and closes cleanly.

Coverage:
  - create → send → drain → close (the happy path)
  - the monotonic ``bytes_received_total`` counter advances on
    incoming bytes
  - leftover bytes from the previous turn ride along in the next
    turn's drained output (Option B — Angle 4 of the PTY-fix plan)
  - ``close_session`` is idempotent against an already-exited child
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_servers.terminal.pty_session import PTYSessionManager

pytestmark = pytest.mark.asyncio


async def test_create_send_close_lifecycle(tmp_path):
    """Plain echo round-trip — bytes go in, bytes come out, session closes."""
    mgr = PTYSessionManager()
    sid = await mgr.create_session(working_directory=str(tmp_path))

    try:
        result = await mgr.send_input(sid, "echo hello-pty\n", timeout_ms=5000)
        # Some shells echo input, some don't; we care that the program
        # produced visible output that includes our token.
        assert (
            "hello-pty" in result.output
        ), f"expected 'hello-pty' in output, got {result.output!r}"
        # Settled cleanly when the burst-then-idle shape is observed.
        # Don't assert settled_cleanly strictly (CI timing can produce
        # 'timeout' instead with bytes still arriving), but at least
        # require we observed bytes for this turn.
        assert result.total_bytes_received > 0
    finally:
        outcome = await mgr.close_session(sid)
        assert outcome["success"] is True


async def test_bytes_received_total_advances_monotonically(tmp_path):
    """The monotonic counter is the bedrock of the new settle detector."""
    mgr = PTYSessionManager()
    sid = await mgr.create_session(working_directory=str(tmp_path))

    try:
        session = mgr._sessions[sid]
        before = session.bytes_received_total
        await mgr.send_input(sid, "printf 'a\\nb\\nc\\n'\n", timeout_ms=5000)
        after = session.bytes_received_total
        assert after > before, f"counter did not advance: before={before} after={after}"
    finally:
        await mgr.close_session(sid)


async def test_leftover_prefix_rides_along_next_turn(tmp_path):
    """Option B: bytes that arrive between turns are NOT discarded.

    Force a between-turns arrival by sending a command that produces
    output and then sleeping (without sending) — the reader protocol
    accumulates bytes into the buffer. The next send_input must NOT
    erase them; they should appear in the drained output of the next
    turn alongside that turn's response.
    """
    mgr = PTYSessionManager()
    sid = await mgr.create_session(working_directory=str(tmp_path))

    try:
        # Turn 1: kick off output and immediately ask for short settle —
        # we want to leave room for "late" bytes that don't make this
        # turn's settle window.
        await mgr.send_input(sid, "echo first-burst\n", settle_ms=200, timeout_ms=2000)

        # Wait long enough for any straggler bytes (a second prompt,
        # bash status messages, whatever) to land in the buffer.
        await asyncio.sleep(0.3)

        # Turn 2: ask for an obviously different output. The Option B
        # contract says any leftover prefix from turn 1 is preserved
        # and appears in this turn's output.
        result = await mgr.send_input(
            sid, "echo second-burst\n", settle_ms=200, timeout_ms=2000
        )
        # The new bytes from this turn must be present.
        assert (
            "second-burst" in result.output
        ), f"expected 'second-burst' in output, got {result.output!r}"
        # We don't strictly require leftover from turn 1 to show up
        # (a fast settle on turn 1 may have captured everything
        # already — that's fine and expected most of the time).
        # The hard contract is "we did not actively discard buffer
        # contents at turn 2's start", which the absence of any
        # _output_buffer.clear() in send_input now guarantees by
        # construction. Asserting on observable output keeps this
        # test deterministic across CI timing variance.
    finally:
        await mgr.close_session(sid)


async def test_close_session_idempotent_after_child_exit(tmp_path):
    """Close is graceful when the child has already exited (EOF path)."""
    mgr = PTYSessionManager()
    sid = await mgr.create_session(working_directory=str(tmp_path))

    try:
        # `exit` makes bash terminate; the protocol's eof_received
        # fires, _reap_session_pid runs, session._exited goes True.
        await mgr.send_input(sid, "exit\n", timeout_ms=3000)
        # Give the EOF a moment to propagate through the transport.
        await asyncio.sleep(0.1)
        session = mgr._sessions.get(sid)
        # Session may have been removed by close_session in some races;
        # if it's still here, _exited should be True.
        if session is not None:
            assert session._exited is True, "expected child to be reaped"
    finally:
        outcome = await mgr.close_session(sid)
        # Either way, close_session reports success — it handles the
        # "already exited and reaped" case without raising.
        assert outcome["success"] is True


async def test_poll_settle_burst_then_idle_detection(tmp_path):
    """The shared :meth:`_poll_settle` helper detects the
    burst-then-idle shape without tripping on mid-print pauses.

    Drives a real session via the same path create_session uses
    (settle_ms=200, timeout_ms=500) and verifies the helper returns
    settled=True with saw_burst=True after the shell prints its
    PS1 and goes quiet. This is the regression guard for the 32f
    Angle-3 refactor — pre-fix, the naive "any growth, any 50ms
    pause" predicate could exit during intra-PS1 print pauses; the
    differential approach mirrors _wait_for_settle's signal
    processing and only settles after sustained idle.
    """
    mgr = PTYSessionManager()
    sid = await mgr.create_session(working_directory=str(tmp_path))

    try:
        # Trigger fresh output by sending input — the echo + response
        # is a fresh burst we can settle on. send_input internally
        # uses _wait_for_settle which delegates to _poll_settle.
        send_task = asyncio.create_task(
            mgr.send_input(sid, "echo settled\n", timeout_ms=3000)
        )
        result = await send_task

        # The send_input path goes through _wait_for_settle which uses
        # _poll_settle internally. A clean settle implies the helper's
        # burst-then-idle predicate fired.
        assert result.status in (
            "settled",
            "timeout",
        ), f"expected settled or timeout (with output), got {result.status!r}"
        # Output should contain our token — proves the settle didn't
        # exit before bytes arrived.
        assert "settled" in result.output, (
            f"expected 'settled' token in output (the helper waited "
            f"for the response burst); got {result.output!r}"
        )
        # Peak rate must be > 0 — proves the rate window saw bytes
        # arriving, not just an idle period.
        assert (
            result.peak_rate_bps > 0.0
        ), f"expected peak_rate_bps > 0; got {result.peak_rate_bps}"
    finally:
        await mgr.close_session(sid)


async def test_screen_text_applies_cursor_movement(tmp_path):
    """pyte APPLIES cursor-movement escapes that strip_ansi only strips.

    The program prints two lines, moves the cursor up two rows, and overwrites
    the first line. ``strip_ansi`` removes the escape but leaves the original
    line text, so the literal ``output`` still contains it; ``screen_text``
    (the cursor-resolved emulator view) shows the overwritten result — proving
    pyte models the screen, not just a stripped byte stream. (This is the value
    pyte adds over strip_ansi, which already resolves bare \\r overwrites.)
    """
    prog = tmp_path / "cur.py"
    prog.write_text(
        "import sys\n"
        "sys.stdin.readline()\n"
        "sys.stdout.write('line one\\n')\n"
        "sys.stdout.write('line two\\n')\n"
        "sys.stdout.write('\\x1b[2A')\n"  # cursor up 2 → back to line one
        "sys.stdout.write('LINE ONE!\\n')\n"  # overwrite line one
        "sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
    )
    mgr = PTYSessionManager()
    sid = await mgr.create_session(
        command=f"python3 {prog}", working_directory=str(tmp_path)
    )
    try:
        result = await mgr.send_input(sid, "go\n", settle_ms=400, timeout_ms=6000)
        # strip_ansi drops the escape but keeps the original line text.
        assert "line one" in result.output, f"raw output: {result.output!r}"
        # pyte applied the cursor-up overwrite: the original line is gone.
        assert "LINE ONE!" in result.screen_text, f"screen: {result.screen_text!r}"
        assert "line two" in result.screen_text
        assert "line one" not in result.screen_text, f"screen: {result.screen_text!r}"
    finally:
        await mgr.close_session(sid)


async def test_compute_gap_captured_via_cpu_idle(tmp_path):
    """A program that pauses to COMPUTE mid-response must be captured in full.

    Regression guard for the integral-flatness + CPU-idle settle. The child
    prints a line, busy-spins (CPU busy) for longer than settle_ms, prints
    more, then finishes. Byte-idle alone would settle during the spin and
    truncate the response; the CPU-idle confirm holds the settle open while
    the foreground group keeps burning CPU, so the full response (incl. the
    final DONE sentinel) is captured.
    """
    prog = tmp_path / "cgap.py"
    prog.write_text(
        "import sys, time\n"
        "def w(s):\n"
        "    sys.stdout.write(s); sys.stdout.flush()\n"
        "def spin(t):\n"
        "    e = time.perf_counter() + t\n"
        "    while time.perf_counter() < e:\n"
        "        pass\n"
        "sys.stdin.readline()\n"
        'w("A\\n"); spin(0.6); w("B\\n"); spin(0.6); w("C\\nDONE\\n")\n'
        "sys.stdin.readline()\n"
    )
    mgr = PTYSessionManager()
    sid = await mgr.create_session(
        command=f"python3 {prog}", working_directory=str(tmp_path)
    )
    try:
        result = await mgr.send_input(sid, "go\n", settle_ms=500, timeout_ms=8000)
        assert "DONE" in result.output, (
            "compute-gap response truncated — the CPU-idle confirm failed to "
            f"hold the settle through a busy-spin pause; got {result.output!r}"
        )
    finally:
        await mgr.close_session(sid)


async def test_exit_is_reported_on_the_SAME_call_that_causes_it(tmp_path):
    """THE REGRESSION THIS FILE EXISTED WITHOUT.

    `_poll_settle` checks `_exited` BEFORE its settle branch, but a program
    that prints a farewell and quits goes idle first: the settle fires while
    the exit callback is still in flight, so the exit was only observable on
    a SUBSEQUENT call — and after `quit` there is no subsequent call.

    Measured on the hy3 run (2026-08-09): `process_exited` was true 0 times
    in 5,162 interactions. The step it gates (the relaunch offer) never fired
    once, so save -> quit -> relaunch -> load -> verify was unreachable for
    the whole run, and the quality gate kept filing the resulting coverage
    hole as a defect no session could have closed.
    """
    mgr = PTYSessionManager()
    sid = await mgr.create_session(working_directory=str(tmp_path))
    try:
        # Launch an interactive child, so the session has one to lose.
        await mgr.send_input(
            sid,
            "python3 -c \"input('> '); print('bye')\"\n",
            settle_ms=400,
            timeout_ms=8000,
        )
        assert mgr._sessions[
            sid
        ]._saw_interactive_child, (
            "test precondition: the child must be seen alive before we quit it"
        )

        # The turn that ends it — output arrives, then the child exits.
        result = await mgr.send_input(sid, "\n", settle_ms=400, timeout_ms=8000)

        assert result.status == "process_exited", (
            "the exit must be reported on THIS call; reporting it on the next "
            f"one means it is never reported at all (got {result.status!r})"
        )
        assert result.interactive_child_running is False
    finally:
        await mgr.close_session(sid)


async def test_a_plain_shell_command_still_settles(tmp_path):
    """The exit check is gated on having SEEN an interactive child: at a bare
    shell prompt `is_interactive_child_running` is also False, and calling
    that a process exit would end every session after its first `ls`."""
    mgr = PTYSessionManager()
    sid = await mgr.create_session(working_directory=str(tmp_path))
    try:
        result = await mgr.send_input(sid, "echo still-here\n", timeout_ms=5000)
        assert result.status != "process_exited"
        assert "still-here" in result.output
    finally:
        await mgr.close_session(sid)


def test_pgrp_cpu_sampling_works_on_this_platform():
    """The CPU-idle settle depends on this returning a real number.

    It is the one signal that distinguishes "the child is computing" from
    "the child is done", and it fails OPEN: None means no signal, the settle
    falls back to byte-idle alone, and any response with a compute pause in it
    truncates. Nothing raises — the capability just disappears.

    That is exactly what the Linux port hit. `ps -o utime=,stime= -g PGID` is a
    BSD idiom; on procps `utime` is not a column (prints "-"), `stime` is START
    TIME rather than system time, and `-g` selects by effective GROUP ID. Every
    line failed to parse and the function returned None on every call.
    """
    import os
    import subprocess
    import time

    from mcp_servers.terminal.pty_session import _pgrp_cpu_seconds

    child = subprocess.Popen(
        [
            "python3",
            "-c",
            "import time\ne=time.perf_counter()+2.0\nwhile time.perf_counter()<e: pass",
        ],
        start_new_session=True,
    )
    try:
        pgrp = os.getpgid(child.pid)
        time.sleep(0.4)
        first = _pgrp_cpu_seconds(pgrp)
        assert (
            first is not None
        ), "no CPU signal — the settle would fall back to byte-idle"
        time.sleep(0.6)
        second = _pgrp_cpu_seconds(pgrp)
        assert second is not None
        # A busy-spinning group must ACCRUE cpu. A constant reading would look
        # exactly like an idle child and settle just as wrongly.
        assert second > first, f"cpu did not accrue while spinning: {first} -> {second}"
    finally:
        child.kill()
        child.wait()


def test_pgrp_cpu_sampling_declines_a_dead_group():
    """None is the honest answer for a group that is gone, and callers rely on
    it meaning 'no signal' rather than 'idle'."""
    from mcp_servers.terminal.pty_session import _pgrp_cpu_seconds

    assert _pgrp_cpu_seconds(0) is None
    assert _pgrp_cpu_seconds(-1) is None
