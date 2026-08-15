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
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("llm-mvp")

# Headroom the governor never spends on UNIFIED memory. The OS, the page cache
# and every non-LLMVP process live in the same pool; a governor that admits
# right up to the iogpu limit hands the machine a hard reboot rather than a
# refusal. Apple's own guidance for recommendedMaxWorkingSetSize is to leave
# 8-16 GB, and three hard reboots (2026-07-24) are why this is 8 and not 4.
HEADROOM_BYTES = 8 * 1024**3

# Headroom on a DISCRETE card, where VRAM is the GPU's alone. Nothing else on
# the machine competes for it, so the reserve only has to cover allocator
# fragmentation and the compute buffers ggml sizes after admission. Applying
# the unified figure here would sterilise 8 of the 3060's 12 GB — the reserve
# would be larger than most of what we want to put on it.
DISCRETE_HEADROOM_BYTES = 1 * 1024**3

# Fallback when sysctl cannot be read. This machine runs
# iogpu.wired_limit_mb=116000; the fallback is deliberately the same number
# so a sysctl failure does not silently widen the budget.
DEFAULT_WIRED_LIMIT_BYTES = 116_000 * 1024**2

# GGMLBackendDevType.GGML_BACKEND_DEVICE_TYPE_GPU. Enumerating by type rather
# than by name keeps this backend-agnostic: Metal, CUDA, ROCm and Vulkan all
# register through the same device table.
_GGML_DEV_TYPE_GPU = 1

# Sentinel device for anything held in host RAM rather than on a card, so a
# CPU-offloaded projector is still PRICED without being charged against a
# GPU's budget. Never an index into gpu_devices().
_CPU_DEVICE = -1

_GGML_LIB = None


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


def is_unified_memory() -> bool:
    """One pool shared with the whole machine, rather than private VRAM.

    Decides which headroom applies and whether the sysctl ceiling is
    meaningful. Apple silicon is the only unified target we run on; a
    discrete card's VRAM belongs to the GPU alone.
    """
    return sys.platform == "darwin"


def _ggml() -> Any:
    """The already-loaded ggml/llama shared library, signatures configured.

    ``ggml_backend_dev_memory`` is exported from the libggml-base.so that
    llama-cpp-python loads at import, and reachable through the llama handle
    because libllama links it. The Python binding does not wrap it — only four
    ggml_backend symbols are bound, none of them device enumeration — so the
    prototypes are declared here against that same handle. No second dlopen,
    no vendored header.
    """
    global _GGML_LIB
    if _GGML_LIB is not None:
        return _GGML_LIB
    import ctypes

    import llama_cpp.llama_cpp as C

    lib = C._lib
    lib.ggml_backend_dev_count.restype = ctypes.c_size_t
    lib.ggml_backend_dev_get.restype = ctypes.c_void_p
    lib.ggml_backend_dev_get.argtypes = [ctypes.c_size_t]
    lib.ggml_backend_dev_name.restype = ctypes.c_char_p
    lib.ggml_backend_dev_name.argtypes = [ctypes.c_void_p]
    lib.ggml_backend_dev_type.restype = ctypes.c_int
    lib.ggml_backend_dev_type.argtypes = [ctypes.c_void_p]
    lib.ggml_backend_dev_memory.restype = None
    lib.ggml_backend_dev_memory.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _GGML_LIB = lib
    return lib


def gpu_devices() -> list[dict]:
    """Every GPU ggml can see, in ``main_gpu`` order.

    The list is filtered to GPU-type devices and kept in enumeration order,
    because that is exactly what llama.cpp's ``main_gpu`` indexes — a config
    saying ``main_gpu: 1`` means the second GPU, not the second ggml device
    (the CPU device also appears in that table).

    Empty when ggml is unreachable or reports no GPU, which callers must read
    as "cannot size devices" and fall back, never as "no memory".
    """
    import ctypes

    try:
        lib = _ggml()
        out: list[dict] = []
        for i in range(int(lib.ggml_backend_dev_count())):
            dev = lib.ggml_backend_dev_get(i)
            if int(lib.ggml_backend_dev_type(dev)) != _GGML_DEV_TYPE_GPU:
                continue
            free = ctypes.c_size_t()
            total = ctypes.c_size_t()
            lib.ggml_backend_dev_memory(dev, ctypes.byref(free), ctypes.byref(total))
            out.append(
                {
                    "index": len(out),
                    "name": lib.ggml_backend_dev_name(dev).decode(errors="replace"),
                    "free_bytes": int(free.value),
                    "total_bytes": int(total.value),
                }
            )
        return out
    except Exception as exc:  # noqa: BLE001 — any failure means "cannot size"
        log.warning("resident: could not enumerate ggml devices (%s)", exc)
        return []


