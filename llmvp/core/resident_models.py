"""Multiple local models HOT and addressable at once (Phase 2b, un-parked).

WHY THIS EXISTS NOW, AND WHY IT DID NOT BEFORE.

MULTI_MODEL_PLAN.md parked local co-residency on the P0.b verdict of
2026-07-16: two models decoding in one process produced concurrent wall ==
solo SUM (Metal serialized the kernels) plus one greedy divergence, so the
plan concluded "true parallelism = cross-process" and shipped the
``openai_compat`` adapter as the escape hatch.

Both halves of that verdict were re-measured on 2026-08-13 against the
current build (b10243) and BOTH fell:

  * The divergence was a PROBE ARTIFACT. ``generate_stream_sync`` runs
    ``Llama.generate(reset=False)`` by design, and the probe reused one
    instance across every arm without restoring it, so dynamic context
    accumulated and the same prompt decoded at a different depth picked
    differently. With a per-decode restore the divergence vanishes
    entirely and serialization tightens from a 0.20-1.09 scatter to
    0.61-0.62. Nothing was ever corrupted by concurrency.
  * Topology is NOT the variable. Same pair, same questions, same arm
    protocol: in-process 0.619, cross-process 0.575 — a gap of 0.044, and
    the probe biases AGAINST cross-process. Two models in one process cost
    essentially nothing versus two processes.

  (serialization = (t_concurrent - max(legs)) / (sum(legs) - max(legs));
   0.0 = free overlap, 1.0 = fully serialized.)

So this module ships WITHOUT the global cross-backend decode lock Phase 2
settled on. That lock existed to contain a hazard that does not exist. The
evidence is in dev/PARALLEL_LANES_2026-08-13.md §7d and the probe is
llmvp/dev/probe_dual_model_strategies.py — re-run it before re-adding one.

WHAT ACTUALLY GOVERNS THROUGHPUT is the pair of WORKLOAD SHAPES, not the
process boundary. Measured the same day: OCR (compute-bound) against text
decode (bandwidth-bound) overlaps at 0.342, two text decoders at ~0.6, two
prefill-heavy tenants at 0.524. Complementary shapes interleave; identical
ones contend. A scheduler above this module should pair on that basis.

WHAT THIS MODULE OWNS. Secondary models only. The PRIMARY stays exactly
where it was — ``factory._backend_instance``, reached by ``get_backend()``
— because the swap machinery, the drain lifecycle and the session manager
are all built on that single reference, and moving it would rewrite
``swapModel`` for no benefit. This registry sits BESIDE it.

THE MEMORY GOVERNOR IS THE LOAD-BEARING PART. Three hard reboots
(2026-07-24) came from allocating KV that could not fit, and P0.a added the
rule that makes the arithmetic honest: a hot model stays FULLY wired until
explicitly unloaded — gpt-oss did not idle-unwire across 15+ quiet minutes,
so "it will shrink when idle" is not an assumption admission may make.
Every resident entry is therefore counted at full weight + full KV.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("llm-mvp")

# Wired-memory headroom the governor never spends. The OS, the page cache and
# every non-LLMVP process live here; a governor that admits right up to the
# iogpu limit hands the machine a hard reboot rather than a refusal.
HEADROOM_BYTES = 8 * 1024**3

# Fallback when sysctl cannot be read. This machine runs
# iogpu.wired_limit_mb=116000; the fallback is deliberately the same number
# so a sysctl failure does not silently widen the budget.
DEFAULT_WIRED_LIMIT_BYTES = 116_000 * 1024**2


def wired_limit_bytes() -> int:
    """The GPU wired ceiling, from sysctl, else the documented default."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "iogpu.wired_limit_mb"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        mb = int((out.stdout or "").strip())
        if mb > 0:
            return mb * 1024**2
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    log.warning(
        "resident: could not read iogpu.wired_limit_mb — assuming %d MB",
        DEFAULT_WIRED_LIMIT_BYTES // 1024**2,
    )
    return DEFAULT_WIRED_LIMIT_BYTES


