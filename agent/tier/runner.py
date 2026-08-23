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

import dataclasses
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

# ── League run protocol (TIER_RUBRIC v2 §2, epoch v2.0) ──────────────
# Contemplators (<20 cyc/h measured) get a HARD 30-work-cycle cap — the
# budget is the model's central-tendency work allotment, not its clock —
# plus a 4h SAFETY wall that never binds a healthy arm (jam protection:
# gemma-4-31b has jammed a generation indefinitely). Grinders keep the
# stage wall. League lives in the model config's doc-only `tier:` block;
# absent/unreadable defaults to grinder, which preserves pre-league
# behaviour for every existing config.
CONTEMPLATOR_CYCLES = 30
CONTEMPLATOR_SAFETY_WALL = "4h"

# Every arm's workspace. Keyed by LABEL alone, with nothing tying it to a
# run — which is precisely why `tier extend` must verify mission identity
# before resuming one (see snapshot_mission_id).
TIER_WORK_ROOT = Path("/tmp/tier")


def config_league(config: str) -> str:
    """Read tier.league from the model's llmvp config (doc-only namespace)."""
    import yaml as _yaml

    for sub in ("", "boss", "experiments"):
        p = ROOT / "llmvp" / "configs" / sub / f"{config}.yaml"
        if not p.is_file():
            continue
        try:
            doc = _yaml.safe_load(p.read_text()) or {}
            league = str((doc.get("tier") or {}).get("league", "") or "")
            return (
                league if league in ("contemplator", "grinder", "both") else "grinder"
            )
        except Exception:  # noqa: BLE001 — unreadable config = default league
            return "grinder"
    return "grinder"


def arm_league(arm: str) -> tuple[str, str]:
    """(bare_config, league) for an arm label.

    `name[c]` / `name[g]` are the explicit labels the CLI expands a
    league:both model into (two arms, two workspaces, one config); a bare
    name resolves through its config.
    """
    if arm.endswith("[c]"):
        return arm[:-3], "contemplator"
    if arm.endswith("[g]"):
        return arm[:-3], "grinder"
    league = config_league(arm)
    return arm, ("grinder" if league == "both" else league)


def _mission_doc(work: Path) -> dict:
    try:
        return json.loads((work / ".agent" / "mission.json").read_text())
    except Exception:  # noqa: BLE001 — fresh arm / unreadable
        return {}


def cycles_consumed(work: Path) -> int:
    """Lifetime work cycles a parked arm already used (mission.json)."""
    return max(0, int(_mission_doc(work).get("cycles_consumed", 0) or 0))


def mission_state(work: Path) -> tuple[str, int]:
    """(status, cycles_consumed) for a workspace; ("", 0) if unreadable."""
    doc = _mission_doc(work)
    return str(doc.get("status", "") or ""), max(
        0, int(doc.get("cycles_consumed", 0) or 0)
    )


def mission_id(work: Path) -> str:
    """The mission this workspace currently holds; "" if unreadable."""
    doc = _mission_doc(work)
    return str(doc.get("mission_id") or doc.get("id") or "")


def snapshot_mission_id(base: Path, label: str) -> str:
    """The mission an arm held AT ARM END, from the run-local `.agent` copy.

    The identity half of the workspace-reuse guard. `/tmp/tier/<label>` is
    keyed by label alone with nothing tying it to a run, so a later batch
    running the same model overwrites it — verified 2026-08-05, where both
    arms of tier_20260803-151411 had been replaced by the 08-04 re-runs.
    Comparing this against mission_id(work) is what stops an extend from
    resuming a stranger's mission into a cited artifact slot.

    NOTE the path shape: `cp -a <work>/.agent <base>/<label>_agent` copies
    the CONTENTS of .agent, so mission.json sits directly under
    `<label>_agent/` — not under a nested `.agent/`. Reading it through
    mission_id() (which appends `.agent`) silently returns "", and an
    empty id is falsy, so the reuse check would skip and every stale
    workspace would read as eligible. The guard would have been decorative.
    """
    try:
        doc = json.loads((base / f"{label}_agent" / "mission.json").read_text())
    except Exception:  # noqa: BLE001 — snapshot predates this change
        return ""
    return str(doc.get("mission_id") or doc.get("id") or "")