def headroom_bytes(device: int = 0) -> int:
    """Memory the governor refuses to spend on ``device``.

    Unified and discrete are different physics, not different tunings of one
    number. On unified memory the reserve protects the OS and every other
    process sharing the pool, and over-admitting reboots the machine. On a
    discrete card nothing else is in the pool, so the reserve only covers
    fragmentation and the compute buffers ggml sizes after admission.
    """
    return HEADROOM_BYTES if is_unified_memory() else DISCRETE_HEADROOM_BYTES


def device_capacity_bytes(device: int = 0) -> int:
    """Total memory of ``device``, before headroom.

    TOTAL, not free, because the caller tracks what it has already admitted —
    charging against free memory would count the resident primary twice.

    ON METAL THIS IS THE SAME CEILING THE SYSCTL REPORTS. ggml's Metal device
    reports recommendedMaxWorkingSetSize, and that value is itself raised by
    iogpu.wired_limit_mb — so switching to the device API is not a widening of
    the M1's budget. The sysctl is kept as a clamp because Apple documents
    recommendedMaxWorkingSetSize as "an approximation ... without affecting
    runtime performance" rather than a hard ceiling, and the lower of the two
    is the honest number.
    """
    devices = gpu_devices()
    if not devices:
        # No GPU table: the pre-2026-08 behaviour, one pool sized by sysctl.
        return wired_limit_bytes()
    idx = min(max(int(device), 0), len(devices) - 1)
    total = devices[idx]["total_bytes"]
    if is_unified_memory():
        return min(total, wired_limit_bytes())
    return total


def model_device(config: Any) -> int:
    """The GPU this config's weights and KV land on (``main_gpu``, else 0)."""
    dev = getattr(config.model, "main_gpu", None)
    return int(dev) if dev is not None else 0


def footprint_by_device(config: Any) -> dict:
    """Bytes this config will claim, keyed by the device that actually pays.

    Weights and KV follow ``main_gpu``. THE PROJECTOR DOES NOT, and that
    split is the whole reason this returns a mapping instead of a number.

    ``_create_vision_instance`` builds its context from the primary's model
    (placed correctly), but the mtmd handler that owns the projector takes
    only ``use_gpu`` — a bool, with no device index anywhere in its signature
    — so mtmd allocates on the default device, which is device 0. Charging
    the projector to the model's device therefore under-counts device 0 by
    exactly the amount that will fail to allocate there. Live: paddle pinned
    to CUDA1 was admitted against a budget that never saw its 840.90 MiB
    projector, which then tried to land on CUDA0 beside muse and killed the
    server with cudaMalloc OOM.

    On a single-GPU host every term keys to 0 and this is the old arithmetic.
    """
    from inference.backends.llama_cpp_backend import LlamaCppBackend

    mcfg = config.model
    dev = model_device(config)
    by_dev: dict = {}

    def charge(device: int, amount: int) -> None:
        by_dev[device] = by_dev.get(device, 0) + int(amount)

    # probe_verified_weights_bytes, when the config declares it, is a MEASURED
    # figure and beats the file-size upper bound.
    verified = getattr(mcfg, "probe_verified_weights_bytes", None)
    if verified:
        charge(dev, int(verified))
    else:
        try:
            charge(dev, LlamaCppBackend.weights_bytes_total(str(mcfg.path)))
        except OSError as exc:
            raise ValueError(f"cannot size weights at {mcfg.path}: {exc}") from exc

    mmproj = getattr(mcfg, "mmproj_path", None)
    if mmproj:
        # Counted SEPARATELY on purpose: weights_bytes_total deliberately
        # excludes a sibling mmproj-*.gguf, and probe_verified_weights_bytes
        # was measured without it too. Charged to device 0 — see above.
        try:
            charge(
                0 if projector_on_gpu(config) else _CPU_DEVICE,
                os.path.getsize(str(mmproj)),
            )
        except OSError:
            log.warning("resident: mmproj %s unreadable — not counted", mmproj)

    from core.context_probe import kv_bytes_per_token

    try:
        per_tok = kv_bytes_per_token(
            str(mcfg.path), getattr(mcfg, "probe_verified_kv_bytes_per_token", None)
        )
        charge(dev, per_tok * int(mcfg.n_ctx))
        # THE VISION POOL IS REAL KV and must be priced. Each pool member is a
        # private context of its own — paddle at vision_n_ctx 32768 x 4 is
        # 2.25 GB, which is larger than its weights and projector combined.
        # Charging only the text n_ctx would let the governor admit a model
        # whose actual footprint it never saw. Counted whether or not vision
        # has been used yet: the pool is built on first request and never
        # released, so "not yet allocated" is a timing detail, not a saving.
        #
        # The CONTEXTS follow main_gpu even though the projector does not:
        # they are built from the primary's model with ordinary context
        # params, and only the mtmd projector escapes placement.
        if mmproj:
            width = max(1, int(getattr(mcfg, "vision_pool_size", 1) or 1))
            charge(
                dev,
                per_tok * int(getattr(mcfg, "vision_n_ctx", 8192) or 8192) * width,
            )
    except Exception as exc:  # noqa: BLE001 — header shapes vary by arch
        raise ValueError(
            f"cannot compute KV geometry for {mcfg.name}: {exc} — "
            "declare probe_verified_kv_bytes_per_token or run --probe-context"
        ) from exc
    return by_dev


