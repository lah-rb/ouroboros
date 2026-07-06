"""Harbor agent adapter: run an Ouroboros mission per Terminal-Bench 2.0 task.

The twin of :mod:`tb_adapter.agent` (the legacy ``terminal_bench`` adapter) for
the **Harbor** harness that Terminal-Bench 2.0 runs on. Harbor's contract is

  ``async setup(environment)`` then ``async run(instruction, environment, context)``

where ``environment.exec(command) -> ExecResult`` is the only handle to the task
container — Harbor deliberately hides the container id. We recover it (write a
unique marker via ``environment.exec``, find the docker container holding it) and
re-attach a docker-py ``Container``, which lets us reuse :class:`ContainerEffects`
**verbatim** — the whole by-name container stack (exec / archive read-write, the
MCP PTY terminal server, the container-aware settle probe) is harness-agnostic.

Per task we:
  1. resolve the task container and its cwd,
  2. self-cap just under Harbor's own ``asyncio.wait_for`` deadline (the task's
     ``[agent].timeout_sec`` × multiplier) so the mission *parks* cleanly before
     Harbor hard-cancels ``run()`` — Harbor grades the container's final state
     either way,
  3. run an Ouroboros mission (``ops`` or ``code_core``) on the host via
     ``run_agent`` with a :class:`ContainerEffects`,
  4. populate ``context`` token metrics and preserve the mission traces into
     ``self.logs_dir`` (Harbor syncs that back to the host).

Register with:
  ``harbor run -d terminal-bench@2.0 \
     --agent-import-path tb_adapter.harbor_agent:OuroborosHarborAgent``
Invoke ``harbor`` from the repo root (and from our ``.venv``, which has both
Harbor and the Ouroboros deps) so ``agent.*`` / ``tb_adapter.*`` import.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MAX_CYCLES = int(os.environ.get("OURO_MAX_CYCLES", "20"))
# OURO_WALL_CLOCK_S pins the exact self-cap wall clock (debug knob, no margin).
_WALL_OVERRIDE = os.environ.get("OURO_WALL_CLOCK_S")
_CAP_FRACTION = 0.9  # park just under Harbor's own wait_for deadline.
_CAP_FALLBACK = 600.0  # if task.toml can't be read (TB2 caps run large).
# Harbor enforces the agent budget as
#   --agent.override-timeout-sec  (if set)  else  task.config.agent.timeout_sec,
# then × --agent-timeout-multiplier (capped at --agent.max-timeout-sec)
# (harbor/trial/trial.py:_compute_agent_timeout_sec). The agent never sees those
# flags, so mirror them via env vars set alongside the flags — else our self-cap
# diverges from Harbor's real deadline and we either self-handicap or get
# hard-cancelled mid-write instead of parking.
_GLOBAL_AGENT_TIMEOUT = os.environ.get("OURO_GLOBAL_AGENT_TIMEOUT_S")
try:
    _TIMEOUT_MULTIPLIER = float(os.environ.get("OURO_TIMEOUT_MULTIPLIER", "1") or "1")
except ValueError:
    _TIMEOUT_MULTIPLIER = 1.0
_LLMVP = os.environ.get("OURO_LLMVP", "http://localhost:8008/graphql")
_TRACE = os.environ.get("OURO_TRACE", "1") != "0"


class OuroborosHarborAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "ouroboros"

    def version(self) -> str | None:
        return "ouroboros-0.1"

    async def setup(self, environment: BaseEnvironment) -> None:
        # No installed-agent / MCP / skills handoff — Ouroboros' brain stays on
        # the host. The container is resolved lazily in run() (it is fully up by
        # then). Nothing to do here.
        return None

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        from agent.flow_sets import get_flow_set
        from agent.loop import run_agent
        from agent.persistence.manager import PersistenceManager
        from agent.persistence.models import MissionConfig, MissionState

        from tb_adapter.container_effects import ContainerEffects

        exec_user = self._exec_user(environment)
        container = await self._resolve_container(environment, exec_user)
        container_cwd = self._probe_container_cwd(container, exec_user)

        task_root = self._task_root(environment)
        wall_clock_s = (
            float(_WALL_OVERRIDE) if _WALL_OVERRIDE else self._per_task_cap(task_root)
        )

        host_tmp = tempfile.mkdtemp(prefix="ouro-tb2-")
        pty_scratch = os.path.join(host_tmp, "pty")
        os.makedirs(pty_scratch, exist_ok=True)

        # Mirror the bench's grading env so our completion checks don't false-fail
        # on deps the verifier installs (best-effort).
        mirrored = self._mirror_test_env(container, task_root, container_cwd, exec_user)

        self._note(
            f"{host_tmp}\ncontainer={container.name}\ncontainer_cwd={container_cwd}\n"
            f"wall_clock_s={wall_clock_s}\nexec_user={exec_user!r}\n"
            f"mirrored_deps={mirrored}\n"
        )

        # Default to the in-graph `classify` router (flow_set="auto"); the same
        # 2-label decision the M3 task_judge made, now in-graph so local and TB
        # share one router. OURO_FLOW_SET is a hard override that SKIPS routing.
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
        # Explicit override adopts the container repo (code_core→ingest_workspace)
        # or enters ops directly; "auto" lets classify route + hand off itself.
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
            exec_user=exec_user,
            trace_thinking=_TRACE,
            trace_prompts=_TRACE,
        )

        # Run the entire Ouroboros loop — and its MCP/anyio machinery and
        # teardown — in a dedicated worker thread with its OWN event loop. Harbor's
        # run() executes inside Harbor's anyio TaskGroup; awaiting our agent (whose
        # MCP client opens anyio cancel scopes) directly in that loop makes those
        # scopes enter in one task and exit in another → RuntimeError("exit cancel
        # scope in a different task") → a CancelledError that errors the trial
        # *after the mission already completed* (so a passing run is never graded).
        # asyncio.run() in a thread isolates the whole lifecycle — the legacy tb
        # adapter got this for free by calling asyncio.run() from a sync method.
        flows_dir = os.path.join(_REPO_ROOT, "flows")
        prompts_dir = os.path.join(_REPO_ROOT, "prompts")

        async def _run_with_drain():
            # Drain sessions the mission left open so it never strands one on
            # the single-instance pool for the next task.
            try:
                await run_agent(
                    mission_id=mission.id,
                    effects=effects,
                    flows_dir=flows_dir,
                    prompts_dir=prompts_dir,
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
                        except Exception:
                            pass

        def _run_mission_isolated() -> None:
            try:
                asyncio.run(_run_with_drain())
            except RuntimeError as e:
                # run_agent raises after parking the mission on budget exhaustion —
                # a clean stop, not a crash. Harbor grades the container regardless.
                if "parked as paused" not in str(e):
                    self._note(f"run_agent error: {e}\n")
            except Exception as e:  # real agent error (not budget)
                self._note(f"run_agent exception: {type(e).__name__}: {e}\n")

        try:
            await asyncio.to_thread(_run_mission_isolated)
        finally:
            # Preserve traces + metrics even when Harbor HARD-CANCELS run() (a
            # CancelledError raised on the await above) because the mission didn't
            # self-cap before Harbor's wait_for deadline. Without this finally the
            # MOST timeout-prone tasks — exactly the ones worth inspecting — leave
            # NO trace at all (the original run lost multi-source/chess this way).
            # The mission thread may still be running (a thread can't be force-
            # killed), but flushes are per-cycle so the trace-so-far is already on
            # disk; copy it. A half-written tail line is tolerated by the loaders.
            # The CancelledError re-propagates after this, so Harbor still records
            # the timeout and grades the container.
            self._preserve(host_tmp)
            tin, tout = self._token_totals(host_tmp)
            context.n_input_tokens = tin
            context.n_output_tokens = tout
            context.metadata = {
                # "auto" when routed in-graph (the resolved flow_set + profile
                # are in the preserved .agent/ouroboros-routing.json), else the
                # explicit OURO_FLOW_SET override.
                "flow_set": flow_set,
                "container": container.name,
                "wall_clock_s": wall_clock_s,
            }

    # ── container resolution ──────────────────────────────────────────
    async def _resolve_container(self, environment: BaseEnvironment, exec_user: str):
        """Re-attach a docker-py ``Container`` to the task's compose container.

        Harbor exposes no container id, so: write a unique marker via
        ``environment.exec`` (which targets the right container), then find the
        docker container whose marker matches. Harbor-version-independent and
        safe under ``--n-concurrent`` (each trial finds its own marker). We narrow
        the candidate set with the compose ``service=main`` label for speed."""
        import docker

        marker = "ouro-" + uuid.uuid4().hex
        await environment.exec(command=f"printf %s {marker} > /tmp/.ouro_cid")

        client = docker.from_env()
        candidates = []
        try:
            candidates = client.containers.list(
                filters={"label": "com.docker.compose.service=main"}
            )
        except Exception:
            candidates = []
        if not candidates:
            candidates = client.containers.list()

        for c in candidates:
            try:
                res = c.exec_run(cmd=["cat", "/tmp/.ouro_cid"], user=exec_user)
                out = (res.output or b"").decode("utf-8", "replace").strip()
                if out == marker:
                    return c
            except Exception:
                continue
        raise RuntimeError("could not resolve the Harbor task container by marker")

    @staticmethod
    def _exec_user(environment: BaseEnvironment) -> str:
        """The user Harbor runs the agent as (set on environment.default_user by
        the orchestrator). Match it so our docker-py exec / PTY grade in the same
        user context the verifier does."""
        u = getattr(environment, "default_user", None)
        return "" if u is None else str(u)

    # ── task wiring (timeout / env mirror / preservation) ─────────────
    def _task_root(self, environment: BaseEnvironment) -> Path | None:
        """The task source dir (holds task.toml). Harbor passes the task's own
        ``environment/`` dir as ``environment_dir`` (trial.py:676); its parent is
        the task root."""
        try:
            env_dir = getattr(environment, "environment_dir", None)
            if env_dir is None:
                return None
            root = Path(env_dir).parent
            return root if (root / "task.toml").is_file() else None
        except Exception:
            return None

    def _per_task_cap(self, task_root: Path | None) -> float:
        """Self-cap at ~0.9× Harbor's wait_for budget so we park before it fires.
        Mirrors Harbor's own computation: a global override wins, else the task's
        ``[agent].timeout_sec`` scaled by the global multiplier."""
        if _GLOBAL_AGENT_TIMEOUT:
            try:
                return max(60.0, float(_GLOBAL_AGENT_TIMEOUT) * _CAP_FRACTION)
            except ValueError:
                pass
        if task_root is not None:
            try:
                import tomllib

                d = tomllib.loads((task_root / "task.toml").read_text())
                t = float((d.get("agent") or {}).get("timeout_sec") or 0)
                if t > 0:
                    return max(60.0, t * _TIMEOUT_MULTIPLIER * _CAP_FRACTION)
            except Exception:
                pass
        return max(60.0, _CAP_FALLBACK * _TIMEOUT_MULTIPLIER)

    def _mirror_test_env(
        self, container, task_root: Path | None, cwd: str, exec_user: str
    ) -> list[str]:
        """Install the deps the task's tests install, into the container, so the
        agent's ``python3 x.py`` and our checks see the env the bench grades in.
        Best-effort. TB2 test scripts live in ``tests/`` (test.sh + *.py)."""
        if task_root is None:
            return []
        pkgs = self._extract_deps(task_root)
        if not pkgs:
            return []
        try:
            has_pip = (
                container.exec_run(
                    cmd=["python3", "-m", "pip", "--version"], user=exec_user
                ).exit_code
                == 0
            )
            if not has_pip:
                container.exec_run(
                    cmd=["apt-get", "update", "-q"], workdir=cwd, user=exec_user
                )
                container.exec_run(
                    cmd=["apt-get", "install", "-y", "-q", "python3-pip"],
                    workdir=cwd,
                    user=exec_user,
                )
            container.exec_run(
                cmd=[
                    "python3", "-m", "pip", "install", "--quiet",
                    "--break-system-packages", *pkgs,
                ],
                workdir=cwd,
                user=exec_user,
            )
        except Exception:
            pass
        return pkgs

    @staticmethod
    def _extract_deps(task_root: Path) -> list[str]:
        """Package names from ``uv add`` / ``pip install`` lines in the task's
        test scripts (tests/test.sh + tests/*.sh)."""
        import re

        pkgs: set[str] = set()
        files = list((task_root / "tests").glob("*.sh"))
        pat = re.compile(r"(?:uv\s+add|uv\s+pip\s+install|pip3?\s+install)\s+([^\n;&|]+)")
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

    def _preserve(self, host_tmp: str) -> None:
        """Copy the mission's .agent (mission.json + traces) into logs_dir so the
        run is inspectable after the container/host-tmp are gone. Harbor syncs
        logs_dir back to the host trial dir."""
        try:
            src = os.path.join(host_tmp, ".agent")
            if os.path.isdir(src):
                shutil.copytree(
                    src,
                    os.path.join(str(self.logs_dir), "ouroboros-mission"),
                    dirs_exist_ok=True,
                )
        except Exception:
            pass

    def _note(self, text: str) -> None:
        try:
            with open(Path(self.logs_dir) / "ouroboros-workdir.txt", "a") as f:
                f.write(text)
        except Exception:
            pass

    def _select_flow_set(self, instruction: str) -> tuple[str, str]:
        """Flow set + capability profile (the task judge): one cold-temp LLMVP
        classification into (ops|code_core, profile), with keyword fallbacks and
        an OURO_FLOW_SET override. Decision logged for audit."""
        from tb_adapter.task_judge import classify_flow_set

        log_path = Path(self.logs_dir) / "ouroboros-routing.json"
        flow_set, profile, method = classify_flow_set(
            instruction, _LLMVP, log_path=log_path
        )
        print(
            f"[ouroboros] flow_set={flow_set} profile={profile} ({method})",
            file=sys.stderr,
        )
        return flow_set, profile

    # ── helpers ───────────────────────────────────────────────────────
    def _probe_container_cwd(self, container, exec_user: str) -> str:
        """The task's 'current directory' — the container's default WORKDIR."""
        try:
            res = container.exec_run(cmd=["pwd"], user=exec_user)
            out = (res.output or b"").decode("utf-8", "replace").strip()
            first = out.splitlines()[0].strip() if out else ""
            if first.startswith("/"):
                return first
        except Exception:
            pass
        return "/app"

    def _token_totals(self, host_tmp: str) -> tuple[int, int]:
        """Sum inference token usage from the flushed trace JSONL (reporting only —
        never affects pass/fail). Best-effort; zeros on any issue."""
        tin = tout = 0
        try:
            for path in glob.glob(os.path.join(host_tmp, ".agent", "traces", "*.jsonl")):
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
