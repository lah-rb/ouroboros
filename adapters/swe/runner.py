"""Per-instance SWE-bench runner: build container → run mission → extract patch.

Reuses the Ouroboros core verbatim (ContainerEffects, run_agent, the code_core
brownfield entry via ingest_workspace + pending_directive). The terminal-bench
harness couplings adapters.tb carries (BaseAgent/AgentResult/TmuxSession, the
~/.cache/terminal-bench task.yaml wall-clock derivation, the run-tests.sh dep
mirror) are dropped — SWE-bench images ship deps in the conda `testbed` env and
the repo is always at /testbed, so no probing is needed.

The mission may exit via a "parked as paused" RuntimeError (clean budget stop);
the container's final state is graded regardless, so the patch is extracted in
a `finally` no matter how the mission ends.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile

# Repo root via the canonical helper — the old two-dirname derivation
# silently became adapters/ when the flat *_adapter dirs moved under
# adapters/<name>/ (found 2026-07-21: every TB2 run since the move
# fast-failed on 'compiled.json not found in .../adapters/flows').
from agent.paths import repo_root as _repo_root

_REPO_ROOT = _repo_root()
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from adapters.swe.instance import SweInstance  # noqa: E402
from adapters.swe.patch import extract_model_patch, prediction_row  # noqa: E402
from adapters._common import (  # noqa: E402
    llmvp_endpoint,
    preserve_agent_dir,
    prune_mode,
    remove_image,
)  # noqa: E402
from agent.mission_runner import (  # noqa: E402
    build_and_save_mission,
    run_mission_isolated,
)

logger = logging.getLogger(__name__)

REPO_DIR = "/testbed"  # SWE-bench repos are always checked out here
_LLMVP = llmvp_endpoint()
# Iteration cap is a LOOSE runaway backstop, not the primary budget — wall-clock
# governs. A 20-cap bound FIRST on 52% of parked missions (some at ~516s, wasting
# >50% of the 1200s wall), inverting the intent; canonical SWE agents cap steps
# high (SWE-agent 250, OpenHands 100) and let cost/time govern. At ~26-90s/cycle a
# healthy mission does ~15-45 cycles within the wall, so 50 rarely binds yet still
# kills a degenerate fast-loop the wall alone would let spin for 1200s.
_MAX_CYCLES = int(os.environ.get("OURO_MAX_CYCLES", "50"))
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

    # code_core ADOPTS the foreign repo: ingest_workspace scans + extracts the
    # existing architecture, then mission_control sees the pending directive →
    # replan → the functional repair sweep against the real files. No greenfield
    # design. (Exactly the head-to-head path that solved langcodes.)
    mission = build_and_save_mission(
        host_tmp,
        instance.problem_statement,
        working_directory=REPO_DIR,
        flow_set="code_core",
        pending_directive=instance.problem_statement,
        task_profile="repair",
        llmvp_endpoint=_LLMVP,
        web_research=False,  # hermetic — no confounding network reach
        held_out_tests=True,  # SWE-bench applies the regression test itself
    )
    return mission, "ingest_workspace"


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
_PRUNE_MODE = prune_mode("OURO_SWE_PRUNE_IMAGES")


def _container_name(instance: SweInstance) -> str:
    return f"ouro-swe-{instance.instance_id}".replace("__", "_")[:60]


def _remove_image(client, image: str) -> None:
    remove_image(client, image)


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
    from adapters.tb.container_effects import ContainerEffects

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
            # CoT/prompt capture parity with adapters.tb (OURO_TRACE=1 → traces
            # carry thinking + rendered prompts for CoT-level failure analysis).
            trace_thinking=_TRACE,
            trace_prompts=_TRACE,
        )
        # Shared isolated harness (agent/mission_runner.py): dedicated
        # thread + loop + drain + park classification.
        outcome = run_mission_isolated(
            effects,
            mission_id=mission.id,
            entry_flow=entry_flow,
            max_cycles=cycles,
            max_wall_clock_s=wall,
        )
        if outcome.parked:
            logger.info("%s: budget stop (parked)", instance.instance_id)
        elif outcome.error is not None:
            logger.warning(
                "%s: mission failed: %s: %s",
                instance.instance_id,
                type(outcome.error).__name__,
                outcome.error,
            )
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

    logger.info("%s: %d-char patch", instance.instance_id, len(model_patch))
    # run_end: hand the freshly-pulled image up so run_pilot prunes it after the
    # whole run (pre-cached images are left alone; was_pulled gates that).
    prune_at_end = (
        instance.image_key if (was_pulled and _PRUNE_MODE == "run_end") else None
    )
    return prediction_row(instance.instance_id, model_name, model_patch), prune_at_end


def _preserve(host_tmp: str, logs_dir: str, instance_id: str) -> None:
    """Copy the mission .agent (mission.json + traces) for the taxonomy tool."""
    dst = os.path.join(logs_dir, instance_id, "ouroboros-mission")
    if not preserve_agent_dir(host_tmp, dst):
        logger.warning("%s: trace preservation failed", instance_id)