def projector_on_gpu(config: Any) -> bool:
    """Whether the mtmd projector is offloaded (config ``vision_projector_gpu``)."""
    val = getattr(config.model, "vision_projector_gpu", None)
    return True if val is None else bool(val)


def estimate_footprint_bytes(config: Any) -> int:
    """Upper-bound resident cost of one model: weights + projector + KV.

    Every term is an over-estimate on purpose. ``weights_bytes_total`` is
    FILE size (an MoE under mmap pages in only what it routes to), and KV is
    computed full-size because ``swa_full`` configs hold both caches. A
    governor that guesses low reboots the machine; one that guesses high
    refuses a load that might have fit, and the operator can lower n_ctx.

    The scalar TOTAL across every device, kept for reporting (``footprintGb``)
    and for the single-pool fallback. Admission uses ``footprint_by_device``.
    """
    return sum(footprint_by_device(config).values())


@dataclass
class ResidentEntry:
    """One hot secondary model."""

    name: str
    backend: Any
    config: Any
    footprint_bytes: int
    loaded_at: float = 0.0
    requests: int = 0
    # What this entry costs EACH device, so admission can charge the right
    # pool. Empty for entries created before per-device accounting; callers
    # fall back to charging footprint_bytes wholly to device 0, which is the
    # correct reading on a single-GPU host.
    footprint_by_device: dict = field(default_factory=dict)


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


def resident_bytes_by_device() -> dict:
    """Registry footprint keyed by device.

    An entry loaded before per-device accounting has no breakdown; its whole
    footprint is charged to device 0, which is exactly right on a single-GPU
    host and conservative anywhere else.
    """
    out: dict = {}
    for e in _registry.entries.values():
        split = e.footprint_by_device or {0: e.footprint_bytes}
        for dev, amount in split.items():
            out[dev] = out.get(dev, 0) + int(amount)
    return out


def _merge_devices(*maps: dict) -> dict:
    out: dict = {}
    for m in maps:
        for dev, amount in (m or {}).items():
            out[dev] = out.get(dev, 0) + int(amount)
    return out


