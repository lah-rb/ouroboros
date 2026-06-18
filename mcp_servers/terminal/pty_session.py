"""PTY session manager — async interactive terminal sessions.

Each session wraps a pseudo-terminal (PTY) connected to a shell or
command. Unlike pipe-based subprocess interaction, PTY sessions:
  - Report isatty() = True to child processes
  - Support interactive programs (input(), readline, REPLs)
  - Use settle-based output detection instead of markers

The settle approach: after sending input, we monitor the output stream.
When no new bytes arrive for `settle_ms` milliseconds, we assume the
program has finished producing output and is waiting for input (or has
exited). This replaces the marker-echo technique which breaks when an
interactive program consumes the marker as user input.

Foreground process detection: on Linux, os.tcgetpgrp(fd) returns the
PID of the process group currently reading from the terminal. When this
differs from the shell's PID, an interactive child is running.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

import pyte
from ptyprocess import PtyProcess
from ptyprocess.ptyprocess import PtyProcessError

from mcp_servers.terminal.ansi import strip_ansi

logger = logging.getLogger(__name__)

# Default settle: how long (ms) output must be silent before we return
DEFAULT_SETTLE_MS = 500
# Default hard timeout (ms) — max wait regardless of settle
DEFAULT_TIMEOUT_MS = 15_000
# Max output buffer size before truncation (chars)
MAX_OUTPUT_CHARS = 50_000

# Terminal-emulator (pyte) screen geometry. Must match the COLUMNS/LINES the
# child sees so line wrapping in the emulated screen matches the program's own.
# HistoryScreen keeps scrollback so a long transcript isn't lost off the top of
# the viewport.
SCREEN_COLS = 120
SCREEN_ROWS = 40
SCREEN_HISTORY = 2000

# ── CPU-idle confirm (settle hardening) ──────────────────────────────
# Byte-idle alone can't tell "done" from "paused to compute mid-response":
# both look like a flat output stream. But a finished program is blocked on
# read() and burns no CPU, while one still computing keeps its foreground
# process group's CPU climbing. So before declaring a clean settle we also
# require that group's CPU to be flat across the settle window. This is
# purely CONSERVATIVE — it can only DELAY a settle, never trigger one early,
# so it cannot introduce truncation; it degrades to byte-idle-only whenever
# CPU can't be sampled (no foreground child / ps unavailable). It does not
# catch a literal sleep() mid-response (a sleeping process is also CPU-idle);
# that needs stdin-read-blocked detection, left as a future upgrade.
CPU_IDLE_EPS_S = 0.05  # CPU-seconds over the settle window that count as "active"
CPU_SAMPLE_MIN_INTERVAL_S = 0.08  # throttle ps sampling during a byte lull

# Container activity probe (sessions whose shell is a `docker exec` relay — tb
# tasks). The host-pgrp CPU probe above sees only the idle relay, so we instead
# sample the CONTAINER's own cumulative work counters via `docker exec`: cpu
# usage (µs) and IO+net bytes, kept SEPARATE — they have wildly different units
# and idle baselines, so summing them against one threshold reads idle as busy
# (cpu-µs alone grows by thousands per window on an idle container). The window
# is "busy" iff cpu OR bytes grew past its OWN threshold. Purely conservative:
# can only DELAY a settle / DEFER the backstop, never settle early; degrades to
# the byte/CPU path when the probe can't be sampled.
CONTAINER_CPU_EPS_USEC = 150_000  # cpu-µs growth/window that counts as real compute
#   (~0.15 core·s over a 0.75s window ≫ idle-daemon noise of a few thousand µs).
CONTAINER_BYTES_EPS = 65_536  # IO+net byte growth/window that counts as active transfer
#   (64 KiB ≫ idle keepalive/log chatter; a real download moves MB/s).
CONTAINER_SAMPLE_MIN_INTERVAL_S = 0.4  # throttle docker-exec probing (it costs ~50-150ms)
CONTAINER_HARD_MAX_MS = 420_000  # absolute ceiling (~7min): an active container defers the
# backstop up to here. MUST stay below the agent's send_input RPC timeout
# (_INTERACT_RPC_TIMEOUT_S in interactive_actions.py) or the RPC aborts the deferral.

# ── Prompt Detection ─────────────────────────────────────────────────
#
# Heuristic prompt patterns checked when output settles. These are
# belt-and-suspenders defaults — flows can supply an explicit prompt
# character via the `expected_prompt` parameter for reliable matching.
#
# Patterns are checked against the last line of stripped output.
# Order: explicit prompt (if provided) → heuristic set.

# Common interactive prompt endings (last non-whitespace on final line)
_HEURISTIC_PROMPT_RE = re.compile(
    r"(?:"
    r">>>\s*$"  # Python REPL
    r"|>\s*$"  # games, node, generic (bare > at end)
    r"|>\s"  # > followed by space (mid-line prompt)
    r"|\$\s*$"  # bash/shell at end of line
    r"|\$\s"  # $ followed by space
    r"|#\s*$"  # root shell at end of line
    r"|#\s"  # # followed by space
    r"|\?\s*$"  # question prompts at end of line
    r"|\?\s"  # question prompts followed by space
    r"|\(y/n\)\s*"  # yes/no prompts
    r")",
    re.IGNORECASE,
)


def _detect_prompt(output: str, expected_prompt: str = "") -> bool:
    """Check whether output ends with a recognizable prompt.

    Args:
        output: The cleaned terminal output to inspect.
        expected_prompt: Explicit prompt string from project config.
            When provided, takes priority over heuristic matching.

    Returns:
        True if the output appears to end at a prompt.
    """
    if not output:
        return False

    # Take the last non-empty line
    lines = output.rstrip().splitlines()
    if not lines:
        return False
    last_line = lines[-1]

    # Explicit prompt match (exact suffix after stripping)
    if expected_prompt:
        return last_line.rstrip().endswith(expected_prompt.rstrip())

    # Heuristic: does the last line end with a known prompt pattern?
    return bool(_HEURISTIC_PROMPT_RE.search(last_line))


def _pgrp_cpu_seconds(pgrp: int) -> float | None:
    """Total CPU seconds (utime+stime) of process group ``pgrp``.

    The out-of-band "is the child still working?" signal used by the settle
    scheme. During a compute-mid-print pause the child keeps accruing CPU even
    though no bytes arrive; once it blocks on read() the total goes flat.
    Returns None when the group can't be sampled (gone, or ``ps`` unavailable)
    — callers treat None as "no CPU signal" and fall back to byte-idle alone.

    NB: callers pass the FOREGROUND process group (``tcgetpgrp(master_fd)``),
    not ``session.pid``. ptyprocess gives the shell a real controlling terminal,
    so the shell does job control: a launched program (e.g. ``python main.py``)
    runs in its OWN foreground pgrp, distinct from the shell's. Sampling
    ``session.pid`` would watch the idle shell and miss the program's CPU; the
    foreground group is whoever is actually computing.
    """
    if pgrp <= 0:
        return None
    try:
        out = subprocess.run(
            ["ps", "-o", "utime=,stime=", "-g", str(pgrp)],
            capture_output=True,
            text=True,
            timeout=1.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    def _sec(tok: str) -> float:
        # ps time format: [[DD-]HH:]MM:SS.ss
        days = 0.0
        if "-" in tok:
            d, tok = tok.split("-", 1)
            days = float(d) * 86400
        parts = tok.split(":")
        secs = float(parts[-1])
        if len(parts) >= 2:
            secs += int(parts[-2]) * 60
        if len(parts) >= 3:
            secs += int(parts[-3]) * 3600
        return days + secs

    total = 0.0
    seen = False
    for line in out.splitlines():
        toks = line.split()
        if len(toks) >= 2:
            try:
                total += _sec(toks[0]) + _sec(toks[1])
                seen = True
            except ValueError:
                continue
    return total if seen else None


def _container_activity(container_name: str) -> tuple[float, float] | None:
    """Cumulative (cpu_usec, io_net_bytes) for a container, sampled via one
    ``docker exec``. cpu_usec = cgroup cpu.stat usage; io_net_bytes = block IO
    (rbytes+wbytes) + net rx+tx. Kept SEPARATE so each can be thresholded against
    its own idle baseline (see CONTAINER_CPU_EPS_USEC / CONTAINER_BYTES_EPS).
    Growth between two reads ⇒ the container is working. Returns None on any
    failure (no docker, container gone, nothing parsed) so the caller degrades to
    the byte/host-CPU settle path.
    """
    if not container_name:
        return None
    try:
        out = subprocess.run(
            [
                "docker", "exec", container_name, "sh", "-c",
                "cat /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/io.stat /proc/net/dev "
                "2>/dev/null",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    cpu = 0.0
    byts = 0.0
    seen = False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("usage_usec"):  # cgroup v2 cpu.stat
            try:
                cpu += float(s.split()[1])
                seen = True
            except (IndexError, ValueError):
                pass
        elif "rbytes=" in s or "wbytes=" in s:  # cgroup v2 io.stat
            for tok in s.split():
                if tok.startswith(("rbytes=", "wbytes=")):
                    try:
                        byts += float(tok.split("=", 1)[1])
                        seen = True
                    except ValueError:
                        pass
        elif ":" in s and not s.startswith(("Inter", "face", "lo:")):  # /proc/net/dev
            try:
                cols = s.split(":", 1)[1].split()
                byts += float(cols[0]) + float(cols[8])  # rx + tx bytes
                seen = True
            except (IndexError, ValueError):
                pass
    return (cpu, byts) if seen else None


def _diagnose_child(pid: int) -> str:
    """Best-effort true state of the session's child for phantom diagnosis.

    Called at a zero-byte settle timeout to tell apart the failure modes a
    raw-slave PTY (no echo) otherwise hides behind ``total_bytes=0``:
      - ``comm=python`` → the fork never reached ``execve`` (the
        fork-in-a-multithreaded-process hazard the reader threads introduced):
        a hung copy of THIS server, alive but never bash → emits nothing ever.
      - ``comm=bash``, state S/I → bash is alive but not reading/printing.
      - state ``Z`` → child exited but the reader missed EOF.

    Walks the whole subtree under ``pid`` — at an interactive turn the shell is
    sleeping and the program-under-test is the shell's CHILD (e.g. ``python
    main.py``), so the program's state (S = sleeping on read ⇒ input was never
    delivered; R = running; Z = crashed-unreaped) is what actually explains a
    zero-byte response.

    Deliberately ``ps``-only — NO ``waitpid`` — so it never reaps the child out
    from under the reader loop / close_session (which would lose the exit code).
    macOS ``ps`` state codes: R runnable, S/I sleeping, U uninterruptible, T
    stopped, Z zombie.
    """
    if pid <= 0:
        return "pid<=0"
    try:
        snap = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,stat=,comm="],
            capture_output=True,
            text=True,
            timeout=1.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "ps_failed"

    # Build pid -> (ppid, stat, comm) and a parent -> children index.
    info: dict[int, tuple[int, str, str]] = {}
    children: dict[int, list[int]] = {}
    for line in snap.splitlines():
        toks = line.split(None, 3)
        if len(toks) < 4:
            continue
        try:
            p, pp = int(toks[0]), int(toks[1])
        except ValueError:
            continue
        info[p] = (pp, toks[2], toks[3])
        children.setdefault(pp, []).append(p)

    if pid not in info:
        return "gone(reaped/exited)"

    # BFS the subtree (cap to avoid pathological output).
    parts: list[str] = []
    queue = [(pid, 0)]
    seen = set()
    while queue and len(parts) < 8:
        cur, depth = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        pp, stat, comm = info[cur]
        parts.append(f"{'  ' * depth}{comm}(pid={cur},stat={stat})")
        for c in sorted(children.get(cur, [])):
            queue.append((c, depth + 1))
    return " | ".join(parts)


@dataclass
class InteractionResult:
    """Result of a send_input or read_output operation."""

    output: str
    status: str  # "settled", "timeout", "process_exited", "error", "no_new_output"
    exit_code: int | None = None  # Only set when process_exited
    interactive_child_running: bool = (
        False  # True when a child process owns the terminal
    )
    prompt_detected: bool = False  # True when output ends with a recognized prompt

    # ── Flow-profile metadata (6c2 round) ────────────────────────
    #
    # A PTY stream is a continuous signal, not a discrete request/
    # response channel, but the earlier settle logic forced an RPC
    # frame onto it. 6c2 surfaced the failure mode: legitimate
    # responses would hit silent mid-response gaps (Python print
    # buffering + event-loop scheduling between the reader task and
    # the poll loop), the old detector would time out with
    # no_new_output + output="", and the evaluator would honestly
    # report a "hang" that wasn't actually there — then diagnose
    # would name the parser, patches would apply, and nothing
    # converged because there was nothing to fix.
    #
    # The new model samples the buffer length over time and
    # publishes derived signal features alongside the result. The
    # consumer in agent/actions/interactive_actions.py uses these
    # to decide whether an ambiguous annotation is warranted or
    # whether we have enough signal to describe what happened on
    # the stream without narrating state we can't observe.
    total_bytes_received: int = 0
    # Cumulative bytes observed across this interaction (i.e., the
    # integral). Zero → program produced no output at all; nonzero
    # → something came back, even if we then ran out of patience.

    peak_rate_bps: float = 0.0
    # Peak observed byte-rate (bytes per second) during the wait
    # window. A crisp burst has a clear peak well above zero; a
    # silent-but-buffered child has a peak of zero. This is the
    # signal we use to confirm that a response actually happened.

    idle_duration_s: float = 0.0
    # Time since last observed byte at the moment the wait returned.
    # Combined with total_bytes_received, distinguishes the four
    # real states: (bytes=0, idle<timeout) doesn't occur, we keep
    # waiting; (bytes=0, idle=timeout) → no response; (bytes>0,
    # idle>=settle) → responded and idle; (bytes>0, idle<settle) →
    # still producing, cut off by timeout.

    settled_cleanly: bool = False
    # True when the flow profile matches a clean burst-then-idle
    # shape: peak rate was observed, followed by idle_duration
    # exceeding the settle window. The old `settled` status is a
    # superset — it also fires for the pathological
    # settled-with-zero-bytes case that 6c2 exposed. This boolean
    # is the strict signal consumers should prefer.

    screen_text: str = ""
    # The pyte terminal-emulator screen at the moment this interaction
    # returned — the cursor-resolved grid (\r overwrites, cursor moves, line
    # wrap, ANSI all applied), trailing blank lines trimmed. `output` is the
    # literal per-turn byte delta; `screen_text` is "what the terminal shows
    # now". Consumers wanting clean state (esp. for programs that repaint or use
    # progress lines) should prefer this; it's "" if the emulator is unavailable.


@dataclass
class SessionInfo:
    """Metadata about an active PTY session."""

    session_id: str
    pid: int  # Shell PID
    master_fd: int  # PTY master file descriptor (== proc.fd)
    shell_pgid: int  # Shell's process group ID
    working_directory: str
    # The ptyprocess.PtyProcess that owns the fork/exec, controlling terminal,
    # child liveness (isalive → waitpid), and teardown. We read its fd directly
    # via os.read on the reader thread, but ptyprocess owns ALL waitpid calls —
    # doing our own would make its isalive() hit ECHILD and raise.
    proc: object | None = field(default=None, repr=False)
    expected_prompt: str = ""  # Explicit prompt character from project config
    # Container NAME for sessions whose shell is a `docker exec` relay (tb tasks).
    # Empty for local sessions. When set, the settle loop probes container-side
    # CPU/IO activity (the host-pgrp CPU probe sees only the idle relay).
    container_name: str = ""
    turn_count: int = 0
    history: list[dict] = field(default_factory=list)
    # Output capture. ``_output_buffer`` is a tail-window of recent bytes
    # (truncated when it exceeds 2× MAX_OUTPUT_CHARS). ``bytes_received_total``
    # is a monotonic counter — every byte the protocol delivers increments
    # it. Settle detection measures growth via the counter (immune to
    # truncation); ``_drain_buffer`` slices the tail for the response text.
    _output_buffer: bytearray = field(default_factory=bytearray)
    bytes_received_total: int = 0
    _exited: bool = False
    _exit_code: int | None = None
    # Thread-based read path. A per-session daemon thread does blocking
    # select()+os.read() on master_fd into _output_buffer. This replaces
    # asyncio.connect_read_pipe, which is built for pipes and intermittently
    # failed to deliver PTY-master readability events (kqueue on a tty-like
    # master), producing 30s zero-byte read timeouts on small responses. A
    # blocking reader has no selector/event-loop dependency, so it can't miss
    # a read. ``_buffer_lock`` guards buffer mutations between the reader
    # thread and the event-loop drain/settle (bytes_received_total is a plain
    # int — GIL-atomic — so the settle poll reads it lock-free).
    _reader_thread: object | None = field(default=None, repr=False)
    _reader_stop: object | None = field(default=None, repr=False)
    _buffer_lock: object = field(default_factory=threading.Lock, repr=False)
    _closed: bool = False
    # Terminal-emulator screen state (pyte). The reader feeds raw bytes into
    # ``byte_stream`` → ``screen``, so ``screen.display`` is the cursor-resolved
    # grid the program actually paints — \r overwrites, cursor moves, line wrap
    # and ANSI all applied. The raw ``_output_buffer`` is the literal byte log
    # (per-turn deltas); the screen is the "what's on screen now" view. Both are
    # guarded by ``_buffer_lock`` (pyte is not thread-safe).
    screen: object | None = field(default=None, repr=False)
    byte_stream: object | None = field(default=None, repr=False)
    # Last send_input write accounting (instruments the input-side phantom). A
    # non-blocking PTY master can short-write or EAGAIN under load; if the
    # trailing newline is dropped the child blocks forever in a canonical
    # read(). last_write_partials counts how many retries the write-all loop
    # needed — >0 on a turn that would otherwise have phantomed is the proof.
    last_write_intended: int = 0
    last_write_partials: int = 0


def _record_proc_exit(session: SessionInfo) -> None:
    """Record the child's exit code from its (already-reaped) ptyprocess.

    ptyprocess.isalive()/close() reap the child and store exitstatus or
    signalstatus; this maps those onto the session in our convention (a clean
    exit is the status; a signal is the negated signal number). Call only AFTER
    isalive()/close() has run, so the status fields are populated.
    """
    proc = session.proc
    if proc is None:
        return
    if getattr(proc, "exitstatus", None) is not None:
        session._exit_code = proc.exitstatus
    elif getattr(proc, "signalstatus", None) is not None:
        session._exit_code = -proc.signalstatus


def _reader_loop(session: SessionInfo) -> None:
    """Blocking read pump for a PTY master, run on a per-session daemon thread.

    Replaces asyncio.connect_read_pipe (which intermittently failed to deliver
    PTY-master readability, causing 30s zero-byte read timeouts on small
    responses). ``select()`` with a short timeout lets us notice the stop
    signal; ``os.read`` after readability always returns the bytes the kernel
    has — no selector/event-loop dependency, so a response is never silently
    missed.

    Appends into the session's tail buffer (under ``_buffer_lock``) and bumps
    the monotonic ``bytes_received_total`` the settle logic reads. On EOF/EIO
    (child closed the PTY) it marks the session exited and reaps the child.
    """
    fd = session.master_fd
    stop = session._reader_stop
    while not stop.is_set():
        try:
            r, _, _ = select.select([fd], [], [], 0.1)
        except (OSError, ValueError):
            break  # fd closed underneath us
        if not r:
            continue
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            continue  # spurious readability; try again
        except OSError:
            data = b""  # EIO/EOF on a PTY master — classified below
        if not data:
            # A PTY master read returns EIO/empty when NO foreground process is
            # writing. That is real EOF only if the session SHELL itself has
            # exited; if a foreground child exits while the shell lives on, the
            # read can transiently EIO — breaking here would kill the reader for
            # the rest of the session and drop every later response (~30s
            # phantom). proc.isalive() (ptyprocess's own non-blocking waitpid,
            # which also handles the zombie case and stores the exit status)
            # tells these apart. We must NOT call os.waitpid ourselves — that
            # would reap the child out from under ptyprocess and make a later
            # isalive() raise ECHILD.
            proc = session.proc
            try:
                alive = proc.isalive() if proc is not None else False
            except PtyProcessError:
                alive = False  # already reaped elsewhere → treat as exited
            if alive:
                time.sleep(0.02)  # shell alive, no foreground writer → keep reading
                continue
            _record_proc_exit(session)
            session._exited = True
            logger.info(
                "PTY reader %s: session shell (pid=%d) exited — stopping",
                session.session_id,
                session.pid,
            )
            break
        with session._buffer_lock:
            session._output_buffer.extend(data)
            session.bytes_received_total += len(data)
            # Truncation guard — keep a tail of MAX_OUTPUT_CHARS once the
            # buffer crosses 2×. The monotonic counter is unaffected, so
            # settle detection keeps measuring real growth.
            buf = session._output_buffer
            if len(buf) > MAX_OUTPUT_CHARS * 2:
                del buf[: len(buf) - MAX_OUTPUT_CHARS]
            # Feed the terminal emulator (cursor-resolved screen state). Never
            # let a parser hiccup kill the reader — the raw buffer is the
            # source of truth; the screen is a best-effort view on top.
            bs = session.byte_stream
            if bs is not None:
                try:
                    bs.feed(data)
                except Exception:  # noqa: BLE001 - defensive: keep reading
                    pass
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "PTY read: session=%s bytes=%d total=%d preview=%r",
                session.session_id,
                len(data),
                session.bytes_received_total,
                data[:60],
            )


class PTYSessionManager:
    """Manages multiple PTY sessions with async I/O."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}

    async def create_session(
        self,
        command: str | None = None,
        working_directory: str = ".",
        env: dict[str, str] | None = None,
        expected_prompt: str = "",
        container_name: str = "",
    ) -> str:
        """Create a new PTY session.

        Args:
            command: Command to run (default: bash).
            working_directory: Working directory for the session.
            env: Additional environment variables.
            expected_prompt: The prompt character the interactive program
                displays when waiting for input (e.g. "> ", ">>> ").
                Used for positive prompt detection in settle logic.

        Returns:
            A unique session_id.
        """
        session_id = uuid4().hex[:12]

        # Resolve working directory
        cwd = os.path.abspath(working_directory)
        if not os.path.isdir(cwd):
            os.makedirs(cwd, exist_ok=True)

        # Build environment
        child_env = dict(os.environ)
        child_env["TERM"] = "dumb"  # Minimize escape sequences
        child_env["NO_COLOR"] = "1"
        child_env["COLUMNS"] = str(SCREEN_COLS)
        child_env["LINES"] = str(SCREEN_ROWS)
        if env:
            child_env.update(env)

        # Spawn the child under a PTY via ptyprocess. This replaces a hand-rolled
        # openpty + fork + setsid + dup2 + execvpe. ptyprocess gives the child a
        # real CONTROLLING TERMINAL (the old code skipped TIOCSCTTY, so
        # tcgetpgrp(master) returned 0 and foreground detection never worked) and
        # owns child liveness/reaping. echo=False so the only bytes we capture
        # are the program's own output — input isn't echoed back — which is what
        # the settle/byte-counting logic assumes (the old code got this via a
        # raw slave). The shell stays canonical otherwise, which is the normal
        # mode for line-oriented interactive programs (input(), REPLs, the
        # challenge games we test against).
        cmd = command or "/bin/bash"
        cmd_parts = cmd.split() if isinstance(cmd, str) else list(cmd)
        try:
            proc = PtyProcess.spawn(
                cmd_parts,
                cwd=cwd,
                env=child_env,
                echo=False,
                dimensions=(40, 120),  # rows, cols — matches LINES/COLUMNS
            )
        except Exception as e:
            logger.error("PTY spawn failed for %s: %s", cmd, e)
            raise

        master_fd = proc.fd
        # Non-blocking master: the reader thread selects then os.reads; a
        # blocking fd could wedge on a spurious readability. ptyprocess never
        # needs the fd blocking (isalive/terminate use waitpid/signals and we
        # never call proc.read()).
        os.set_blocking(master_fd, False)
        pid = proc.pid
        # ptyprocess setsid's the child and gives it the controlling terminal, so
        # the shell is its own session+group leader: pgid == pid. No getpgid race.
        shell_pgid = pid

        session = SessionInfo(
            session_id=session_id,
            pid=pid,
            master_fd=master_fd,
            shell_pgid=shell_pgid,
            working_directory=cwd,
            expected_prompt=expected_prompt,
            container_name=container_name,
            proc=proc,
        )

        # Terminal-emulator screen, sized to match the child's COLUMNS×LINES so
        # wrapping agrees. Created BEFORE the reader thread so the very first
        # bytes are parsed into it.
        screen = pyte.HistoryScreen(
            SCREEN_COLS, SCREEN_ROWS, history=SCREEN_HISTORY, ratio=0.5
        )
        session.screen = screen
        session.byte_stream = pyte.ByteStream(screen)

        # Start the blocking reader thread (see _reader_loop). It owns reads on
        # master_fd; the manager owns the fd's lifetime and closes it in
        # close_session. No asyncio transport — the previous connect_read_pipe
        # path intermittently dropped PTY-master readability events.
        session._reader_stop = threading.Event()
        session._reader_thread = threading.Thread(
            target=_reader_loop,
            args=(session,),
            name=f"pty-reader-{session_id}",
            daemon=True,
        )
        session._reader_thread.start()

        self._sessions[session_id] = session

        # Angle-3 (refactored to differential settle): use the same
        # _poll_settle helper as send_input's _wait_for_settle. The
        # previous "any growth, any 50ms pause" predicate could exit
        # during mid-PS1 print pauses — a brief gap inside one bash
        # banner write looked like settled, but the rest of PS1 then
        # arrived after our buffer.clear() and got misattributed to
        # the consumer's first send_input. The differential predicate
        # requires sustained idle (200ms) AFTER a non-zero peak rate
        # is observed, which doesn't trip on intra-burst gaps.
        # 500ms cap so a silent shell doesn't stall creation.
        poll = await self._poll_settle(
            session,
            start_total=0,
            settle_ms=200,
            timeout_ms=500,
        )
        # Angle-5 logging: surface every create-time settle so future
        # zero-byte residuals are diagnosable from logs alone.
        # ``saw_burst=False`` after a 500ms wait points at "shell
        # never produced its banner" — distinguishable here from
        # "shell produced banner but kept printing past 500ms" which
        # would show settled=False with saw_burst=True.
        logger.debug(
            "PTY create_session settle: session=%s total_bytes=%d "
            "peak_rate_bps=%.1f idle_s=%.3f saw_burst=%s settled=%s "
            "timed_out=%s",
            session_id,
            poll["total_bytes"],
            poll["peak_rate_bps"],
            poll["idle_duration_s"],
            poll["saw_burst"],
            poll["settled"],
            poll["timed_out"],
        )

        # Consume the initial prompt/MOTD. Reset the monotonic counter
        # too so the first send_input's "growth from start_total" is
        # measured against an empty session, not whatever the shell
        # banner happened to deliver during init. Reset the emulator screen
        # likewise so its first read reflects the consumer's session, not the
        # shell banner. Under the lock so the reader can't feed mid-reset.
        with session._buffer_lock:
            session._output_buffer.clear()
            session.bytes_received_total = 0
            if session.screen is not None:
                session.screen.reset()

        logger.info(
            "PTY session %s created: pid=%d, cmd=%s, cwd=%s",
            session_id,
            pid,
            cmd,
            cwd,
        )

        return session_id

    async def send_input(
        self,
        session_id: str,
        text: str,
        await_response: bool = True,
        settle_ms: int = DEFAULT_SETTLE_MS,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> InteractionResult:
        """Send input text to a PTY session.

        Args:
            session_id: The session to send to.
            text: Text to send (including newlines if needed).
            await_response: If True, wait for output to settle after sending.
            settle_ms: How long (ms) output must be silent before returning.
            timeout_ms: Hard timeout (ms) regardless of settle.

        Returns:
            InteractionResult with output and status.
        """
        session = self._sessions.get(session_id)
        if not session:
            return InteractionResult(
                output="",
                status="error",
            )

        if session._exited:
            return InteractionResult(
                output=self._drain_buffer(session),
                status="process_exited",
                exit_code=session._exit_code,
            )

        # Angle 4 / Option B: do NOT clear the buffer here. Capture
        # the monotonic byte counter as our checkpoint instead. Any
        # bytes already in the buffer (late arrivals from a previous
        # turn that landed after that turn's settle) will be drained
        # alongside this turn's response so the model sees them
        # rather than having them silently discarded.
        start_total = session.bytes_received_total

        # Write the FULL input to the PTY master. The master is non-blocking, so
        # os.write can return a SHORT count (wrote fewer bytes than asked) or
        # raise EAGAIN under load. The old code ignored the return value — and
        # dropping even the trailing newline strands the child in a canonical
        # read() waiting for the rest of the line: the input-side phantom (child
        # sleeping on read, total_bytes=0, no_new_output for the full timeout).
        # Loop until every byte is delivered. last_write_partials records how
        # many retries were needed so the no_new_output diagnostic can prove
        # whether short-writes are the culprit.
        payload = text.encode("utf-8")
        mv = memoryview(payload)
        written = 0
        partials = 0
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 5.0
        while written < len(payload):
            try:
                n = os.write(session.master_fd, mv[written:])
            except BlockingIOError:
                n = 0  # slave input queue full — wait for the child to drain it
            except OSError as e:
                logger.error("Failed to write to PTY %s: %s", session_id, e)
                return InteractionResult(output="", status="error")
            written += n
            if written < len(payload):
                partials += 1
                if loop.time() > deadline:
                    logger.error(
                        "PTY write stalled %s: delivered %d/%d bytes",
                        session_id,
                        written,
                        len(payload),
                    )
                    return InteractionResult(output="", status="error")
                await asyncio.sleep(0.005)
        session.last_write_intended = len(payload)
        session.last_write_partials = partials
        if partials:
            logger.warning(
                "PTY write needed %d retries to deliver %d bytes (session=%s) — "
                "non-blocking short-write/EAGAIN under load; the write-all loop "
                "recovered it (this is the input-side phantom vector)",
                partials,
                len(payload),
                session_id,
            )

        session.turn_count += 1

        if not await_response:
            return InteractionResult(output="", status="settled")

        # Wait for output to settle
        return await self._wait_for_settle(session, start_total, settle_ms, timeout_ms)

    async def read_output(
        self,
        session_id: str,
        settle_ms: int = DEFAULT_SETTLE_MS,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> InteractionResult:
        """Read available output from a PTY session.

        Waits for output to settle (no new bytes for settle_ms) or timeout.

        Args:
            session_id: The session to read from.
            settle_ms: How long (ms) output must be silent before returning.
            timeout_ms: Hard timeout (ms) regardless of settle.

        Returns:
            InteractionResult with output and status.
        """
        session = self._sessions.get(session_id)
        if not session:
            return InteractionResult(output="", status="error")

        if session._exited and not session._output_buffer:
            return InteractionResult(
                output="",
                status="process_exited",
                exit_code=session._exit_code,
            )

        # read_output is "what's pending right now" — measure growth
        # from this instant (anything already in the buffer will be
        # included in the drained output but won't count toward the
        # "did new bytes arrive" decision).
        start_total = session.bytes_received_total
        return await self._wait_for_settle(session, start_total, settle_ms, timeout_ms)

    async def close_session(self, session_id: str) -> dict:
        """Close a PTY session and clean up.

        Returns:
            Dict with success, total_turns, and transcript.
        """
        session = self._sessions.pop(session_id, None)
        if not session:
            return {"success": False, "total_turns": 0, "transcript": ""}

        session._closed = True

        # Stop the reader thread and wait for it to exit BEFORE closing the fd
        # (the thread does select/os.read on master_fd; closing it underneath a
        # live read would race). It polls the stop event every 100ms.
        if session._reader_stop is not None:
            session._reader_stop.set()
        if session._reader_thread is not None:
            session._reader_thread.join(timeout=1.0)

        # Terminate + close via ptyprocess (it owns both the fd and the child).
        # The reader thread is already stopped above, so there's no concurrent
        # os.read on the fd and no concurrent waitpid to race isalive(). close()
        # closes the master fd and, with force=True, escalates SIGHUP → SIGINT →
        # SIGTERM → SIGKILL until the child is gone.
        proc = session.proc
        if proc is not None:
            try:
                proc.close(force=True)
            except (OSError, PtyProcessError):
                pass  # already dead / fd already closed
            if session._exit_code is None:
                _record_proc_exit(session)

        # Build transcript
        transcript_parts = []
        for entry in session.history:
            transcript_parts.append(f"[Turn {entry.get('turn', '?')}]")
            if entry.get("input"):
                transcript_parts.append(f"  Input: {entry['input']}")
            if entry.get("output"):
                transcript_parts.append(f"  Output: {entry['output']}")
        transcript = "\n".join(transcript_parts)

        logger.info(
            "PTY session %s closed: %d turns",
            session_id,
            session.turn_count,
        )

        return {
            "success": True,
            "total_turns": session.turn_count,
            "transcript": transcript,
        }

    def get_foreground_pid(self, session_id: str) -> int | None:
        """Get the PID of the foreground process group.

        If this differs from shell_pgid, an interactive child is running.

        Returns:
            Foreground PGID, or None if session not found or detection fails.
        """
        session = self._sessions.get(session_id)
        if not session:
            return None
        try:
            return os.tcgetpgrp(session.master_fd)
        except OSError:
            return None

    def is_interactive_child_running(self, session_id: str) -> bool:
        """Check if a child process (not the shell) owns the terminal."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        fg = self.get_foreground_pid(session_id)
        if fg is None:
            return False
        return fg != session.shell_pgid

    def list_sessions(self) -> list[str]:
        """Return IDs of all active sessions."""
        return list(self._sessions.keys())

    @staticmethod
    def _render_screen(session: SessionInfo) -> str:
        """Render a session's pyte screen to text (trailing blanks trimmed).

        The cursor-resolved viewport — what the terminal currently shows. Held
        under the buffer lock because the reader thread feeds the same screen
        and pyte is not thread-safe. Returns "" if the emulator is unavailable.
        """
        screen = session.screen
        if screen is None:
            return ""
        try:
            with session._buffer_lock:
                lines = [line.rstrip() for line in screen.display]
        except Exception:  # noqa: BLE001 - never let rendering break a result
            return ""
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def read_screen(self, session_id: str) -> str:
        """Return the current cursor-resolved screen (pyte) as text.

        "What the terminal shows right now" — \\r overwrites, cursor moves, line
        wrap and ANSI already applied. Empty string if the session is gone.
        """
        session = self._sessions.get(session_id)
        if not session:
            return ""
        return self._render_screen(session)

    # ── Internal ──────────────────────────────────────────────────

    # NOTE: reading is driven by the module-level _reader_loop on a per-session
    # daemon thread (started in create_session). Child liveness and reaping are
    # owned by the session's ptyprocess (proc.isalive()/proc.close()); we never
    # call os.waitpid ourselves. _record_proc_exit maps proc's exit/signal
    # status onto the session after the child is reaped.

    async def _poll_settle(
        self,
        session: SessionInfo,
        start_total: int = 0,
        settle_ms: int = 750,
        timeout_ms: int = 30000,
    ) -> dict:
        """Poll bytes_received_total for the integral-flatness settle shape.

        Pure waiting loop — no buffer drain, no history record. The
        caller decides what to do with the buffer afterward. Shared
        between :meth:`_wait_for_settle` (the runtime path used by
        send_input/read_output) and ``create_session``'s post-fork
        settle, so both paths use the same settle detection.

        Settle predicate (integral-based): bytes have arrived
        (``total_bytes > 0``) AND the cumulative byte count has been
        FLAT for ``settle_s`` (idle since the last byte ≥ settle_s),
        AND the foreground process group is not still burning CPU
        across that window (the conservative CPU-idle confirm). This
        decides closure from the integral and its windowed flatness —
        never from a measured byte-rate, which is sampling-fragile
        (a burst under a busy loop lands in one sample and reads as
        rate 0). ``peak_rate_bps`` is retained as telemetry only.

        Returns a dict with:
          saw_burst: bool — kept for back-compat; now == (total_bytes > 0)
          settled: bool — bytes flat for settle_s AND CPU not still climbing
          process_exited: bool — child process exited during wait
          timed_out: bool — deadline reached without settling
          total_bytes: int — bytes since start_total
          peak_rate_bps: float — observed peak in bytes/sec (telemetry)
          idle_duration_s: float — time since last byte arrival
        """
        loop = asyncio.get_event_loop()
        settle_s = settle_ms / 1000.0
        timeout_s = timeout_ms / 1000.0
        deadline = loop.time() + timeout_s
        # Poll interval — small enough to catch fast bursts without
        # burning CPU, big enough that rate estimates over a window
        # aren't dominated by sample noise.
        poll_s = min(0.05, settle_s / 4)

        # Sample ring — (timestamp, cumulative_bytes_observed). Retained for
        # peak_rate_bps, which is now TELEMETRY ONLY: closure is decided on the
        # integral (cumulative bytes) and its flatness over the settle window,
        # not on a measured rate. Estimating the rate (a derivative) from a
        # sample ring is sampling-fragile — under a busy/starved event loop the
        # burst lands in a single sample and the rate reads 0, which used to
        # wedge the old `peak_rate>0` gate into a never-settles timeout. The
        # integral never lies, so we drive the decision from it directly.
        samples: list[tuple[float, int]] = []
        rate_window_s = max(0.5, settle_s * 2)
        peak_rate_bps = 0.0
        # Integral-flatness clock: when bytes last grew (None until first byte).
        # idle = now - last_byte_time; "bytes flat for settle_s" is closure.
        last_byte_time: float | None = None
        # CPU-idle confirm: sliding ring of (t, cpu_seconds) sampled during a
        # byte lull, pruned to the settle window. <2 samples / unsamplable ⇒ no
        # CPU signal ⇒ fall back to byte-idle alone.
        cpu_samples: list[tuple[float, float]] = []
        last_cpu_sample_t = 0.0
        # Container-activity confirm — the analogue of cpu_samples for a session
        # whose shell is a docker-exec relay (the host pgrp sees only the idle
        # relay). Sampled during a byte lull, pruned to the settle window, reset
        # on byte growth. container_busy gates the settle AND defers the backstop
        # while the container is doing silent work (a --quiet download), up to an
        # absolute ceiling; <2 spanning samples ⇒ no signal ⇒ byte/CPU path.
        container_name = session.container_name
        container_samples: list[tuple[float, float]] = []
        last_container_sample_t = 0.0
        last_container_active_t = 0.0  # stale until activity is actually observed
        container_hard_deadline = loop.time() + CONTAINER_HARD_MAX_MS / 1000.0
        backstop_grace_s = max(2.0, settle_s * 2)

        def _current_rate_bps(now: float) -> float:
            """Bytes/sec over the trailing rate window."""
            if len(samples) < 2:
                return 0.0
            cutoff = now - rate_window_s
            baseline = None
            for ts, cum in samples:
                if ts >= cutoff:
                    baseline = (ts, cum)
                    break
            if baseline is None:
                baseline = samples[-1]
            last_ts, last_cum = samples[-1]
            dt = last_ts - baseline[0]
            if dt <= 0.0:
                return 0.0
            dbytes = last_cum - baseline[1]
            return max(0.0, dbytes / dt)

        while True:
            now = loop.time()
            current_total = session.bytes_received_total
            total_bytes = max(current_total - start_total, 0)

            # Record sample + maintain the integral-flatness clock. Set
            # last_byte_time on ANY observed growth — INCLUDING the very first
            # sample (the old code only updated it when `samples` was already
            # non-empty, so a burst that landed before the first poll never
            # registered an arrival time → idle never advanced → never settled).
            grew = bool(samples) and current_total > samples[-1][1]
            first_data = not samples and current_total > start_total
            if grew or first_data:
                last_byte_time = now
                cpu_samples.clear()  # lull (if any) is over; restart CPU window
                container_samples.clear()  # ditto for the container-activity window
            samples.append((now, current_total))
            cutoff = now - (rate_window_s * 2)
            samples = [s for s in samples if s[0] >= cutoff]

            current_rate = _current_rate_bps(now)  # telemetry only
            if current_rate > peak_rate_bps:
                peak_rate_bps = current_rate
            saw_data = total_bytes > 0
            idle_duration_s = (
                (now - last_byte_time) if last_byte_time is not None else 0.0
            )

            # CPU sampling during a byte lull (throttled), pruned to the window.
            if (
                saw_data
                and idle_duration_s > 0
                and now - last_cpu_sample_t >= CPU_SAMPLE_MIN_INTERVAL_S
            ):
                # Sample the FOREGROUND group (the program actually computing),
                # not session.pid. With a real controlling terminal the shell
                # does job control, so a launched program runs in its own pgrp;
                # session.pid would watch the idle shell and miss its CPU.
                fg_pgrp = self.get_foreground_pid(session.session_id)
                cpu_now = _pgrp_cpu_seconds(
                    fg_pgrp if fg_pgrp and fg_pgrp > 0 else session.pid
                )
                if cpu_now is not None:
                    cpu_samples.append((now, cpu_now))
                last_cpu_sample_t = now
            cpu_samples = [c for c in cpu_samples if c[0] >= now - settle_s]

            # ── Container-activity sampling + confirm (docker-exec sessions) ──
            # The analogue of the CPU confirm for a container whose work the host
            # pgrp can't see. Sample only when bytes are NOT currently flowing
            # (``not grew`` — a flowing stream is already its own activity signal),
            # throttled because the probe shells out. Run it off-loop so the
            # ~100ms docker-exec doesn't stall the poll. container_busy can only
            # DELAY a settle / DEFER the backstop, never close early.
            container_busy = False
            if container_name:
                if (
                    not grew
                    and now - last_container_sample_t >= CONTAINER_SAMPLE_MIN_INTERVAL_S
                ):
                    work = await loop.run_in_executor(
                        None, _container_activity, container_name
                    )
                    now = loop.time()  # the probe took time; re-read the clock
                    if work is not None:
                        container_samples.append((now, work[0], work[1]))  # t, cpu, bytes
                    last_container_sample_t = now
                container_samples = [
                    c for c in container_samples if c[0] >= now - settle_s
                ]
                # Busy iff cpu OR bytes grew past its OWN threshold over a window
                # spanning ≥ half the settle time. Separate thresholds: cpu-µs and
                # byte counts have different units and idle baselines.
                container_busy = len(container_samples) >= 2 and (
                    container_samples[-1][0] - container_samples[0][0]
                ) >= settle_s * 0.5 and (
                    (container_samples[-1][1] - container_samples[0][1])
                    >= CONTAINER_CPU_EPS_USEC
                    or (container_samples[-1][2] - container_samples[0][2])
                    >= CONTAINER_BYTES_EPS
                )
                if container_busy:
                    last_container_active_t = now

            # ── Process exit ────────────────────────────────────
            if session._exited:
                # Brief tail to catch a final burst before
                # connection_lost wraps things up.
                await asyncio.sleep(0.05)
                final_total = max(0, session.bytes_received_total - start_total)
                return {
                    "saw_burst": final_total > 0,
                    "settled": False,
                    "process_exited": True,
                    "timed_out": False,
                    "total_bytes": final_total,
                    "peak_rate_bps": peak_rate_bps,
                    "idle_duration_s": idle_duration_s,
                }

            # ── Integral-flatness + CPU-idle confirm (clean settle) ──
            # Closure = bytes arrived AND have been flat for settle_s. Before
            # returning, confirm the foreground program isn't still computing
            # (CPU climbing across the window). The CPU check is conservative:
            # it can only DELAY settle, and degrades to byte-idle-only when the
            # window lacks ≥2 spanning samples (unsamplable / too-short lull).
            if saw_data and idle_duration_s >= settle_s:
                cpu_busy = (
                    len(cpu_samples) >= 2
                    and (cpu_samples[-1][0] - cpu_samples[0][0]) >= settle_s * 0.5
                    and (cpu_samples[-1][1] - cpu_samples[0][1]) >= CPU_IDLE_EPS_S
                )
                if not cpu_busy and not container_busy:
                    return {
                        "saw_burst": saw_data,
                        "settled": True,
                        "process_exited": False,
                        "timed_out": False,
                        "total_bytes": total_bytes,
                        "peak_rate_bps": peak_rate_bps,
                        "idle_duration_s": idle_duration_s,
                    }

            # ── Hard timeout ────────────────────────────────────
            # A container session DEFERS the backstop while the container is still
            # working — recent container activity OR bytes still arriving — so a
            # silent --quiet download (invisible to the host-pgrp CPU probe) is
            # never aborted mid-flight. Bounded by an absolute ceiling so a genuine
            # hang (no output AND no container activity) still returns.
            recent_byte = last_byte_time is not None and (now - last_byte_time) < backstop_grace_s
            container_working = container_name and now < container_hard_deadline and (
                (now - last_container_active_t) < backstop_grace_s or recent_byte
            )
            if now >= deadline and not container_working:
                return {
                    "saw_burst": saw_data,
                    "settled": False,
                    "process_exited": False,
                    "timed_out": True,
                    "total_bytes": total_bytes,
                    "peak_rate_bps": peak_rate_bps,
                    "idle_duration_s": idle_duration_s,
                }

            await asyncio.sleep(poll_s)

    async def _wait_for_settle(
        self,
        session: SessionInfo,
        start_total: int,
        settle_ms: int,
        timeout_ms: int,
    ) -> InteractionResult:
        """Wait for output to settle or timeout.

        Uses :meth:`_poll_settle` for the differential burst-then-idle
        detection, then drains the buffer and records history. The
        polling loop is shared with create_session's post-fork settle
        so both paths use identical settle semantics.

        Settle decision returned in InteractionResult:
          - ``settled`` (settled_cleanly=True) — burst-then-idle shape
            observed; the program responded and went idle.
          - ``process_exited`` — child exited during the wait.
          - ``no_new_output`` — deadline reached with zero bytes
            received. Likely a true hang or input consumed silently.
          - ``timeout`` — deadline reached with bytes received but
            never went idle long enough.
        """
        poll = await self._poll_settle(
            session,
            start_total=start_total,
            settle_ms=settle_ms,
            timeout_ms=timeout_ms,
        )
        output = self._drain_buffer(session)

        if poll["process_exited"]:
            self._record_history(session, output)
            return InteractionResult(
                output=output,
                status="process_exited",
                exit_code=session._exit_code,
                prompt_detected=False,
                total_bytes_received=poll["total_bytes"],
                peak_rate_bps=poll["peak_rate_bps"],
                idle_duration_s=poll["idle_duration_s"],
                settled_cleanly=False,
                screen_text=self._render_screen(session),
            )

        if poll["settled"]:
            child_running = self.is_interactive_child_running(session.session_id)
            prompt_found = _detect_prompt(output, session.expected_prompt)
            self._record_history(session, output)
            return InteractionResult(
                output=output,
                status="settled",
                interactive_child_running=child_running,
                prompt_detected=prompt_found,
                total_bytes_received=poll["total_bytes"],
                peak_rate_bps=poll["peak_rate_bps"],
                idle_duration_s=poll["idle_duration_s"],
                settled_cleanly=True,
                screen_text=self._render_screen(session),
            )

        # ── Hard timeout path ────────────────────────────────────
        if session._exited:
            status = "process_exited"
        elif poll["total_bytes"] == 0:
            status = "no_new_output"
        else:
            status = "timeout"

        # Angle-5 logging: surface diagnostic detail when a settle
        # times out without bytes arriving. Default verbosity is
        # debug so this is silent in normal operation; turning on
        # debug logging produces a trail across the residual zero-
        # byte cases that's impossible to reconstruct from
        # InteractionResult alone (peak_rate, idle_duration, child
        # alive at timeout). Future zero-byte regressions will be
        # diagnosable directly from logs.
        if status == "no_new_output":
            reader = session._reader_thread
            reader_alive = bool(reader and reader.is_alive())
            # child_state is the decisive instrument: at a zero-byte timeout it
            # `ps`-inspects the child. comm=python ⇒ the fork never exec'd bash
            # (fork-in-multithreaded-process hazard) — a mute session born dead.
            # comm=bash ⇒ bash alive but silent (different cause). (The old
            # tcgetpgrp-based "child_alive" was unreliable — it returns 0 here,
            # which compares != shell_pgid and always read as alive.)
            child_state = _diagnose_child(session.pid)
            logger.warning(
                "PTY settle timeout: no_new_output session=%s pid=%d "
                "reader_alive=%s child=[%s] total_bytes=%d idle_s=%.3f "
                "timeout_ms=%d last_write=%d/%d(partials=%d)",
                session.session_id,
                session.pid,
                reader_alive,
                child_state,
                poll["total_bytes"],
                poll["idle_duration_s"],
                timeout_ms,
                session.last_write_intended,
                session.last_write_intended,
                session.last_write_partials,
            )

        child_running = not session._exited and self.is_interactive_child_running(
            session.session_id
        )
        prompt_found = (
            _detect_prompt(output, session.expected_prompt) if output else False
        )
        self._record_history(session, output)
        return InteractionResult(
            output=output,
            status=status,
            exit_code=session._exit_code if session._exited else None,
            interactive_child_running=child_running,
            prompt_detected=prompt_found,
            total_bytes_received=poll["total_bytes"],
            peak_rate_bps=poll["peak_rate_bps"],
            idle_duration_s=poll["idle_duration_s"],
            settled_cleanly=False,
            screen_text=self._render_screen(session),
        )

    def _drain_buffer(self, session: SessionInfo) -> str:
        """Drain the output buffer, decode, strip ANSI, return clean text."""
        # Snapshot+clear under the lock so the reader thread can't extend or
        # truncate mid-drain (it appends under the same lock).
        with session._buffer_lock:
            if not session._output_buffer:
                return ""
            raw = bytes(session._output_buffer)
            session._output_buffer.clear()
        text = raw.decode("utf-8", errors="replace")
        clean = strip_ansi(text)
        # The slave tty runs in cooked output mode (OPOST|ONLCR), so newlines
        # arrive as \r\n and lone \r is a carriage return. Normalize to \n for
        # clean capture. (The hand-rolled predecessor used a raw slave, so it
        # never saw \r; pyte will model \r as a real cursor move in phase 2.)
        clean = clean.replace("\r\n", "\n").replace("\r", "\n")

        # Truncate if needed
        if len(clean) > MAX_OUTPUT_CHARS:
            head = clean[: MAX_OUTPUT_CHARS // 2]
            tail = clean[-MAX_OUTPUT_CHARS // 2 :]
            clean = (
                head
                + f"\n\n[... truncated {len(clean) - MAX_OUTPUT_CHARS} chars ...]\n\n"
                + tail
            )

        return clean.strip()

    def _record_history(self, session: SessionInfo, output: str) -> None:
        """Record an interaction in session history."""
        session.history.append(
            {
                "turn": session.turn_count,
                "output": output[:2000] if output else "",
            }
        )
