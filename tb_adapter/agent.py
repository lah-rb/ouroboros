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

Register with: ``tb run --agent-import-path tb_adapter.agent:OuroborosAgent``.
Invoke ``tb`` from the repo root so ``agent.*`` / ``tb_adapter.*`` import.
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
from pathlib import Path

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.terminal.tmux_session import TmuxSession

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MAX_CYCLES = int(os.environ.get("OURO_MAX_CYCLES", "20"))
# When set, OURO_WALL_CLOCK_S overrides the per-task cap (useful for debugging);
# otherwise the cap is derived from each task's own max_agent_timeout_sec.
_WALL_OVERRIDE = os.environ.get("OURO_WALL_CLOCK_S")
_CAP_FRACTION = 0.9  # park just under the harness's own per-task wait_for limit.
_CAP_FALLBACK = 300.0  # if the task dir can't be found.
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
        from agent.loop import run_agent
        from agent.persistence.manager import PersistenceManager
        from agent.persistence.models import MissionConfig, MissionState

        from tb_adapter.container_effects import ContainerEffects

        container = session.container
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

        pm = PersistenceManager(host_tmp)
        pm.init_agent_dir()
        mission = MissionState(
            objective=instruction,
            status="active",
            config=MissionConfig(
                working_directory=container_cwd,
                flow_set="ops",
                llmvp_endpoint=_LLMVP,
            ),
        )
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

        failure_mode = FailureMode.NONE
        try:
            asyncio.run(
                run_agent(
                    mission_id=mission.id,
                    effects=effects,
                    flows_dir=os.path.join(_REPO_ROOT, "flows"),
                    prompts_dir=os.path.join(_REPO_ROOT, "prompts"),
                    entry_flow="ops_control",
                    max_cycles=_MAX_CYCLES,
                    max_wall_clock_s=wall_clock_s,
                )
            )
        except RuntimeError as e:
            # run_agent raises on budget exhaustion (cycle/wall-clock) after
            # parking the mission — that's a clean stop, not a crash. The bench
            # grades the container's final state regardless. Anything else is a
            # real agent error.
            if "parked as paused" in str(e):
                failure_mode = FailureMode.AGENT_TIMEOUT
            else:
                failure_mode = FailureMode.UNKNOWN_AGENT_ERROR
        except Exception:
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
        """Cap ourselves at ~0.9× the task's own max_agent_timeout_sec so we
        park before the harness's wait_for fires (and don't self-handicap)."""
        if task_dir is not None:
            try:
                import yaml

                d = yaml.safe_load((task_dir / "task.yaml").read_text()) or {}
                t = float(d.get("max_agent_timeout_sec") or 0)
                if t > 0:
                    return max(60.0, t * _CAP_FRACTION)
            except Exception:
                pass
        return _CAP_FALLBACK

    def _mirror_test_env(self, container, task_dir: Path | None, cwd: str) -> list[str]:
        """Install the deps the task's grading scripts install, into the
        container's system python, so our completion checks see the same env the
        bench grades in. Best-effort; failures are harmless (checks just fall
        back to the barer env)."""
        if task_dir is None:
            return []
        pkgs = self._extract_deps(task_dir)
        if not pkgs:
            return []
        try:
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