def admission_check(config: Any) -> tuple[bool, str, dict]:
    """May ``config`` be loaded alongside what is already hot?

    Counts the PRIMARY too: it is the largest resident model in every
    realistic deployment, and a governor that ignored it would admit a
    second model into memory the primary already owns.

    PER DEVICE, because memory is not one pool. This box has 24 GB on the
    3090 and 12 GB on the 3060 and a model declares which it wants via
    ``main_gpu``; summing them into a single ceiling admits a load that
    cannot fit anywhere. On a single-GPU host — every Metal machine — there
    is one device, every term keys to 0, and the arithmetic below is
    byte-identical to what it replaced.
    """
    from inference.backends import factory

    name = config.model.name
    if str(getattr(config.model, "split_mode", "none") or "none").lower() != "none":
        # A layer/row split spreads one model over every card, so there is no
        # single device to charge and the per-device question is malformed.
        # Refused rather than guessed: no config uses it, and silently
        # charging it to main_gpu would under-count every other device.
        return (
            False,
            f"{name} declares split_mode "
            f"{getattr(config.model, 'split_mode')!r}: the memory governor "
            f"can only size a model pinned to one device (split_mode: none). "
            f"Pin it, or load it as the primary where no admission runs.",
            {},
        )

    try:
        want_by_dev = footprint_by_device(config)
    except ValueError as exc:
        return False, str(exc), {}

    primary_by_dev: dict = {}
    primary = factory.get_backend()
    if primary is not None:
        try:
            primary_by_dev = footprint_by_device(primary.config)
        except (ValueError, AttributeError) as exc:
            # Unknown primary cost is not a licence to guess zero.
            return (
                False,
                f"cannot size the resident primary ({exc}) — refusing to admit "
                f"{name} against an unknown budget",
                {},
            )

    used_by_dev = _merge_devices(primary_by_dev, resident_bytes_by_device())
    gb = 1024**3
    facts = {
        "primaryBytes": sum(primary_by_dev.values()),
        "residentBytes": resident_bytes(),
        "requestedBytes": sum(want_by_dev.values()),
        "perDevice": {},
    }

    for dev, want in sorted(want_by_dev.items()):
        if dev == _CPU_DEVICE:
            # Host RAM. Priced in the report so the figure reconciles, but not
            # gated: a CPU-offloaded projector competes with the page cache,
            # not with a card, and this governor exists to protect cards.
            facts["perDevice"]["cpu"] = {"requestedBytes": want}
            continue
        capacity = device_capacity_bytes(dev)
        headroom = headroom_bytes(dev)
        budget = capacity - headroom
        used = used_by_dev.get(dev, 0)
        facts["perDevice"][str(dev)] = {
            "capacityBytes": capacity,
            "headroomBytes": headroom,
            "budgetBytes": budget,
            "usedBytes": used,
            "requestedBytes": want,
        }
        if used + want > budget:
            devices = gpu_devices()
            label = devices[dev]["name"] if dev < len(devices) else f"device {dev}"
            return (
                False,
                (
                    f"{name} needs {want / gb:.1f} GB on {label}; "
                    f"{used / gb:.1f} GB already hot there against a "
                    f"{budget / gb:.1f} GB budget "
                    f"({capacity / gb:.1f} GB capacity − {headroom / gb:.1f} GB "
                    f"headroom). Unload something on that device, lower its "
                    f"n_ctx or vision_pool_size, or pin it elsewhere with "
                    f"main_gpu."
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

        # PER DEVICE, because one aggregate number is what let this go wrong:
        # "77.5 GB budget free" was reported on a box whose largest pool is
        # 24 GB, and the load it green-lit killed the process.
        gb = 1024**3
        where = ", ".join(
            f"{'cpu' if k == 'cpu' else 'dev' + k} "
            f"{v['requestedBytes'] / gb:.1f}GB"
            + (
                ""
                if k == "cpu"
                else f"/{(v['budgetBytes'] - v['usedBytes']) / gb:.1f}GB free"
            )
            for k, v in sorted(facts.get("perDevice", {}).items())
        )
        log.info(
            "🔥 resident load %s (%.1f GB est — %s)",
            name,
            facts["requestedBytes"] / gb,
            where or "no device breakdown",
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
            # Recomputed rather than lifted from `facts`, whose perDevice map
            # is display-shaped (string keys, cpu bucket). The next admission
            # charges against THIS, so it has to be the real breakdown.
            footprint_by_device=footprint_by_device(cfg),
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