def manifest_rows(base: Path) -> list[tuple[str, str, str]]:
    """(dest, config, status) per MANIFEST.txt row, in file order."""
    out: list[tuple[str, str, str]] = []
    try:
        text = (base / "MANIFEST.txt").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) == 3:
            out.append((parts[0], parts[1], parts[2]))
    return out


def arm_slot(base: Path, label: str) -> Optional[int]:
    """The staged slot this arm last wrote, or None if it never staged."""
    found = None
    for dest, config, _status in manifest_rows(base):
        if config != label:
            continue
        m = re.search(r"arm(\d+)$", dest)
        if m:
            found = int(m.group(1))
    return found


def next_free_slot(base: Path) -> int:
    """Lowest slot number no arm has claimed."""
    used = set()
    for dest, _config, _status in manifest_rows(base):
        m = re.search(r"arm(\d+)$", dest)
        if m:
            used.add(int(m.group(1)))
    staged = base / "staged"
    if staged.is_dir():
        for child in staged.iterdir():
            m = re.fullmatch(r"arm(\d+)", child.name)
            if m:
                used.add(int(m.group(1)))
    return max(used) + 1 if used else 1


def run_epoch(base: Path) -> str:
    """The rubric MAJOR version a batch ran under, e.g. "v1" / "v2".

    The brief changes with the epoch, and LADDER.md is explicit that
    cross-epoch comparisons are never valid. A batch records its rubric in
    batch.log; major version is the right granularity because v2 -> v2.1
    was a rubric refinement on an unchanged brief.
    """
    try:
        text = (base / "batch.log").read_text(errors="ignore")
    except OSError:
        return ""
    m = re.search(r"rubric=TIER_RUBRIC_?v(\d+)", text)
    return f"v{m.group(1)}" if m else ""


def extend_candidates(base: Path) -> list[dict]:
    """Every arm of a finished batch, with a verdict on whether it can extend.

    Returns a verdict for ALL arms, not just eligible ones — when nothing is
    extendable the reason per arm IS the product, and "no candidates" alone
    sends you reading logs.

    Ladder order is load-bearing:

      no_config        BEFORE grinder — config_league() defaults a missing
                       config to grinder, so a renamed/deleted config would
                       otherwise report as a league refusal and mislead.
      workspace_reused BEFORE the status/cycle checks — a reused workspace's
                       numbers belong to a DIFFERENT mission, and reporting
                       them as this arm's is how the wrong mission gets
                       extended into a cited artifact slot.
    """
    out: list[dict] = []
    try:
        state = json.loads((base / "STATE.json").read_text())
    except Exception:  # noqa: BLE001 — unreadable run
        return out

    epoch, today = run_epoch(base), _rubric_epoch_today()
    for label in state.get("arms") or []:
        if not isinstance(label, str):
            continue
        cfg, league = arm_league(label)
        work = TIER_WORK_ROOT / label
        row: dict = {
            "arm": label,
            "config": cfg,
            "league": league,
            "slot": arm_slot(base, label),
            "cycles": 0,
            "status": "",
            "verdict": "",
        }
        prior = next(
            (r for r in (state.get("results") or []) if r.get("config") == label),
            {},
        )
        row["minutes"] = int(prior.get("minutes", 0) or 0)

        if not any(
            (ROOT / "llmvp" / "configs" / sub / f"{cfg}.yaml").is_file()
            for sub in ("", "boss", "experiments")
        ):
            row["verdict"] = "no_config"
        elif league != "contemplator":
            row["verdict"] = "grinder"
        elif not (work / ".agent").is_dir():
            row["verdict"] = "workspace_gone"
        elif epoch and today and epoch != today:
            # Cross-epoch: the brief itself differs, so extending would
            # splice this epoch's cycles onto a previous epoch's artifact.
            # LADDER.md: "Historical comparisons are never" valid.
            row["verdict"] = "foreign_epoch"
            row["epoch"] = epoch
        else:
            snap, live = snapshot_mission_id(base, label), mission_id(work)
            status, cycles = mission_state(work)
            row["status"], row["cycles"] = status, cycles
            if snap and live and snap != live:
                row["verdict"] = "workspace_reused"
                row["snapshot_id"], row["live_id"] = snap, live
            elif status != "paused":
                row["verdict"] = f"mission_{status or 'unknown'}"
            elif "cycles_consumed" not in _mission_doc(work):
                # The field postdates these missions, so it reads 0 and
                # `30 - 0` grants a FULL fresh budget while presenting as a
                # top-up. An unknown is not a zero.
                row["verdict"] = "no_cycle_record"
            elif cycles >= CONTEMPLATOR_CYCLES:
                row["verdict"] = "budget_spent"
            else:
                row["verdict"] = "eligible"
        out.append(row)
    return out


