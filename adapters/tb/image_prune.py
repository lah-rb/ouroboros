"""Image-prune policy for the terminal-bench adapters — mirrors
``adapters.swe.runner``'s ``OURO_SWE_PRUNE_IMAGES``, adapted to the harness-owned
container lifecycle.

The tb / Harbor harness CREATES and TEARS DOWN the task container; our agent only
uses ``session.container`` (or the resolved Harbor container). So — unlike the SWE
runner, which owns the container and prunes its image in the teardown ``finally`` —
here we prune the IMAGE that persists AFTER the harness kills the container. That
image is exactly what accumulated unbounded (the ~100GB of never-pruned
``alexgshaw/*`` TB2 task images that drove the host-memory creep; see memory
docker-image-creep-root-cause).

Modes (env ``OURO_TB_PRUNE_IMAGES``):
  off          — keep every image (fastest re-runs, unbounded growth).
  run_end      — DEFAULT: at process exit, remove every task image this run
                 touched. The clean analog of the SWE run_end sweep (the harness
                 owns per-task teardown, so exit is our only guaranteed hook) —
                 a run leaves no task images behind.
  per_instance — remove the PRIOR task's image at the next task's start (its
                 container is already torn down by then); atexit sweeps the last.
                 Tightest bound, but re-pulls any base image shared across tasks.

``note_task_image(container)`` is called once per task after the container is
resolved. Best-effort throughout — a prune failure never touches the run.
"""

from __future__ import annotations

import atexit
import logging

from adapters._common import prune_mode, remove_image

logger = logging.getLogger(__name__)

_PRUNE_MODE = prune_mode("OURO_TB_PRUNE_IMAGES")

_touched: set[str] = set()  # all task images seen (run_end sweep target)
_prev: list[str] = []  # [prior task image] — the per_instance lag slot
_atexit_registered = False


def _image_ref(container) -> str:
    """A removable ref for the container's image — its first tag, else its id."""
    img = getattr(container, "image", None)
    if img is None:
        return ""
    tags = getattr(img, "tags", None) or []
    return tags[0] if tags else (getattr(img, "id", "") or "")


def _remove_image(client, image: str) -> None:
    remove_image(client, image, label="tb")


def _run_end_sweep() -> None:
    """atexit hook: prune the touched task images (run_end = all; per_instance =
    just the final one — the earlier ones were removed by the lag)."""
    images = set(_prev) if _PRUNE_MODE == "per_instance" else set(_touched)
    if not images:
        return
    try:
        import docker

        client = docker.from_env()
    except Exception as e:  # noqa: BLE001
        logger.warning("tb: run-end prune skipped (no docker client): %s", e)
        return
    for image in images:
        _remove_image(client, image)
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass


def note_task_image(container) -> None:
    """Register the task's image for pruning per ``OURO_TB_PRUNE_IMAGES``. Call
    once per task, after the container is resolved. Never raises."""
    global _atexit_registered
    if _PRUNE_MODE == "off":
        return
    try:
        image = _image_ref(container)
        if not image:
            return
        if _PRUNE_MODE == "per_instance":
            client = getattr(container, "client", None)
            if _prev and _prev[0] != image:
                _remove_image(client, _prev[0])
            _prev[:] = [image]
        else:  # run_end
            _touched.add(image)
        if not _atexit_registered:
            atexit.register(_run_end_sweep)
            _atexit_registered = True
    except Exception as e:  # noqa: BLE001
        logger.warning("tb: note_task_image failed: %s", e)