def estimate_footprint_bytes(config: Any) -> int:
    """Upper-bound resident cost of one model: weights + projector + KV.

    Every term is an over-estimate on purpose. ``weights_bytes_total`` is
    FILE size (an MoE under mmap pages in only what it routes to), and KV is
    computed full-size because ``swa_full`` configs hold both caches. A
    governor that guesses low reboots the machine; one that guesses high
    refuses a load that might have fit, and the operator can lower n_ctx.
    """
    from inference.backends.llama_cpp_backend import LlamaCppBackend

    mcfg = config.model

    # probe_verified_weights_bytes, when the config declares it, is a MEASURED
    # figure and beats the file-size upper bound.
    verified = getattr(mcfg, "probe_verified_weights_bytes", None)
    if verified:
        total = int(verified)
    else:
        try:
            total = LlamaCppBackend.weights_bytes_total(str(mcfg.path))
        except OSError as exc:
            raise ValueError(f"cannot size weights at {mcfg.path}: {exc}") from exc

    mmproj = getattr(mcfg, "mmproj_path", None)
    if mmproj:
        # Counted SEPARATELY on purpose: weights_bytes_total deliberately
        # excludes a sibling mmproj-*.gguf, and probe_verified_weights_bytes
        # was measured without it too.
        try:
            total += os.path.getsize(str(mmproj))
        except OSError:
            log.warning("resident: mmproj %s unreadable — not counted", mmproj)

    from core.context_probe import kv_bytes_per_token

    try:
        per_tok = kv_bytes_per_token(
            str(mcfg.path), getattr(mcfg, "probe_verified_kv_bytes_per_token", None)
        )
        total += per_tok * int(mcfg.n_ctx)
        # THE VISION POOL IS REAL KV and must be priced. Each pool member is a
        # private context of its own — paddle at vision_n_ctx 32768 x 4 is
        # 2.25 GB, which is larger than its weights and projector combined.
        # Charging only the text n_ctx would let the governor admit a model
        # whose actual footprint it never saw. Counted whether or not vision
        # has been used yet: the pool is built on first request and never
        # released, so "not yet allocated" is a timing detail, not a saving.
        if mmproj:
            width = max(1, int(getattr(mcfg, "vision_pool_size", 1) or 1))
            total += per_tok * int(getattr(mcfg, "vision_n_ctx", 8192) or 8192) * width
    except Exception as exc:  # noqa: BLE001 — header shapes vary by arch
        raise ValueError(
            f"cannot compute KV geometry for {mcfg.name}: {exc} — "
            "declare probe_verified_kv_bytes_per_token or run --probe-context"
        ) from exc
    return total


@dataclass
class ResidentEntry:
    """One hot secondary model."""

    name: str
    backend: Any
    config: Any
    footprint_bytes: int
    loaded_at: float = 0.0
    requests: int = 0


@dataclass
class _Registry:
    entries: dict = field(default_factory=dict)
    # Per-name teardown latch, mirroring factory._teardown_failed. A failed
    # unload may have orphaned C contexts holding wired GPU memory, and the
    # entry is the only handle that can reach them — so the name is BURNED
    # for this process rather than allowed to stack a second copy on top.
    burned: set = field(default_factory=set)
    lock: Optional[asyncio.Lock] = None

    def get_lock(self) -> asyncio.Lock:
        if self.lock is None:
            self.lock = asyncio.Lock()
        return self.lock


_registry = _Registry()


def get_resident(name: str) -> Optional[Any]:
    """The live backend for ``name``, or None if it is not hot here.

    Does NOT consider the primary — callers resolve that through
    ``factory.get_backend()`` first, since it is reached differently.
    """
    entry = _registry.entries.get(name)
    if entry is None:
        return None
    entry.requests += 1
    return entry.backend


def is_resident(name: str) -> bool:
    return name in _registry.entries


def list_resident() -> list:
    """Snapshot for the ``models`` query / telemetry."""
    return [
        {
            "name": e.name,
            "footprintBytes": e.footprint_bytes,
            "loadedAt": e.loaded_at,
            "requests": e.requests,
        }
        for e in _registry.entries.values()
    ]


def resident_bytes() -> int:
    """What this registry currently holds — secondaries only."""
    return sum(e.footprint_bytes for e in _registry.entries.values())


