"""Tier batch runner — serial model arms, per-arm blind staging, live control.

    ouroboros.py tier run --models glm-4.7-flash,gemma-4-26b-a4b
    ouroboros.py tier status
    ouroboros.py tier pause         # park the arm mid-flight, free the machine
    ouroboros.py tier resume        # pick the same arm back up where it stopped
    ouroboros.py tier stop          # finish the current arm, then end the chain
    ouroboros.py tier skip          # abandon the current arm, continue to the next
    ouroboros.py tier force-stop    # abandon the current arm AND end the chain

Replaces dev/tier_batch.sh, which proved the shape on 2026-07-29 and cost two
arms teaching it. Every rule below is one of those lessons.

── THE SERVER COMES DOWN AFTER RUNS ─────────────────────────────────
Default, not an option you remember to pass. A tier batch is a scheduled block
of work, not a service window: when the chain ends nobody is waiting on
inference, and leaving ~75-98 GB wired for nothing is the wrong resting state.
`--leave-server-up` opts out for the case where a batch is feeding something
else. `active_config` is always restored regardless, so the next manual boot
lands on production rather than whichever arm ran last.

── STOPPING IS THREE VERBS, NOT ONE ─────────────────────────────────
The shell version had a single SKIP sentinel and it was not enough. Abandoning
a bad arm, ending the chain after the current arm finishes, and getting out NOW
are different intentions with different costs:

    skip        this arm is a write-off; stage whatever exists, go to the next
    stop        this arm is fine; let it reach its backstop, then end the chain
    force-stop  end the arm and the chain immediately

`stop` is the one the shell version lacked, and the one most often wanted — on
2026-07-29 ending the chain after a good arm meant killing the script mid-flight
and cleaning up its orphans by hand.

── PAUSE IS THE INTERLEAVING VERB, AND IT IS THE SAFE ONE ───────────
A 16-arm batch is ~36 hours; experimental work does not wait that long. `pause`
parks the CURRENT arm mid-flight, releases the machine, and records enough to
pick that same arm back up later — so a batch can share a workstation with
whatever else needs it.

It rides the mission pause already in the loop, and that choice is what makes it
safe rather than merely convenient: `mission pause` pushes an event and
agent/loop.py drains CLEANLY at the next cycle boundary. Nothing is cancelled
mid-generation, so pause cannot reproduce the abandoned-generation block that
`skip` caused on 2026-07-29. **Prefer pause over skip whenever the arm is worth
keeping** — skip exists for write-offs.

Resuming re-boots that arm's model, `mission resume`s it, and gives it only the
wall clock it had left: an arm paused 40 minutes into a 2h backstop resumes with
80 minutes, not a fresh two hours. Without that the per-arm budget would inflate
by one full backstop every pause, and arms would stop being comparable.

── ABANDONING AN ARM DOES NOT FREE THE SERVER ───────────────────────
Cancelling the agent leaves the generation decoding server-side, and uvicorn's
graceful shutdown then WAITS on it — `Waiting for background tasks to complete`.
On 2026-07-29 a 70k-token unbounded generation blocked shutdown past the 900s
stop timeout and the next arm never booted. So `_stop_server` escalates: SIGTERM,
then SIGKILL after `KILL_AFTER_S`. The escalation is deliberate and was ratified
after a hard kill left the next model booting clean 91 seconds later. Bounded
generations do not need it — arms that reach their backstop naturally hand the
server over in about a second.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent.mission_config import parse_duration

ROOT = Path(__file__).resolve().parents[2]
RUNS = Path.home() / "ouroboros-runs"
ENDPOINT = "http://localhost:8008/graphql"
PRODUCTION_CONFIG = "gpt-oss-120b-a5-swarm-524k"

RUBRIC = ROOT / "dev/blind_panel/TIER_RUBRIC_v1.md"


def _rubric_version() -> str:
    """The instrument version, READ FROM THE INSTRUMENT.

    This was hardcoded `TIER_RUBRIC_v1.0` and kept saying so after the rubric
    became v1.1, which is the worst way for a provenance label to fail: the
    record confidently names a version the arm was not scored under. Deriving it
    means the label cannot drift from the file, and a missing rubric says so
    rather than asserting a version."""
    try:
        head = RUBRIC.read_text().lstrip().splitlines()[0]
    except (OSError, IndexError):
        return "TIER_RUBRIC(unreadable)"
    m = re.search(r"\bv(\d+\.\d+)", head)
    return f"TIER_RUBRIC_v{m.group(1)}" if m else "TIER_RUBRIC(unversioned)"


BOOT_TIMEOUT_S = 1200  # step-3.7/hy3-class weights load slowly
TERM_WAIT_S = 240  # polite window before escalating
KILL_AFTER_S = 240
POLL_S = 30

# One event, not one line. The shell version grepped `degenerat|long-cycle|
# repetition guard` and reported 5 for a single abort, because one event writes
# several matching lines. Anchor on the abort sentence alone.
DEGEN_MARKER = "aborted the generation as degenerate"

CONTROL_WORDS = ("skip", "stop", "force-stop", "pause")
# Verbs that end the CHAIN, not merely the current arm.
CHAIN_ENDING = ("stop", "force-stop", "pause")


@dataclass
class ArmResult:
    config: str
    status: str  # completed | skipped | unsupported | create_failed
    minutes: int = 0
    files: int = 0
    py_ok: int = 0
    py_fail: int = 0
    goals: str = ""
    degenerations: int = 0
    staged: Optional[str] = None
    detail: str = ""


@dataclass
class TierRun:
    arms: list[str]
    mission: str = "game_challenge_tier"
    wall: str = "2h"
    top_phase: str = "quality"
    budget_h: float = 11.0
    leave_server_up: bool = False
    base: Path = field(default_factory=Path)
    results: list[ArmResult] = field(default_factory=list)
    # Set when this run continues a paused one: {"config", "work", "consumed_s"}.
    # The first arm is then RESUMED (mission resume, remaining wall) rather than
    # created, and its working directory must survive.
    resume_from: Optional[dict] = None

    # ── plumbing ──────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(self.base / "batch.log", "a") as fh:
            fh.write(line + "\n")

    def _write_state(self, **kw) -> None:
        state = {
            "pid": os.getpid(),
            "base": str(self.base),
            "arms": self.arms,
            "mission": self.mission,
            "wall": self.wall,
            **kw,
        }
        tmp = self.base / "STATE.json.tmp"
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.base / "STATE.json")

    def _take_control(self) -> Optional[str]:
        """Read and CONSUME a control word. Consuming matters: a `skip` left in
        place would skip every subsequent arm too."""
        ctl = self.base / "CONTROL"
        if not ctl.exists():
            return None
        word = ctl.read_text().strip().lower()
        ctl.unlink(missing_ok=True)
        return word if word in CONTROL_WORDS else None

    # ── server lifecycle ──────────────────────────────────────────
    @staticmethod
    def _server_pids() -> list[int]:
        out = subprocess.run(
            ["pgrep", "-f", "api/main.py"], capture_output=True, text=True
        ).stdout.split()
        return [int(p) for p in out if p.isdigit()]

    @staticmethod
    def _healthy() -> bool:
        try:
            import urllib.request

            req = urllib.request.Request(
                ENDPOINT,
                data=b'{"query":"{ health { status } }"}',
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return '"ok"' in r.read().decode()
        except Exception:  # noqa: BLE001 — unreachable is simply not-healthy
            return False

    def _stop_server(self) -> bool:
        pids = self._server_pids()
        if not pids:
            return True
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.time() + TERM_WAIT_S
        while time.time() < deadline:
            if not self._server_pids():
                return True
            time.sleep(1)
        # See the module docstring: an abandoned generation blocks uvicorn's
        # graceful shutdown indefinitely. Escalation is the ratified answer.
        self.log(
            f"  server ignored SIGTERM for {TERM_WAIT_S}s "
            f"(abandoned-generation block) — escalating to SIGKILL"
        )
        for p in self._server_pids():
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(3)
        return not self._server_pids()

    def _boot_server(self, config: str, slog: Path) -> bool:
        (ROOT / "llmvp" / "active_config.txt").write_text(config)
        with open(slog, "w") as fh:
            subprocess.Popen(
                [str(ROOT / "llmvp" / ".venv" / "bin" / "python"), "api/main.py"],
                cwd=ROOT / "llmvp",
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.time() + BOOT_TIMEOUT_S
        while time.time() < deadline:
            if self._healthy():
                return True
            if slog.exists() and "Startup failed" in slog.read_text(errors="ignore"):
                return False
            time.sleep(10)
        return False

    # ── artifact accounting ───────────────────────────────────────
    @staticmethod
    def _authored(work: Path) -> list[Path]:
        """Authored files only. The first tier run reported 622 files for one
        model and 80 for another — a 7x 'productivity gap' that was .venv,
        __pycache__ and .ruff_cache. Real counts were 9 and 11."""
        skip = {".agent", ".venv", "__pycache__", ".ruff_cache", ".git"}
        return [
            p
            for p in work.rglob("*")
            if p.is_file()
            and not skip & set(p.relative_to(work).parts)
            and p.name != "OUTCOME"
        ]

    def _stage(self, work: Path, idx: int, config: str) -> Optional[str]:
        """Strip, blind-scan, and copy somewhere /tmp cannot eat — immediately,
        so judging can begin while the next arm boots. The directory is an index,
        never the model name, and the map lives outside the staged tree."""
        if not self._authored(work):
            self.log("  no authored files — nothing to stage (tier 3 candidate)")
            self._manifest("-", config, "NO_ARTIFACT")
            return None
        dest = self.base / "staged" / f"arm{idx:02d}"
        slog = self.base / f"stage_{config}.log"
        with open(slog, "w") as fh:
            rc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "dev/blind_panel/stage.py"),
                    "--judges",
                    "1",
                    "--out",
                    str(dest),
                    str(work),
                ],
                stdout=fh,
                stderr=subprocess.STDOUT,
            ).returncode
        if rc != 0:
            self.log(f"  STAGE FAILED — see {slog}")
            self._manifest("-", config, "STAGE_FAILED")
            return None
        (dest / "KEY.json").unlink(
            missing_ok=True
        )  # redundant, and it names the config
        # MODEL-name leaks block a judgement; framework names do not. hy3's arm
        # was manifested `staged_with_leaks` on one hit — `main.py:16` containing
        # 'Ouroboros', the game's own title screen byline — which is present in
        # every arm and identifies none of them. Matching stage.py's own heading
        # keeps one taxonomy: if that heading changes, this must too, so it is
        # asserted rather than assumed.
        stage_out = slog.read_text(errors="ignore")
        leaked = "MODEL-NAME LEAKS" in stage_out
        advisory = "advisory (framework/judge names" in stage_out
        if not (leaked or advisory or "identifier scan" in stage_out):
            self.log(
                "  !! stage.py produced no identifier-scan verdict — "
                "treating as UNBLINDED (its output format changed?)"
            )
            leaked = True
        note = (
            "  !! MODEL-NAME LEAK — DO NOT JUDGE"
            if leaked
            else (
                "  (no model names; framework byline only)"
                if advisory
                else "  (scan clean)"
            )
        )
        self.log(f"  staged -> {dest}{note}")
        self._manifest(
            str(dest), config, "BLOCKED_model_name_leak" if leaked else "staged"
        )
        return str(dest)

    def _manifest(self, dest: str, config: str, status: str) -> None:
        with open(self.base / "MANIFEST.txt", "a") as fh:
            fh.write(f"{dest}  {config}  {status}\n")

    # ── one arm ───────────────────────────────────────────────────
    def _remaining_wall_s(self, consumed_s: float) -> float:
        """Wall clock this arm has LEFT, never a fresh backstop.

        A resumed arm that got the full window again would inflate the per-arm
        budget by one backstop per pause, and arms tiered against a 2h stage
        would no longer be comparable — an arm paused three times would have had
        eight hours. Floored at 60s so a resume is never a no-op."""
        return max(60.0, parse_duration(self.wall) - consumed_s)

    def _pause_arm(self, work: Path, proc: subprocess.Popen, started: float) -> None:
        """Park the arm via the mission's own pause, then wait for it to drain.

        NOT proc.terminate(). `mission pause` pushes an event and agent/loop.py
        drains at the next cycle boundary, so no generation is abandoned — which
        is precisely what makes pause safe where skip is not."""
        mins = int((time.time() - started) / 60)
        self.log(f"  pause at {mins}min — pushing mission pause, draining cleanly")
        subprocess.run(
            [
                sys.executable,
                "ouroboros.py",
                "mission",
                "pause",
                "--working-dir",
                str(work),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        # A cycle can be long; the drain is worth waiting for, because the
        # alternative is the abandoned-generation block.
        deadline = time.time() + 1800
        while proc.poll() is None and time.time() < deadline:
            time.sleep(5)
        if proc.poll() is None:
            self.log("  drain exceeded 30min — terminating (arm still resumable)")
            proc.terminate()
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _run_arm(
        self, idx: int, config: str, resume: Optional[dict] = None
    ) -> tuple[ArmResult, Optional[str]]:
        work = Path("/tmp/tier") / config
        if resume is None:
            subprocess.run(["rm", "-rf", str(work)])
            work.mkdir(parents=True, exist_ok=True)
        elif not (work / ".agent").is_dir():
            # /tmp is not durable. If the workspace is gone the arm cannot be
            # resumed, and silently starting it over would mis-report a
            # part-consumed backstop as a full one.
            self.log(f"  CANNOT RESUME — {work}/.agent is gone (/tmp was cleared)")
            return (
                ArmResult(
                    config,
                    "resume_lost",
                    detail="working dir vanished; re-run this arm fresh",
                ),
                None,
            )
        slog, rlog = self.base / f"{config}_server.log", self.base / f"{config}_run.log"
        self.log(f"\n─── ARM {idx}/{len(self.arms)}: {config} ───")

        if not self._stop_server():
            return ArmResult(config, "skipped", detail="server would not stop"), None
        if not self._boot_server(config, slog):
            txt = slog.read_text(errors="ignore") if slog.exists() else ""
            why = next(
                (
                    ln
                    for ln in txt.splitlines()
                    if "REFUSED" in ln or "Startup failed" in ln
                ),
                "boot timeout",
            )
            self.log(f"  UNSUPPORTED — {why[:120]}")
            self._manifest("-", config, "UNSUPPORTED")
            return ArmResult(config, "unsupported", detail=why[:200]), None
        if "Could not load static tokens" in slog.read_text(errors="ignore"):
            self.log("  !! static tokens FAILED — no system block this arm")
        self.log("  server up")

        consumed = float(resume["consumed_s"]) if resume else 0.0
        if resume is None:
            create = subprocess.run(
                [
                    sys.executable,
                    "ouroboros.py",
                    "mission",
                    "create",
                    "--mission_config",
                    self.mission,
                    "--working-dir",
                    str(work),
                    "--top-phase",
                    self.top_phase,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if create.returncode != 0:
                self.log("  TIER 3 (create) — mission create failed")
                self._manifest("-", config, "TIER3_CREATE")
                return (
                    ArmResult(config, "create_failed", detail=create.stderr[-300:]),
                    None,
                )
        else:
            subprocess.run(
                [
                    sys.executable,
                    "ouroboros.py",
                    "mission",
                    "resume",
                    "--working-dir",
                    str(work),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )

        remaining = self._remaining_wall_s(consumed)
        if resume:
            self.log(
                f"  resuming at {int(consumed/60)}min consumed — "
                f"{int(remaining/60)}min of the {self.wall} backstop left"
            )
        else:
            self.log("  mission running")

        env = {**os.environ, "OURO_LLMVP": ENDPOINT}
        started = time.time() - consumed  # so elapsed reads as total arm time
        with open(rlog, "a" if resume else "w") as fh:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "ouroboros.py",
                    "start",
                    "--working-dir",
                    str(work),
                    "--max-wall-clock",
                    str(int(remaining)),
                    "--trace-thinking",
                ],
                cwd=ROOT,
                env=env,
                stdout=fh,
                stderr=subprocess.STDOUT,
            )

        control = None
        while proc.poll() is None:
            self._heartbeat(idx, config, work, slog, rlog, started)
            word = self._take_control()
            if word:
                control = word
                if word == "pause":
                    self._pause_arm(work, proc, started)
                    break
                if word in ("skip", "force-stop"):
                    mins = int((time.time() - started) / 60)
                    self.log(f"  {word} at {mins}min — ending this arm")
                    proc.terminate()
                    try:
                        proc.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                self.log("  stop requested — letting this arm finish first")
            time.sleep(POLL_S)
        proc.wait()

        elapsed_s = time.time() - started
        if control == "pause":
            # A paused arm is NOT staged: it is unfinished on purpose and will
            # be judged only once it has spent its full backstop.
            self._paused_state = {
                "config": config,
                "work": str(work),
                "consumed_s": elapsed_s,
                "index": idx,
            }
            return (
                ArmResult(
                    config, "paused", int(elapsed_s / 60), len(self._authored(work))
                ),
                control,
            )

        res = self._tally(config, work, slog, int(elapsed_s / 60))
        res.status = "skipped" if control in ("skip", "force-stop") else "completed"
        self.log(
            f"  done {res.minutes}min files={res.files} "
            f"py_ok={res.py_ok} py_fail={res.py_fail} degen={res.degenerations} "
            f"| {res.goals}"
        )
        res.staged = self._stage(work, idx, config)
        agent_dir = work / ".agent"
        if agent_dir.is_dir():  # telemetry for the OBSERVED half of the record
            subprocess.run(
                ["cp", "-a", str(agent_dir), str(self.base / f"{config}_agent")]
            )
        return res, control

    def _tally(self, config: str, work: Path, slog: Path, minutes: int) -> ArmResult:
        files = self._authored(work)
        ok = bad = 0
        for p in (f for f in files if f.suffix == ".py"):
            try:
                compile(p.read_text(errors="ignore"), str(p), "exec")
                ok += 1
            except SyntaxError:
                bad += 1
        goals = ""
        try:
            st = subprocess.run(
                [
                    sys.executable,
                    "ouroboros.py",
                    "mission",
                    "status",
                    "--working-dir",
                    str(work),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=90,
            )
            goals = " ".join(
                ln.strip()
                for ln in st.stdout.splitlines()
                if "Status:" in ln or "Goals (" in ln
            )
        except Exception:  # noqa: BLE001 — a status hang must not lose the arm
            goals = "(status unavailable)"
        degen = (
            slog.read_text(errors="ignore").count(DEGEN_MARKER) if slog.exists() else 0
        )
        return ArmResult(
            config, "completed", minutes, len(files), ok, bad, goals, degen
        )

    def _heartbeat(
        self, idx: int, config: str, work: Path, slog: Path, rlog: Path, started: float
    ) -> None:
        files = self._authored(work)
        degen = (
            slog.read_text(errors="ignore").count(DEGEN_MARKER) if slog.exists() else 0
        )
        tail = ""
        if rlog.exists():
            tail = "\n".join(rlog.read_text(errors="ignore").splitlines()[-5:])
        body = (
            f"arm            : {idx}/{len(self.arms)}  {config}\n"
            f"elapsed_min    : {int((time.time()-started)/60)}   (backstop {self.wall})\n"
            f"authored_files : {len(files)}  "
            f"(py {sum(1 for f in files if f.suffix == '.py')})\n"
            f"degenerations  : {degen}   (events, not log lines)\n"
            f"server_healthy : {self._healthy()}\n"
            f"control        : ouroboros.py tier skip | stop | force-stop\n"
            f"--- run.log tail ---\n{tail}\n"
        )
        (self.base / "HEARTBEAT.txt").write_text(body)
        self._write_state(
            current_arm=config,
            index=idx,
            elapsed_min=int((time.time() - started) / 60),
            files=len(files),
            degenerations=degen,
        )

    # ── the chain ─────────────────────────────────────────────────
    def execute(self) -> int:
        self.base.mkdir(parents=True, exist_ok=True)
        (self.base / "staged").mkdir(exist_ok=True)
        (self.base / "MANIFEST.txt").touch()
        self.log(
            f"=== tier batch · {len(self.arms)} arms · {self.wall} each "
            f"· budget {self.budget_h}h ==="
        )
        self.log(
            f"    mission={self.mission} rubric={_rubric_version()} "
            f"base={self.base}"
        )
        t0 = time.time()

        for idx, config in enumerate(self.arms, 1):
            if (time.time() - t0) / 3600 >= self.budget_h:
                self.log(
                    f"=== BUDGET REACHED — not starting {config}. "
                    f"Remaining: {', '.join(self.arms[idx-1:])}"
                )
                self._remaining = self.arms[idx - 1 :]
                break
            word = self._take_control()
            if word in CHAIN_ENDING:
                self.log(f"=== {word} before {config} — ending the chain ===")
                self._remaining = self.arms[idx - 1 :]
                break
            # Only the FIRST arm of a resumed run continues a paused mission;
            # everything after it starts fresh.
            resume, self.resume_from = self.resume_from, None
            res, control = self._run_arm(idx, config, resume=resume)
            self.results.append(res)
            if control in CHAIN_ENDING:
                self.log(f"=== {control} — ending the chain ===")
                # A pause keeps the CURRENT arm at the head of the queue so it
                # is the one resumed; stop/force-stop drop it.
                self._remaining = (
                    self.arms[idx - 1 :] if control == "pause" else self.arms[idx:]
                )
                break

        self._finish()
        return 0

    def _finish(self) -> None:
        paused = getattr(self, "_paused_state", None)
        self.log("\n=== pausing ===" if paused else "\n=== ending ===")
        self._stop_server()
        (ROOT / "llmvp" / "active_config.txt").write_text(PRODUCTION_CONFIG)
        # A pause exists to free the machine for other work, so the server comes
        # down regardless of --leave-server-up.
        if self.leave_server_up and not paused:
            slog = self.base / "restore_server.log"
            if self._boot_server(PRODUCTION_CONFIG, slog):
                self.log(f"restored {PRODUCTION_CONFIG} (server UP, --leave-server-up)")
            else:
                self.log(f"WARN {PRODUCTION_CONFIG} did not come back up")
        else:
            self.log(f"server DOWN; active_config restored to {PRODUCTION_CONFIG}")

        self.log("\n=== SUMMARY ===")
        for r in self.results:
            self.log(
                f"  {r.config:<30} {r.status:<12} {r.minutes:>3}min "
                f"files={r.files:<3} py_fail={r.py_fail} degen={r.degenerations} "
                f"{'-> ' + r.staged if r.staged else ''}"
            )
        self.log(
            f"\nmanifest: {self.base / 'MANIFEST.txt'}  "
            f"(the map lives HERE, never inside staged/)"
        )

        remaining = getattr(self, "_remaining", [])
        if paused:
            left = int((parse_duration(self.wall) - paused["consumed_s"]) / 60)
            self.log(
                f"\nPAUSED on {paused['config']} after "
                f"{int(paused['consumed_s']/60)}min ({left}min of backstop left)"
            )
            self.log(f"queue: {', '.join(remaining) or '(none)'}")
            self.log("resume with:  ouroboros.py tier resume")
            # finished stays FALSE so `tier resume` can find this run, but the
            # pid is dead — _active_run() checks liveness, so a paused run is
            # correctly neither active nor finished.
            self._write_state(
                current_arm=None,
                finished=False,
                paused=True,
                paused_arm=paused,
                remaining_arms=remaining,
                results=[r.__dict__ for r in self.results],
            )
        else:
            if remaining:
                self.log(f"\nnot run: {', '.join(remaining)}")
            self._write_state(
                current_arm=None,
                finished=True,
                paused=False,
                remaining_arms=remaining,
                results=[r.__dict__ for r in self.results],
            )
