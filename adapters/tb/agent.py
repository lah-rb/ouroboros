"""terminal-bench agent adapter: run an Ouroboros ``ops`` mission per task.

Implements terminal-bench's ``BaseAgent``. For each task the harness hands us
an ``instruction`` and a ``TmuxSession`` whose ``.container`` is the task's
Docker container. We:

  1. spin up an Ouroboros ``ops`` mission with ``objective = instruction`` and
     ``working_directory = the container cwd`` (so the model's cwd-relative
     completion checks are correct),
  2. run it on the host via ``run_agent`` with a :class:`ContainerEffects` that
     routes shell work into the container,
  3. return an ``AgentResult``. The harness then runs its hidden tests against
     the container's final state — the same state the ops gate checked.

Three things are wired to the task itself (via the task_id embedded in
``logging_dir`` → the dataset-cache task dir): the **per-task timeout** (we cap
ourselves just under the task's own ``max_agent_timeout_sec`` instead of a fixed
number), the **test-env mirror** (we pre-install the deps the task's
``run-tests.sh`` installs — e.g. ``uv add numpy`` — so our completion checks see
the same environment the bench grades in, not a barer one that fails on a
missing import), and **trace preservation** (the mission's traces are copied
into ``logging_dir`` so runs are inspectable after the container is gone).

Register with: ``tb run --agent-import-path adapters.tb.agent:OuroborosAgent``.
Invoke ``tb`` from the repo root so ``agent.*`` / ``adapters.tb.*`` import.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.terminal.tmux_session import TmuxSession

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MAX_CYCLES = int(os.environ.get("OURO_MAX_CYCLES", "20"))
# When set, OURO_WALL_CLOCK_S overrides the per-task cap RAW (no safety margin —
# a debug knob to pin the exact wall clock).
_WALL_OVERRIDE = os.environ.get("OURO_WALL_CLOCK_S")
_CAP_FRACTION = 0.9  # park just under the harness's own per-task wait_for limit.
_CAP_FALLBACK = 300.0  # if the task dir can't be found.
# The harness computes its wait_for budget as
#   --global-agent-timeout-sec  (if set)  else
#   task.max_agent_timeout_sec × --global-timeout-multiplier
# (terminal_bench/harness/harness.py). The agent never receives those CLI
# flags, so mirror them via env vars — set them alongside the flags. Without
# this, our self-cap is computed off the raw task.yaml value and diverges from
# the harness's actual deadline under a global override/multiplier.
_GLOBAL_AGENT_TIMEOUT = os.environ.get("OURO_GLOBAL_AGENT_TIMEOUT_S")
try:
    _TIMEOUT_MULTIPLIER = float(os.environ.get("OURO_TIMEOUT_MULTIPLIER", "1") or "1")
except ValueError:
    _TIMEOUT_MULTIPLIER = 1.0
_LLMVP = os.environ.get("OURO_LLMVP", "http://localhost:8008/graphql")
# Detailed tracing on by default (capture judge CoT + full prompts/responses);
# set OURO_TRACE=0 to disable.
_TRACE = os.environ.get("OURO_TRACE", "1") != "0"


class OuroborosAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "ouroboros"

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        from agent.flow_sets import get_flow_set
        from agent.loop import run_agent
        from agent.persistence.manager import PersistenceManager
        from agent.persistence.models import MissionConfig, MissionState

        from adapters.tb.container_effects import ContainerEffects
        from adapters.tb.image_prune import note_task_image

        container = session.container
        # Register the task image for pruning (OURO_TB_PRUNE_IMAGES) — the harness
        # tears down the container but leaves the image, the unbounded sink.
        note_task_image(container)
        host_tmp = tempfile.mkdtemp(prefix="ouro-tb-")
        pty_scratch = os.path.join(host_tmp, "pty")
        os.makedirs(pty_scratch, exist_ok=True)
        container_cwd = self._probe_container_cwd(container)

        task_dir = self._task_dir(logging_dir)
        wall_clock_s = (
            float(_WALL_OVERRIDE) if _WALL_OVERRIDE else self._per_task_cap(task_dir)
        )
        # Mirror the bench's grading env so checks don't fail on deps the grader
        # installs (run-tests.sh: `uv add numpy` etc.).
        mirrored = self._mirror_test_env(container, task_dir, container_cwd)

        if logging_dir is not None:
            try:
                (Path(logging_dir) / "ouroboros-workdir.txt").write_text(
                    f"{host_tmp}\ncontainer_cwd={container_cwd}\n"
                    f"wall_clock_s={wall_clock_s}\nmirrored_deps={mirrored}\n"
                )
            except Exception:
                pass

        # Routing: default to the in-graph `classify` flow (flow_set="auto") —
        # the same 2-label decision the M3 task_judge made, now made in-graph so
        # local and TB share one router. classify picks flow_set + profile via
        # menu turns, seeds the directive, and hands off to the chosen
        # controller. OURO_FLOW_SET is a hard override that SKIPS routing.
        override = os.environ.get("OURO_FLOW_SET")
        flow_set = override if override in ("ops", "code_core") else "auto"

        pm = PersistenceManager(host_tmp)
        pm.init_agent_dir()
        mission = MissionState(
            objective=instruction,
            status="active",
            config=MissionConfig(
                working_directory=container_cwd,
                flow_set=flow_set,
                llmvp_endpoint=_LLMVP,
                # tb runs are hermetic — no web reach (keeps cross-model
                # comparison from being confounded by network access).
                web_research=False,
            ),
        )
        # Explicit override: code_core ADOPTS the container repo via
        # ingest_workspace (scan → extract architecture → replan → repair sweep);
        # ops enters its controller directly. For "auto", classify does both
        # (it seeds pending_directive for code_core and tail-calls the target).
        if flow_set == "code_core":
            mission.pending_directive = instruction
            entry_flow = "ingest_workspace"
        elif flow_set == "ops":
            entry_flow = get_flow_set("ops").entry_flow
        else:
            entry_flow = "classify"
        pm.save_mission(mission)

        effects = ContainerEffects(
            container=container,
            container_workdir=container_cwd,
            host_working_directory=host_tmp,
            host_pty_scratch=pty_scratch,
            llmvp_endpoint=_LLMVP,
            exec_user="",
            trace_thinking=_TRACE,
            trace_prompts=_TRACE,
        )

        async def _run_with_drain():
            # Drain sessions the mission left open (park/kill) so it never
            # strands one on the single-instance pool for the next task.
            try:
                await run_agent(
                    mission_id=mission.id,
                    effects=effects,
                    flows_dir=os.path.join(_REPO_ROOT, "flows"),
                    prompts_dir=os.path.join(_REPO_ROOT, "prompts"),
                    entry_flow=entry_flow,
                    max_cycles=_MAX_CYCLES,
                    max_wall_clock_s=wall_clock_s,
                )
            finally:
                for _teardown in ("end_open_inference_sessions", "mcp_disconnect_all"):
                    _fn = getattr(effects, _teardown, None)
                    if _fn is not None:
                        try:
                            await _fn()
                        except (Exception, asyncio.CancelledError):
                            # CancelledError is a BaseException — a cancellation
                            # landing during drain escaped the bare `except
                            # Exception`, killed asyncio.run, and cost the whole
                            # run its results.json (ab-adaptive-2). τ-adapter
                            # parity: swallow it; teardown is best-effort.
                            pass

        # Run the mission loop on ITS OWN thread + event loop (τ-adapter /
        # GAIA parity): run_agent's MCP/PTY machinery uses anyio cancel scopes
        # bound to the creating task, and the bench harness's own async
        # context can leak a cancellation into our subprocess waits when the
        # loops share a thread. A dedicated thread has no ambient scopes.
        failure_mode = FailureMode.NONE
        _outcome: dict[str, BaseException] = {}

        def _mission_thread() -> None:
            try:
                asyncio.run(_run_with_drain())
            except BaseException as e:  # noqa: BLE001 — classified below
                _outcome["exc"] = e

        _t = threading.Thread(
            target=_mission_thread, name="ouroboros-mission", daemon=True
        )
        _t.start()
        _t.join()
        _exc = _outcome.get("exc")
        if _exc is not None:
            # run_agent raises on budget exhaustion (cycle/wall-clock) after
            # parking the mission — that's a clean stop, not a crash. The bench
            # grades the container's final state regardless. Anything else is a
            # real agent error.
            if isinstance(_exc, RuntimeError) and "parked as paused" in str(_exc):
                failure_mode = FailureMode.AGENT_TIMEOUT
            else:
                failure_mode = FailureMode.UNKNOWN_AGENT_ERROR

        self._preserve(host_tmp, logging_dir)
        tin, tout = self._token_totals(host_tmp)
        return AgentResult(
            total_input_tokens=tin,
            total_output_tokens=tout,
            failure_mode=failure_mode,
        )

    # ── task wiring (timeout / env mirror / preservation) ─────────────
    def _task_dir(self, logging_dir: Path | None) -> Path | None:
        """The dataset-cache dir for this task, via the task_id embedded in
        logging_dir (``…/<task_id>/<trial>/agent-logs``)."""
        if logging_dir is None:
            return None
        try:
            task_id = Path(logging_dir).parent.parent.name
            if not task_id:
                return None
            hits = glob.glob(
                os.path.expanduser(
                    os.path.join("~", ".cache", "terminal-bench", "*", "*", task_id)
                )
            )
            for h in hits:
                if os.path.isfile(os.path.join(h, "task.yaml")):
                    return Path(h)
        except Exception:
            pass
        return None

    def _per_task_cap(self, task_dir: Path | None) -> float:
        """Self-cap at ~0.9× the harness's wait_for budget so we park before it
        fires (and don't self-handicap). Mirrors the harness's own computation:
        a global override wins; otherwise the task's max_agent_timeout_sec scaled
        by the global multiplier."""
        # --global-agent-timeout-sec overrides the task value entirely.
        if _GLOBAL_AGENT_TIMEOUT:
            try:
                return max(60.0, float(_GLOBAL_AGENT_TIMEOUT) * _CAP_FRACTION)
            except ValueError:
                pass
        if task_dir is not None:
            try:
                import yaml

                d = yaml.safe_load((task_dir / "task.yaml").read_text()) or {}
                t = float(d.get("max_agent_timeout_sec") or 0)
                if t > 0:
                    return max(60.0, t * _TIMEOUT_MULTIPLIER * _CAP_FRACTION)
            except Exception:
                pass
        return max(60.0, _CAP_FALLBACK * _TIMEOUT_MULTIPLIER)

    def _mirror_test_env(self, container, task_dir: Path | None, cwd: str) -> list[str]:
        """Install the deps the task's grading scripts install, into the
        container's system python, so the agent's `python3 x.py` (and our checks)
        see the same env the bench grades in. Best-effort.

        The t-bench ubuntu-24-04 images ship NO pip and NO ensurepip (the graders
        use `uv`), so a bare `python3 -m pip install` is a silent no-op — which is
        exactly why csv-class tasks thrashed forever on install. So: ensure pip
        first (`apt-get install python3-pip`, ~15s), THEN install with
        --break-system-packages (PEP-668). Returns the deps it attempted."""
        if task_dir is None:
            return []
        pkgs = self._extract_deps(task_dir)
        if not pkgs:
            return []
        try:
            has_pip = (
                container.exec_run(cmd=["python3", "-m", "pip", "--version"]).exit_code
                == 0
            )
            if not has_pip:
                container.exec_run(cmd=["apt-get", "update", "-q"], workdir=cwd)
                container.exec_run(
                    cmd=["apt-get", "install", "-y", "-q", "python3-pip"], workdir=cwd
                )
            container.exec_run(
                cmd=[
                    "python3",
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--break-system-packages",
                    *pkgs,
                ],
                workdir=cwd,
            )
        except Exception:
            pass
        return pkgs

    @staticmethod
    def _extract_deps(task_dir: Path) -> list[str]:
        """Package names from `uv add` / `pip install` lines in the task's
        grading scripts (run-tests.sh + tests/*.sh)."""
        pkgs: set[str] = set()
        files = [task_dir / "run-tests.sh", *((task_dir / "tests").glob("*.sh"))]
        pat = re.compile(
            r"(?:uv\s+add|uv\s+pip\s+install|pip3?\s+install)\s+([^\n;&|]+)"
        )
        for p in files:
            try:
                text = p.read_text(errors="replace")
            except Exception:
                continue
            for m in pat.finditer(text):
                for tok in m.group(1).split():
                    tok = tok.strip().strip("\"'")
                    if not tok or tok.startswith("-"):
                        continue
                    if any(c in tok for c in "$/.=") or tok in ("install", "add"):
                        continue
                    pkgs.add(tok)
        return sorted(pkgs)

    def _preserve(self, host_tmp: str, logging_dir: Path | None) -> None:
        """Copy the mission's .agent (mission.json + traces) into logging_dir so
        the run is inspectable after the container/host-tmp are gone."""
        if logging_dir is None:
            return
        try:
            src = os.path.join(host_tmp, ".agent")
            if os.path.isdir(src):
                shutil.copytree(
                    src,
                    os.path.join(str(logging_dir), "ouroboros-mission"),
                    dirs_exist_ok=True,
                )
        except Exception:
            pass

    def _select_flow_set(
        self, instruction: str, logging_dir: Path | None
    ) -> tuple[str, str]:
        """Flow set + capability profile for this task (the M3 task judge): one
        cold-temp LLMVP classification into (ops|code_core, profile), with keyword
        heuristic fallbacks and an OURO_FLOW_SET override for the flow set. The
        profile (service|data_transform|invertible|repair|answer|plain) gates the
        completion oracle rungs. Decision + method logged to
        logging_dir/ouroboros-routing.json for audit."""
        from adapters.tb.task_judge import classify_flow_set

        log_path = (
            Path(logging_dir) / "ouroboros-routing.json" if logging_dir else None
        )
        flow_set, profile, method = classify_flow_set(
            instruction, _LLMVP, log_path=log_path
        )
        print(
            f"[ouroboros] flow_set={flow_set} profile={profile} ({method})",
            file=sys.stderr,
        )
        return flow_set, profile

    # ── helpers ───────────────────────────────────────────────────────
    def _probe_container_cwd(self, container) -> str:
        """The task's 'current directory' — the container's default WORKDIR."""
        try:
            res = container.exec_run(cmd=["pwd"])
            out = (res.output or b"").decode("utf-8", "replace").strip()
            first = out.splitlines()[0].strip() if out else ""
            if first.startswith("/"):
                return first
        except Exception:
            pass
        return "/app"

    def _token_totals(self, host_tmp: str) -> tuple[int, int]:
        """Sum inference token usage from the flushed trace JSONL (reporting
        only — never affects pass/fail). Best-effort; zeros on any issue."""
        tin = tout = 0
        try:
            for path in glob.glob(
                os.path.join(host_tmp, ".agent", "traces", "*.jsonl")
            ):
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        tin += int(row.get("tokens_in") or 0)
                        tout += int(row.get("tokens_out") or 0)
        except Exception:
            return 0, 0
        return tin, tout
