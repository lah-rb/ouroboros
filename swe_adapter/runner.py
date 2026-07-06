"""Per-instance SWE-bench runner: build container → run mission → extract patch.

Reuses the Ouroboros core verbatim (ContainerEffects, run_agent, the code_core
brownfield entry via ingest_workspace + pending_directive). The terminal-bench
harness couplings tb_adapter carries (BaseAgent/AgentResult/TmuxSession, the
~/.cache/terminal-bench task.yaml wall-clock derivation, the run-tests.sh dep
mirror) are dropped — SWE-bench images ship deps in the conda `testbed` env and
the repo is always at /testbed, so no probing is needed.

The mission may exit via a "parked as paused" RuntimeError (clean budget stop);
the container's final state is graded regardless, so the patch is extracted in
a `finally` no matter how the mission ends.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from swe_adapter.instance import SweInstance  # noqa: E402
from swe_adapter.patch import extract_model_patch, prediction_row  # noqa: E402

logger = logging.getLogger(__name__)

REPO_DIR = "/testbed"  # SWE-bench repos are always checked out here
_LLMVP = os.environ.get("OURO_LLMVP", "http://localhost:8008/graphql")
_MAX_CYCLES = int(os.environ.get("OURO_MAX_CYCLES", "20"))
_WALL_CLOCK_S = float(os.environ.get("OURO_WALL_CLOCK_S", "1200") or "1200")
# CoT/prompt capture parity with the tb adapter: OURO_TRACE=1 → capture thinking
# + rendered prompts into the trace (for CoT-level failure analysis).
_TRACE = bool(os.environ.get("OURO_TRACE"))


def build_mission(instance: SweInstance, host_tmp: str):
    """Construct the MissionState for a SWE-bench instance (no container / LLM).

    Pure and testable: forces the code_core brownfield path with the repair
    profile — no LLM flow-set judge (SWE-bench is always code_core + repair).
    Returns (mission, entry_flow).
    """
    from agent.persistence.manager import PersistenceManager
    from agent.persistence.models import MissionConfig, MissionState

    pm = PersistenceManager(host_tmp)
    pm.init_agent_dir()
    mission = MissionState(
        objective=instance.problem_statement,
        status="active",
        config=MissionConfig(
            working_directory=REPO_DIR,
            flow_set="code_core",
            task_profile="repair",
            llmvp_endpoint=_LLMVP,
            web_research=False,  # hermetic — no confounding network reach
            held_out_tests=True,  # SWE-bench applies the regression test itself
        ),
    )
    # code_core ADOPTS the foreign repo: ingest_workspace scans + extracts the
    # existing architecture, then mission_control sees the pending directive →
    # replan → the functional repair sweep against the real files. No greenfield
    # design. (Exactly the head-to-head path that solved langcodes.)
    mission.pending_directive = instance.problem_statement
    entry_flow = "ingest_workspace"
    pm.save_mission(mission)
    return mission, entry_flow


def _docker_client():
    import docker

    return docker.from_env()


# Image-prune policy (the 162GB / unified-memory driver): each instance uses a
# distinct multi-GB sweb image, and nothing pruned them, so the Docker VM's
# layer page-cache + Rosetta amd64 translation cache grew unbounded on a Mac.
#   off          — keep every image (fastest re-runs, unbounded growth)
#   run_end      — DEFAULT: remove only images this run freshly PULLED, at the
#                  end. Keeps pre-cached images; a run leaves nothing new behind.
#   per_instance — remove each image right after its instance (one image
#                  resident at a time; re-pulls on any re-run).
_PRUNE_MODE = os.environ.get("OURO_SWE_PRUNE_IMAGES", "run_end").strip().lower()
if _PRUNE_MODE not in ("off", "run_end", "per_instance"):
    _PRUNE_MODE = "run_end"


def _container_name(instance: SweInstance) -> str:
    return f"ouro-swe-{instance.instance_id}".replace("__", "_")[:60]


def _remove_image(client, image: str) -> None:
    """Best-effort image removal (frees the layer cache the VM holds resident)."""
    try:
        client.images.remove(image, force=True)
        logger.info("pruned image %s", image)
    except Exception as e:  # noqa: BLE001
        logger.warning("image prune failed for %s: %s", image, e)


def _start_container(client, instance: SweInstance) -> tuple:
    """Pull (if needed) and start a detached container. Returns
    ``(container, was_pulled)`` — was_pulled gates run_end pruning so we only
    remove images this run introduced, not the operator's pre-cached ones."""
    image = instance.image_key
    was_pulled = False
    try:
        client.images.get(image)
    except Exception:
        logger.info("pulling %s (first use)…", image)
        client.images.pull(image)
        was_pulled = True
    # Name-collision guard: a prior run killed mid-instance can leave a
    # same-named container; containers.run would 409 BEFORE the try/finally and
    # strand it. Force-remove any stale one first so teardown stays sound.
    name = _container_name(instance)
    try:
        client.containers.get(name).remove(force=True)
        logger.info("removed stale container %s before start", name)
    except Exception:
        pass  # normal: no pre-existing container
    container = client.containers.run(
        image,
        command="sleep infinity",
        detach=True,
        # amd64 images under Apple silicon need Rosetta (QEMU segfaults the
        # verifiers) — platform is honored by Docker Desktop's Rosetta setting.
        platform="linux/amd64",
        working_dir=REPO_DIR,
        name=name,
    )
    return container, was_pulled


