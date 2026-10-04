"""Per-turn checkpoint of a session's recurrent state — roll a turn back
without re-prefilling the session.

WHY. Hybrid/recurrent models (qwen4exp, the GDN qwens) keep part of their
memory as a fixed recurrent state that already mixes in every token it has
seen, so a tail ``memory_seq_rm`` is REFUSED — the state cannot be un-mixed.
Until this existed, dropping a turn (a degenerate span, an abandoned stream,
the turn's own reasoning) meant restoring the static blob and re-prefilling
the ENTIRE session: minutes at depth, and the reason prior-turn reasoning was
never stripped on these models (2026-09-22: a qwen4exp session walk reached
184k tokens deep by file 5 of 6, decode fell 31 → 9.7 tok/s, and the keystone
file's turn hit the context ceiling mid-thought).

HOW. llama.cpp's PARTIAL_ONLY sequence state (pinned build ea19e8d) saves and
restores ONLY the recurrent part — ~113 MiB on qwen4exp, flat in n_ctx; the
attention and indexer caches are skipped (llama-memory-hybrid-idx.cpp
state_write/state_read). Restoring it puts the recurrent cell back at the
checkpoint position (recurrent state_read_meta), after which
``memory_seq_rm(0, pos, -1)`` is no longer a partial erase of the recurrent
state and succeeds on every cache (llama-memory-recurrent.cpp seq_rm).
``memory_can_shift`` was never the gate for this: on these archs it is False
because of IMROPE positions, not because of the recurrent memory.

SHAPE. One checkpoint per instance — a pinned session owns its instance —
held in one reusable host buffer (the partial size is constant for a model).
Host mode only: ON_DEVICE keys the payload by seq inside the context and a
missing device buffer aborts the process. The store registers with the
context, so everything that invalidates the binding's own checkpoints
(memory_clear/reset, load_state, a failed decode, a context close) drops this
one too; a rollback against a checkpoint that no longer describes the live KV
is refused before anything is touched.

FAIL CLOSED. A restore that fails after its first mutation is destructive on
hybrid-idx (state_read calls state_drop and wipes the seq), so any failure
past that point resets the instance; the session's KV invariant then fails
and the next turn restores the static base and replays. Never a half-restored
KV.
"""

from __future__ import annotations

import ctypes
import enum
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("llm-mvp")

# llama.h: LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY (== the legacy SWA_ONLY). On a
# plain llama_kv_cache the flag is IGNORED and the whole sequence's KV is
# serialized (GBs at depth) — which is why "seq_rm" mode never captures state.
PARTIAL_ONLY = 1
SEQ = 0  # pool instances generate on seq 0

MODE_PARTIAL = "partial"  # hybrid/recurrent: capture the recurrent state
MODE_SEQ_RM = "seq_rm"  # attention-only memory: a tail rm is already exact
MODE_NONE = "none"


class RestoreOutcome(str, enum.Enum):
    OK = "ok"
    # Validation failed before any mutation — the KV is exactly as it was.
    REFUSED_UNTOUCHED = "refused"
    # A mutation failed part-way; the instance was reset (empty context).
    FAILED_RESET = "failed_reset"


@dataclass
class TurnCheckpoint:
    pos: int  # KV length the checkpoint describes
    nbytes: int  # partial-state size (0 in seq_rm mode)
    ctx_id: int  # id() of the LlamaContext it was taken on
    prefix_digest: bytes  # blake2b of input_ids[:pos]
    save_ms: float = 0.0


def _digest(input_ids: Any, n: int) -> bytes:
    return hashlib.blake2b(input_ids[:n].tobytes(), digest_size=16).digest()


class _Failed(RuntimeError):
    pass


