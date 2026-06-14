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

Register with: ``tb run --agent-import-path tb_adapter.agent:OuroborosAgent``.
Invoke ``tb`` from the repo root so ``agent.*`` / ``tb_adapter.*`` import.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import tempfile
from pathlib import Path

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.terminal.tmux_session import TmuxSession

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Internal bounds (the harness enforces its own agent timeout in a worker
# thread it cannot kill, so we cap ourselves *below* it and park cleanly).
_MAX_CYCLES = int(os.environ.get("OURO_MAX_CYCLES", "15"))
_WALL_CLOCK_S = float(os.environ.get("OURO_WALL_CLOCK_S", "240"))
_LLMVP = os.environ.get("OURO_LLMVP", "http://localhost:8008/graphql")


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

        if logging_dir is not None:
            try:
                (Path(logging_dir) / "ouroboros-workdir.txt").write_text(
                    f"{host_tmp}\ncontainer_cwd={container_cwd}\n"
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
                    max_wall_clock_s=_WALL_CLOCK_S,
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

        tin, tout = self._token_totals(host_tmp)
        return AgentResult(
            total_input_tokens=tin,
            total_output_tokens=tout,
            failure_mode=failure_mode,
        )

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