def run_instance(
    instance: SweInstance,
    model_name: str,
    logs_dir: str,
    wall_clock_s: float | None = None,
    max_cycles: int | None = None,
    client=None,
) -> tuple[dict, str | None]:
    """Run one Ouroboros mission against a SWE-bench instance; return
    ``(predictions_row, image_to_prune_at_run_end)``. The second is the image
    key when this run PULLED it and the mode is run_end (so run_pilot prunes it
    after the whole run), else None. Never raises for a mission-level failure —
    a crashed or parked mission yields whatever patch the container holds.

    ``client`` — a shared docker client (run_pilot reuses ONE across instances
    rather than leaking a per-instance ``from_env()``). None → create+close
    locally (standalone use)."""
    from agent.loop import run_agent
    from tb_adapter.container_effects import ContainerEffects

    wall = wall_clock_s if wall_clock_s is not None else _WALL_CLOCK_S
    cycles = max_cycles if max_cycles is not None else _MAX_CYCLES

    own_client = client is None
    client = client or _docker_client()
    container, was_pulled = _start_container(client, instance)
    host_tmp = tempfile.mkdtemp(prefix="ouro-swe-")
    pty_scratch = os.path.join(host_tmp, "pty")
    os.makedirs(pty_scratch, exist_ok=True)
    model_patch = ""
    try:
        mission, entry_flow = build_mission(instance, host_tmp)
        effects = ContainerEffects(
            container=container,
            container_workdir=REPO_DIR,
            host_working_directory=host_tmp,
            host_pty_scratch=pty_scratch,
            llmvp_endpoint=_LLMVP,
            exec_user="",
            # CoT/prompt capture parity with tb_adapter (OURO_TRACE=1 → traces
            # carry thinking + rendered prompts for CoT-level failure analysis).
            trace_thinking=_TRACE,
            trace_prompts=_TRACE,
        )
        async def _run_with_drain():
            # Drain sessions the mission left open so it never strands one on
            # the single-instance pool for the next instance.
            try:
                await run_agent(
                    mission_id=mission.id,
                    effects=effects,
                    flows_dir=os.path.join(_REPO_ROOT, "flows"),
                    prompts_dir=os.path.join(_REPO_ROOT, "prompts"),
                    entry_flow=entry_flow,
                    max_cycles=cycles,
                    max_wall_clock_s=wall,
                )
            finally:
                # Drain LLMVP sessions AND disconnect MCP (kills the terminal
                # server tree) INSIDE the loop, before it closes — on the park
                # exit the per-flow close never ran, so PTY/server processes
                # would otherwise orphan.
                for teardown in ("end_open_inference_sessions", "mcp_disconnect_all"):
                    fn = getattr(effects, teardown, None)
                    if fn is not None:
                        try:
                            await fn()
                        except Exception:
                            pass

        try:
            asyncio.run(_run_with_drain())
        except RuntimeError as e:
            if "parked as paused" in str(e):
                logger.info("%s: budget stop (parked)", instance.instance_id)
            else:
                logger.warning("%s: mission RuntimeError: %s", instance.instance_id, e)
        except Exception:
            logger.exception("%s: mission crashed", instance.instance_id)
    finally:
        # The container's final state is what the grader sees — extract the
        # patch no matter how the mission ended.
        try:
            model_patch = extract_model_patch(container, REPO_DIR)
        except Exception:
            logger.exception("%s: patch extraction failed", instance.instance_id)
        _preserve(host_tmp, logs_dir, instance.instance_id)
        try:
            container.remove(force=True)
        except Exception:
            logger.warning("%s: container teardown failed", instance.instance_id)
        # per_instance: prune the image now (tightest memory bound).
        if _PRUNE_MODE == "per_instance":
            _remove_image(client, instance.image_key)
        if own_client:  # standalone call owns its client — close it (FD/socket leak)
            try:
                client.close()
            except Exception:
                pass

    logger.info(
        "%s: %d-char patch", instance.instance_id, len(model_patch)
    )
    # run_end: hand the freshly-pulled image up so run_pilot prunes it after the
    # whole run (pre-cached images are left alone; was_pulled gates that).
    prune_at_end = instance.image_key if (was_pulled and _PRUNE_MODE == "run_end") else None
    return prediction_row(instance.instance_id, model_name, model_patch), prune_at_end


def _preserve(host_tmp: str, logs_dir: str, instance_id: str) -> None:
    """Copy the mission .agent (mission.json + traces) for the taxonomy tool."""
    src = os.path.join(host_tmp, ".agent")
    if not os.path.isdir(src):
        return
    dst = os.path.join(logs_dir, instance_id, "ouroboros-mission")
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except Exception:
        logger.warning("%s: trace preservation failed", instance_id)