class TurnCheckpointStore:
    """The one live turn checkpoint of one instance."""

    # Read by LlamaContext._invalidate_checkpoints (device-only invalidations
    # skip host-mode caches).
    on_device = False

    def __init__(self, mode: str) -> None:
        if mode not in (MODE_PARTIAL, MODE_SEQ_RM):
            raise ValueError(f"no checkpoint store for mode {mode!r}")
        self.mode = mode
        self._buf: Optional[ctypes.Array] = None
        self._ck: Optional[TurnCheckpoint] = None
        self._registered_ctx_id: Optional[int] = None
        self._context_ref = None  # set by LlamaContext._register_checkpoint_cache
        self.last_restore_ms: float = 0.0

    @property
    def checkpoint(self) -> Optional[TurnCheckpoint]:
        return self._ck

    # ── binding registration protocol ──────────────────────────────────

    def _invalidate_memory(
        self,
        seq_id: int = -1,
        suffix_start: Optional[int] = None,
        keep_seq_id: Optional[int] = None,
    ) -> None:
        """The context changed under us. Keep the checkpoint only when the
        change is a tail removal at or above its position (the rollback's own
        rm, or a generate trimming a stale tail)."""
        if self._ck is None:
            return
        if keep_seq_id is not None:
            if keep_seq_id != SEQ:
                self._ck = None
            return
        if seq_id not in (-1, SEQ):
            return
        if suffix_start is not None and self._ck.pos <= suffix_start:
            return
        self._ck = None

    def close(self) -> None:
        self._ck = None
        self._buf = None
        self._context_ref = None

    def _register(self, ctx: Any) -> None:
        if self._registered_ctx_id == id(ctx):
            return
        reg = getattr(ctx, "_register_checkpoint_cache", None)
        if reg is not None:
            reg(self)
        self._registered_ctx_id = id(ctx)

    # ── capture / restore ─────────────────────────────────────────────

    def capture(self, inst: Any) -> Optional[TurnCheckpoint]:
        """Checkpoint the instance at its current KV length. None on failure
        (nothing is mutated either way)."""
        ctx = inst._ctx
        pos = int(inst.n_tokens)
        t0 = time.perf_counter()
        self._ck = None
        nbytes = 0
        if self.mode == MODE_PARTIAL:
            size = int(ctx.get_state_seq_size_ext(SEQ, PARTIAL_ONLY))
            if size <= 0:
                return None
            if self._buf is None or len(self._buf) != size:
                self._buf = (ctypes.c_uint8 * size)()
            got = int(ctx.get_state_seq_data_ext(self._buf, size, SEQ, PARTIAL_ONLY))
            if got != size:
                log.warning("turn checkpoint: short read %d/%d — not armed", got, size)
                return None
            nbytes = size
        self._register(ctx)
        self._ck = TurnCheckpoint(
            pos=pos,
            nbytes=nbytes,
            ctx_id=id(ctx),
            prefix_digest=_digest(inst.input_ids, pos),
            save_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return self._ck

    def restore(self, inst: Any, pos: int) -> RestoreOutcome:
        """Roll the instance back to ``pos`` (the checkpoint's position)."""
        ck = self._ck
        ctx = inst._ctx
        t0 = time.perf_counter()
        # Validate with the KV untouched. Every refusal here is safe.
        if ck is None or ck.pos != pos or ck.ctx_id != id(ctx):
            return RestoreOutcome.REFUSED_UNTOUCHED
        if int(inst.n_tokens) < pos:
            return RestoreOutcome.REFUSED_UNTOUCHED
        if _digest(inst.input_ids, pos) != ck.prefix_digest:
            return RestoreOutcome.REFUSED_UNTOUCHED
        if self.mode == MODE_PARTIAL:
            if int(ctx.get_state_seq_size_ext(SEQ, PARTIAL_ONLY)) != ck.nbytes:
                return RestoreOutcome.REFUSED_UNTOUCHED
        try:
            if self.mode == MODE_PARTIAL:
                # Recurrent state first: a tail rm BEFORE it is refused on a
                # hybrid (the cell still sits past pos).
                wrote = int(
                    ctx.set_state_seq_data_ext(self._buf, ck.nbytes, SEQ, PARTIAL_ONLY)
                )
                if wrote != ck.nbytes:
                    raise _Failed(f"set_state_seq_data_ext wrote {wrote}/{ck.nbytes}")
            if ctx.memory_seq_rm(SEQ, pos, -1) is False:
                raise _Failed(f"tail rm at {pos} refused")
            pos_max = getattr(ctx, "memory_seq_pos_max", None)
            if pos_max is not None and int(pos_max(SEQ)) != pos - 1:
                raise _Failed(f"memory ends at {int(pos_max(SEQ))}, expected {pos - 1}")
        except Exception as e:  # noqa: BLE001 — every failure here resets
            log.error("⏪ turn rollback to %d FAILED (%s) — resetting instance", pos, e)
            self._ck = None
            inst.reset()
            return RestoreOutcome.FAILED_RESET
        inst.n_tokens = pos
        # The binding's per-eval caches describe the dropped tail; the next
        # step is always an eval, but nothing stale may survive to it.
        for attr, val in (
            ("_last_eval_output_start", 0),
            ("_last_eval_output_count", 0),
            ("_restored_logits", None),
            ("_prefilled_prompt", None),
        ):
            if hasattr(inst, attr):
                setattr(inst, attr, val)
        # Our own set/rm calls invalidated us through the registration; the
        # checkpoint still describes [0, pos) exactly — re-arm it.
        self._ck = ck
        self.last_restore_ms = (time.perf_counter() - t0) * 1000.0
        return RestoreOutcome.OK