def _rubric_epoch_today() -> str:
    m = re.search(r"v(\d+)", _rubric_version())
    return f"v{m.group(1)}" if m else ""


VERDICT_HELP = {
    "foreign_epoch": "ran under a PREVIOUS brief epoch — cross-epoch "
    "comparisons are never valid, so there is nothing to finish",
    "no_cycle_record": "its mission predates cycles_consumed, so the budget "
    "left is unknown — 30-0 would grant a full fresh run, not a top-up",
    "no_config": "no llmvp config by that name — renamed or removed",
    "grinder": "ran GRINDER; a wall-bound finish is the contract, not a shortfall",
    "workspace_gone": "/tmp workspace was reaped — nothing left to resume",
    "workspace_reused": "workspace now holds a DIFFERENT mission (a later batch "
    "reused it) — extending would resume a stranger",
    "budget_spent": "already spent its full cycle budget",
    "eligible": "",
}


RUNS = Path.home() / "ouroboros-runs"
ENDPOINT = "http://localhost:8008/graphql"
PRODUCTION_CONFIG = "gpt-oss-120b-a5-swarm-524k"

RUBRIC = ROOT / "dev/blind_panel/TIER_RUBRIC_v2.md"


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
    # v2's headline carries no minor number — the minor is optional.
    m = re.search(r"\bv(\d+(?:\.\d+)?)", head)
    return f"TIER_RUBRIC_v{m.group(1)}" if m else "TIER_RUBRIC(unversioned)"


BOOT_TIMEOUT_S = 1200  # step-3.7/hy3-class weights load slowly
TERM_WAIT_S = 240  # polite window before escalating
KILL_AFTER_S = 240
POLL_S = 30
# Independent time-up enforcement. --max-wall-clock is handed to the agent,
# which parks ITSELF at the next cycle boundary — so an arm whose cycle never
# ends is never bounded by it at all. Live 2026-08-21: a PTY session orbited
# to 335 turns with zero goal progress, the cycle never closed, and the runner
# sat 40 MINUTES past the wall waiting for a park that could not come; the arm
# had to be abandoned by hand. The grace is generous on purpose — an honest
# cycle (a long session plus its evaluate turn) must be able to finish and
# park cleanly, and only a genuinely wedged one should be killed.
WALL_GRACE_S = 600

# One event, not one line. The shell version grepped `degenerat|long-cycle|
# repetition guard` and reported 5 for a single abort, because one event writes
# several matching lines. Anchor on the abort sentence alone.
#
# IT IS WRITTEN BY THE AGENT, INTO run.log — not by the server. Both counters
# below read the RUN log for exactly that reason. They read the SERVER log
# until 2026-07-30, which made `degen` a dead signal: every tier arm ever
# recorded degen=0, including one whose long-cycle guard demonstrably fired
# and wrote a runaway capture. A counter that cannot go up reads as "clean".
DEGEN_MARKER = "aborted the generation as degenerate"

CONTROL_WORDS = ("skip", "stop", "force-stop", "pause")
# Verbs that end the CHAIN, not merely the current arm.
CHAIN_ENDING = ("stop", "force-stop", "pause")