def admission_check(config: Any) -> tuple[bool, str, dict]:
    """May ``config`` be loaded alongside what is already hot?

    Counts the PRIMARY too: it is the largest resident model in every
    realistic deployment, and a governor that ignored it would admit a
    second model into memory the primary already owns.
    """
    from inference.backends import factory

    limit = wired_limit_bytes()
    want = estimate_footprint_bytes(config)

    primary_bytes = 0
    primary = factory.get_backend()
    if primary is not None:
        try:
            primary_bytes = estimate_footprint_bytes(primary.config)
        except (ValueError, AttributeError) as exc:
            # Unknown primary cost is not a licence to guess zero.
            return (
                False,
                f"cannot size the resident primary ({exc}) — refusing to admit "
                f"{config.model.name} against an unknown budget",
                {},
            )

    used = primary_bytes + resident_bytes()
    budget = limit - HEADROOM_BYTES
    facts = {
        "limitBytes": limit,
        "headroomBytes": HEADROOM_BYTES,
        "primaryBytes": primary_bytes,
        "residentBytes": resident_bytes(),
        "requestedBytes": want,
        "budgetBytes": budget,
    }
    if used + want > budget:
        gb = 1024**3
        return (
            False,
            (
                f"{config.model.name} needs {want / gb:.1f} GB; "
                f"{used / gb:.1f} GB already hot (primary {primary_bytes / gb:.1f} "
                f"+ resident {resident_bytes() / gb:.1f}) against a "
                f"{budget / gb:.1f} GB budget "
                f"({limit / gb:.1f} GB wired limit − {HEADROOM_BYTES / gb:.1f} GB "
                f"headroom). Unload something or lower its n_ctx."
            ),
            facts,
        )
    return True, "", facts


async def load(name: str) -> Any:
    """Make ``name`` hot as a secondary, or return it if it already is.

    Never loads the active primary — that model is already served, and a
    second copy of it is exactly the memory bomb the governor exists to
    prevent.
    """
    import time

    from core import model_registry
    from core.config import Config, resolve_config_path
    from inference.backends.factory import create_backend

    if name in _registry.burned:
        raise RuntimeError(
            f"{name!r} previously failed to unload — its C contexts and wired "
            f"memory may still be allocated, and loading again would stack a "
            f"second copy on top. Restart the process."
        )

    async with _registry.get_lock():
        existing = _registry.entries.get(name)
        if existing is not None:
            return existing.backend

        if name == model_registry.active_name():
            raise ValueError(
                f"{name!r} is the active primary — it is already served; "
                f"loading it here would allocate a second copy"
            )

        path = resolve_config_path(name)
        if path is None:
            raise KeyError(f"unknown model config {name!r} — see the models query")
        import yaml

        with open(path, encoding="utf-8") as fh:
            cfg = Config(**yaml.safe_load(fh))

        ok, why, facts = admission_check(cfg)
        if not ok:
            raise MemoryError(why)

        log.info(
            "🔥 resident load %s (%.1f GB est, %.1f GB budget free)",
            name,
            facts["requestedBytes"] / 1024**3,
            (facts["budgetBytes"] - facts["primaryBytes"] - facts["residentBytes"])
            / 1024**3,
        )
        backend = create_backend(cfg)
        try:
            await backend.initialize()
        except Exception:
            # Init failed — best-effort teardown so a retry is not stacking.
            try:
                await backend.shutdown()
            except Exception as texc:  # noqa: BLE001
                _registry.burned.add(name)
                log.error(
                    "resident %s: init failed AND teardown failed (%s) — name "
                    "burned for this process",
                    name,
                    texc,
                )
            raise

        _registry.entries[name] = ResidentEntry(
            name=name,
            backend=backend,
            config=cfg,
            footprint_bytes=facts["requestedBytes"],
            loaded_at=time.time(),
        )
        return backend


async def unload(name: str) -> None:
    """Tear one secondary down and free its wired memory."""
    async with _registry.get_lock():
        entry = _registry.entries.pop(name, None)
        if entry is None:
            raise KeyError(f"{name!r} is not resident")
        try:
            await entry.backend.shutdown()
        except Exception as exc:  # noqa: BLE001 — the latch is the whole point
            _registry.burned.add(name)
            log.error(
                "❌ resident %s: shutdown FAILED (%s) — wired memory may be "
                "orphaned; the name is burned for this process",
                name,
                exc,
            )
            raise
        log.info("resident unload %s (%.1f GB)", name, entry.footprint_bytes / 1024**3)


async def shutdown_all() -> None:
    """Free every secondary. Called from the server's shutdown path BEFORE
    the primary goes down, so a failure here is visible while there is still
    a process to report it."""
    for name in list(_registry.entries):
        try:
            await unload(name)
        except Exception as exc:  # noqa: BLE001 — keep freeing the rest
            log.error("resident shutdown_all: %s failed (%s)", name, exc)


def _reset_for_tests() -> None:
    """Drop all state without touching backends. Tests only."""
    _registry.entries.clear()
    _registry.burned.clear()
    _registry.lock = None