@dataclass
class ArmResult:
    config: str
    status: str  # completed | skipped | wall_killed | unsupported | create_failed
    minutes: int = 0
    files: int = 0
    py_ok: int = 0
    py_fail: int = 0
    goals: str = ""
    degenerations: int = 0
    staged: Optional[str] = None
    detail: str = ""
    # The league this arm RAN under and the lifetime cycles it reached.
    # Stored as inputs rather than a derived `short` flag: cyc/h is what
    # drives league placement and it is currently re-derived by hand into
    # YAML comments, so the record should carry the raw numbers. Recorded
    # for grinders too — step37 was a grinder whose cycle count is exactly
    # what revealed the mis-league.
    league: str = ""
    cycles: int = 0


@dataclass
class TierRun:
    arms: list[str]
    mission: str = "game_challenge_tier"
    wall: str = "2h"
    top_phase: str = "quality"
    budget_h: float = 11.0
    leave_server_up: bool = False
    # Per-run OVERRIDES of the contemplator protocol governors (None = use
    # the TIER_RUBRIC v2 §2 defaults above). An arm that sets either is
    # deliberately OFF-PROTOCOL and is not cycle-comparable to arms that
    # ran under the standard 30/4h contract — the batch header says so out
    # loud, and STATE.json records it, so a later reader cannot mistake a
    # long arm for a normal one.
    contemplator_cycles: Optional[int] = None
    contemplator_wall: Optional[str] = None
    base: Path = field(default_factory=Path)
    results: list[ArmResult] = field(default_factory=list)
    # Set when this run continues a paused one: {"config", "work", "consumed_s"}.
    # The first arm is then RESUMED (mission resume, remaining wall) rather than
    # created, and its working directory must survive.
    resume_from: Optional[dict] = None

    @property
    def cyc_cap(self) -> int:
        return self.contemplator_cycles or CONTEMPLATOR_CYCLES

    @property
    def safety_wall(self) -> str:
        return self.contemplator_wall or CONTEMPLATOR_SAFETY_WALL

    # Arms this base has EVER run, adopted from a prior STATE.json on
    # re-entry. `arms` narrows to the remaining queue on a resume, and
    # writing that as the batch's arm list erased the rest of the record.
    _prior_arms: list[str] = field(default_factory=list)
    _adopted: bool = False

    @property
    def full_arms(self) -> list[str]:
        """Every arm this base has run, prior ones first — never narrows."""
        out = list(self._prior_arms)
        out.extend(a for a in self.arms if a not in out)
        return out

    # ── plumbing ──────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(self.base / "batch.log", "a") as fh:
            fh.write(line + "\n")

    # An arm whose re-entry FAILED must not overwrite the good record of its
    # earlier run — STATE.json would then deny an artifact that is still on
    # disk and cited in LADDER.md.
    NON_CLOBBERING = ("resume_lost", "unsupported", "create_failed")

    def _record(self, res: "ArmResult") -> None:
        """One entry per arm label, replacing on re-entry."""
        for i, prior in enumerate(self.results):
            if prior.config != res.config:
                continue
            if res.status in self.NON_CLOBBERING:
                prior.detail = (f"re-entry {res.status}: {res.detail}").strip()[:300]
            else:
                self.results[i] = res
            return
        self.results.append(res)

    def _claim_base(self) -> bool:
        """One worker per base.

        `_active_run()` guards the CLI verbs but NOT the `--_worker` path, and
        a re-entry writes into a directory that may already have a live owner.
        Two workers would interleave STATE.json writes and the loser's arms
        would vanish from the record. Uses the pid the state already carries —
        no new lock file.
        """
        try:
            prior = json.loads((self.base / "STATE.json").read_text())
        except Exception:  # noqa: BLE001 — fresh base
            return True
        pid = prior.get("pid")
        if not isinstance(pid, int) or pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True  # dead owner — the base is ours
        except PermissionError:
            pass  # alive, just not ours to signal — still an owner
        self.log(
            f"!! base {self.base.name} is owned by live worker pid {pid} — refusing"
        )
        return False

    def _adopt_prior_state(self) -> None:
        """Inherit a prior STATE.json so a re-entry adds to the record.

        Without this a re-entering worker starts with `results = []` and its
        first write replaces the batch's entire per-arm record with a
        one-element list. That is live today for `tier resume`.
        """
        try:
            prior = json.loads((self.base / "STATE.json").read_text())
        except Exception:  # noqa: BLE001 — fresh base, nothing to adopt
            return
        known = {f.name for f in dataclasses.fields(ArmResult)}
        adopted: list[ArmResult] = []
        for row in prior.get("results") or []:
            if not isinstance(row, dict):
                continue
            try:
                adopted.append(
                    ArmResult(**{k: v for k, v in row.items() if k in known})
                )
            except Exception:  # noqa: BLE001 — a malformed row is not fatal
                continue
        self.results = adopted
        self._prior_arms = [a for a in (prior.get("arms") or []) if isinstance(a, str)]
        self._adopted = True
        if adopted or self._prior_arms:
            self.log(
                f"    adopted prior record: {len(adopted)} arm result(s), "
                f"{len(self._prior_arms)} arm(s) known to this base"
            )

    def _write_state(self, **kw) -> None:
        """STATE.json is ALWAYS a complete picture, never a partial one.

        It was a full overwrite fed by PARTIAL callers, which cost real
        history two ways. `_heartbeat` omits `results`, so every 30s
        rewrite dropped the per-arm record for the rest of the run — a
        batch that died mid-flight lost every finished arm. And `arms`
        came from `self.arms`, which on a re-entry (`tier resume`, and
        now `tier extend`) is only the REMAINING queue, so the record
        forgot which arms the batch ever contained.

        Fixing the contract here rather than at each call site is
        deliberate: the next caller cannot reintroduce it. Cost is
        serialising <=16 small dicts every 30s.
        """
        state = {
            "pid": os.getpid(),
            "base": str(self.base),
            "arms": self.full_arms,
            "mission": self.mission,
            "wall": self.wall,
            # Recorded even when None so a reader can tell "ran under the
            # protocol" from "no field written by an older build".
            "contemplator_cycles": self.contemplator_cycles,
            "contemplator_wall": self.contemplator_wall,
            "results": [r.__dict__ for r in self.results],
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

    def _stage(self, work: Path, slot: int, config: str) -> Optional[str]:
        """Strip, blind-scan, and copy somewhere /tmp cannot eat — immediately,
        so judging can begin while the next arm boots. The directory is an index,
        never the model name, and the map lives outside the staged tree.

        `slot` comes from _slot_for, NOT the loop index — see its docstring.
        """
        if not self._authored(work):
            self.log("  no authored files — nothing to stage (tier 3 candidate)")
            self._manifest("-", config, "NO_ARTIFACT")
            return None
        dest = self.base / "staged" / f"arm{slot:02d}"
        replacing = dest.is_dir()
        slog = self.base / f"stage_{config}.log"
        with open(slog, "w") as fh:
            rc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "dev/blind_panel/stage.py"),
                    "--judges",
                    "1",
                    # THE ARM CANNOT PASS CARRYING ITS OWN NAME. stage.py's
                    # denylist is maintained by hand and cannot contain a model
                    # nobody has tiered yet — live, an arm shipped
                    # "# Muse Glimmer 30b" as its README title and the scan
                    # called it judgeable. We know the config here; say so.
                    "--arm-identifier",
                    config,
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
        if replacing:
            # stage.py rmtree's the destination, so the prior artifact is
            # already gone by here. Say so in the record: MANIFEST is
            # append-only, and a second `staged` row would read as a second
            # artifact rather than a replacement of a cited one.
            self.log(f"  REPLACED the artifact previously staged at {dest}")
        self._manifest(
            str(dest),
            config,
            (
                "BLOCKED_model_name_leak"
                if leaked
                else ("staged_extended" if replacing else "staged")
            ),
        )
        return str(dest)

    def _manifest(self, dest: str, config: str, status: str) -> None:
        with open(self.base / "MANIFEST.txt", "a") as fh:
            fh.write(f"{dest}  {config}  {status}\n")

    # ── one arm ───────────────────────────────────────────────────
    def _remaining_wall_s(
        self, consumed_s: float, backstop: str | None = None
    ) -> float:
        """Wall clock this arm has LEFT, never a fresh backstop.

        A resumed arm that got the full window again would inflate the per-arm
        budget by one backstop per pause, and arms tiered against a 2h stage
        would no longer be comparable — an arm paused three times would have had
        eight hours. Floored at 60s so a resume is never a no-op.

        ``backstop`` overrides the stage wall — contemplator arms compute
        against their 4h SAFETY wall, not the grinder stage wall."""
        return max(60.0, parse_duration(backstop or self.wall) - consumed_s)

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
        # `config` is the ARM LABEL (may carry a [c]/[g] league suffix for a
        # league:both model); `cfg` is the bare llmvp config the server and
        # mission consume. Workspace/logs/results key by the LABEL so a both-
        # model's two arms stay distinct.
        cfg, league = arm_league(config)
        work = TIER_WORK_ROOT / config
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
        if resume is not None and resume.get("mission_id"):
            # Second enforcement of the workspace-identity guard. The CLI
            # already checked, but discovery and boot are seconds apart and
            # /tmp/tier/<label> is keyed by model name alone — a concurrent
            # batch could claim it in between. Resuming a stranger's mission
            # would stage it over this run's artifact, which is a cited key.
            live = mission_id(work)
            if live and live != resume["mission_id"]:
                self.log(
                    f"  CANNOT RESUME — {work} now holds mission {live}, "
                    f"not {resume['mission_id']} (a later batch reused it)"
                )
                return (
                    ArmResult(
                        config,
                        "resume_lost",
                        detail=(
                            f"workspace reused by mission {live}; "
                            f"re-run this arm fresh"
                        ),
                    ),
                    None,
                )
        slog, rlog = self.base / f"{config}_server.log", self.base / f"{config}_run.log"
        self.log(f"\n─── ARM {idx}/{len(self.arms)}: {config} ───")

        if not self._stop_server():
            return ArmResult(config, "skipped", detail="server would not stop"), None
        if not self._boot_server(cfg, slog):
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
        # Elapsed the RECORD should show, kept separate from the elapsed the
        # BUDGET counts. An extend deliberately gets consumed_s=0 (a fresh
        # safety wall) but must still report total arm time — cyc/h drives
        # league placement, and a number covering only the last leg would
        # inflate the rate of exactly the arms whose rate is in question.
        prior_elapsed = float((resume or {}).get("prior_elapsed_s", 0.0) or 0.0)
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

        if league == "contemplator":
            backstop = self.safety_wall
            remaining_cycles = max(1, self.cyc_cap - cycles_consumed(work))
        else:
            backstop = self.wall
            remaining_cycles = None
        remaining = self._remaining_wall_s(consumed, backstop)
        if resume:
            self.log(
                f"  resuming at {int(consumed/60)}min consumed — "
                f"{int(remaining/60)}min of the {backstop} backstop left"
                + (
                    f", {remaining_cycles}/{self.cyc_cap} cycles left"
                    if remaining_cycles is not None
                    else ""
                )
            )
        else:
            self.log(
                "  mission running"
                + (
                    f" (league={league}, cap {self.cyc_cap} cycles, "
                    f"safety wall {backstop})"
                    if league == "contemplator"
                    else f" (league={league}, wall {backstop})"
                )
            )

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
                ]
                + (
                    ["--max-cycles", str(remaining_cycles)]
                    if remaining_cycles is not None
                    else []
                ),
                cwd=ROOT,
                env=env,
                stdout=fh,
                stderr=subprocess.STDOUT,
            )

        control = None
        wall_s = parse_duration(backstop or self.wall)
        overrun_killed = False
        while proc.poll() is None:
            self._heartbeat(idx, config, work, slog, rlog, started)
            # Time-up backstop: the agent parks itself at a CYCLE boundary, so
            # a wedged cycle can outlive its wall indefinitely. Past wall +
            # grace, take the arm down and stage what it built.
            if wall_s and (time.time() - started) > wall_s + WALL_GRACE_S:
                over = int((time.time() - started - wall_s) / 60)
                self.log(
                    f"  !! TIME-UP KILL — {over}min past the {backstop} wall "
                    f"(grace {WALL_GRACE_S // 60}min); the agent never parked, "
                    f"so the cycle is wedged. Staging what exists."
                )
                overrun_killed = True
                proc.terminate()
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
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

        elapsed_s = (time.time() - started) + prior_elapsed
        if control == "pause":
            # A paused arm is NOT staged: it is unfinished on purpose and will
            # be judged only once it has spent its full backstop.
            # consumed_s is the BUDGET clock and must stay the leg only —
            # `tier resume` subtracts it from the backstop. Folding prior
            # elapsed in would hand the next resume a budget already spent
            # and strand it on _remaining_wall_s's 60-second floor.
            # prior_elapsed rides separately so the RECORD stays total.
            self._paused_state = {
                "config": config,
                "work": str(work),
                "consumed_s": elapsed_s - prior_elapsed,
                "prior_elapsed_s": prior_elapsed,
                "index": idx,
            }
            return (
                ArmResult(
                    config, "paused", int(elapsed_s / 60), len(self._authored(work))
                ),
                control,
            )

        res = self._tally(config, work, rlog, int(elapsed_s / 60))
        res.status = "skipped" if control in ("skip", "force-stop") else "completed"
        if overrun_killed:
            # The arm is staged (it built what it built) but the record must
            # say the wall did not stop it cleanly — a reader comparing cyc/h
            # or goals against a normally-parked arm needs to know this one
            # was cut out of a wedged cycle.
            res.status = "wall_killed"
            res.detail = (
                f"time-up kill: never parked within "
                f"{WALL_GRACE_S // 60}min grace past the wall"
            )
        res.league = league
        res.cycles = cycles_consumed(work)
        rate = (res.cycles / (res.minutes / 60)) if res.minutes else 0.0
        self.log(
            f"  done {res.minutes}min files={res.files} "
            f"py_ok={res.py_ok} py_fail={res.py_fail} degen={res.degenerations} "
            f"| {res.cycles} cyc ({rate:.1f} cyc/h) | {res.goals}"
        )
        if (
            league == "contemplator"
            and res.cycles < self.cyc_cap
            and res.status != "completed"
        ):
            # The 4h safety wall bound this arm, not its cycle budget — so
            # there is unspent budget an extend can finish. Say it here, at
            # the moment it happens, with the command already written.
            self.log(
                f"  !! SHORT FINISH — {res.cycles}/{self.cyc_cap} cycles; "
                f"the safety wall bound this arm, not its cycle budget"
            )
            self.log(
                f"     ouroboros.py tier extend --run {self.base.name} "
                f"--arm {config}"
            )
        res.staged = self._stage(work, self._slot_for(idx, config), config)
        self._snapshot_agent(work, config)
        return res, control

    def _slot_for(self, idx: int, config: str) -> int:
        """The staged slot this arm owns.

        `staged/armNN` is a PUBLISHED CITATION KEY — dev/blind_panel/LADDER.md
        cites artifacts as `tier_<stamp>/staged/armNN` — so a re-entry must
        write back into the arm's own slot rather than wherever it happens to
        land in the current queue.

        It used to be the loop index over `self.arms`, which on ANY re-entry
        is the remaining queue restarting at 1. `tier resume` on arm 3 of 6
        therefore re-staged into `arm01`, and stage.py:184 rmtree's the
        destination first — silently destroying arm 1's artifact. That is
        live today, not a hazard introduced by extend.

        A fresh arm keeps `idx` deliberately: arms that produce no artifact
        consume no slot, so deriving from the manifest would renumber a
        fresh run and invalidate the citations.
        """
        prior = arm_slot(self.base, config)
        if prior is not None:
            return prior
        return next_free_slot(self.base) if self._adopted else idx

    def _snapshot_agent(self, work: Path, config: str) -> None:
        """Telemetry for the OBSERVED half of the record — and the run-local
        mission identity `tier extend` verifies a workspace against.

        `cp -a SRC DST` with DST an existing directory copies INTO it, so a
        re-entry produced `<label>_agent/.agent/` and the snapshot at the
        expected path silently stopped updating. Clear first; `.agent`
        accumulates, so the newer copy is a superset of the old.
        """
        agent_dir = work / ".agent"
        if not agent_dir.is_dir():
            return
        dest = self.base / f"{config}_agent"
        subprocess.run(["rm", "-rf", str(dest)])
        subprocess.run(["cp", "-a", str(agent_dir), str(dest)])

    def _tally(self, config: str, work: Path, rlog: Path, minutes: int) -> ArmResult:
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
            rlog.read_text(errors="ignore").count(DEGEN_MARKER) if rlog.exists() else 0
        )
        return ArmResult(
            config, "completed", minutes, len(files), ok, bad, goals, degen
        )

    def _heartbeat(
        self, idx: int, config: str, work: Path, slog: Path, rlog: Path, started: float
    ) -> None:
        files = self._authored(work)
        degen = (
            rlog.read_text(errors="ignore").count(DEGEN_MARKER) if rlog.exists() else 0
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
        if not self._claim_base():
            return 1
        self._adopt_prior_state()
        self.log(
            f"=== tier batch · {len(self.arms)} arms · {self.wall} each "
            f"· budget {self.budget_h}h ==="
        )
        if self.contemplator_cycles or self.contemplator_wall:
            # Loud on purpose. A contemplator arm run off the 30-cycle /
            # 4h contract is not cycle-comparable to the arms already in
            # TIER_JUDGEMENTS, and the batch log is where a later reader
            # looks first.
            self.log(
                f"    !! OFF-PROTOCOL contemplator governors — "
                f"cap {self.cyc_cap} cycles, safety wall {self.safety_wall} "
                f"(protocol: {CONTEMPLATOR_CYCLES} / "
                f"{CONTEMPLATOR_SAFETY_WALL}) — NOT cycle-comparable to "
                f"standard arms"
            )
        # THE PHASE CEILING IS PROVENANCE. An arm stopped at `structural` did
        # not attempt the phases that exercise what it built, so its artifact is
        # not comparable with one run to `quality` — and the batch log recorded
        # mission and rubric but not this, leaving archived arms unanswerable on
        # it after the fact. Live: a structural-only arm produced a tree whose
        # every defect was "authored but never exercised", which is exactly the
        # class the later phases exist to catch.
        self.log(
            f"    mission={self.mission} rubric={_rubric_version()} "
            f"top_phase={self.top_phase} base={self.base}"
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
            self._record(res)
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
            rate = f"{r.cycles / (r.minutes / 60):.1f}" if r.minutes else "-"
            self.log(
                f"  {r.config:<30} {r.status:<12} {r.minutes:>3}min "
                f"files={r.files:<3} py_fail={r.py_fail} degen={r.degenerations} "
                f"cyc={r.cycles:<3} {rate:>5} cyc/h "
                f"{'-> ' + r.staged if r.staged else ''}"
            )
        self.log(
            f"\nmanifest: {self.base / 'MANIFEST.txt'}  "
            f"(the map lives HERE, never inside staged/)"
        )

        # Contemplators the safety wall bound before their cycle budget ran
        # out. Listed at the moment of the finish so it lands in batch.log,
        # rather than being discovered weeks later from a YAML comment.
        short = [
            r
            for r in self.results
            if r.league == "contemplator" and 0 < r.cycles < self.cyc_cap
        ]
        if short:
            self.log(
                f"\n=== SHORT FINISHES ({len(short)}) — cycle budget left unspent ==="
            )
            for r in short:
                self.log(f"  {r.config:<30} {r.cycles}/{self.cyc_cap} cycles")
                self.log(
                    f"     ouroboros.py tier extend --run {self.base.name} "
                    f"--arm {r.config}"
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
            )
        else:
            if remaining:
                self.log(f"\nnot run: {', '.join(remaining)}")
            self._write_state(
                current_arm=None,
                finished=True,
                paused=False,
                remaining_arms=remaining,
            )
