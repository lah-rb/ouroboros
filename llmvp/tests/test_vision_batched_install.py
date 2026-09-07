"""Batched-vision install: the multimodal prefix machinery.

Pins the contracts probe_vision_kv_integrity.py proved on hardware
(2026-08-26, dev/BATCHED_VISION_2026-08-26.md): text and image rows land
on the SEAT's seq at sequential positions with no logits requested;
image rows appear in slot.input_ids only as the negative MEDIA_SENTINEL
(never a real id — a sentinel that reaches detokenize or a replay must
fail loudly, not decode as garbage); has_media marks the seat
non-snapshottable; prepare_seat clears the mark and serves the synthetic
EMPTY vision persona head without touching any band seq; and the install
orchestrator enforces the position law for atomic (M-RoPE-class)
installs instead of silently corrupting occupancy.
"""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import Future

import pytest

from inference.batched_engine import BatchedEngine, PersonaHead, SeqSlot
from inference.vision_batched import (
    MediaChunk,
    SplitPrompt,
    VisionInstallError,
    install_multimodal_prefix,
)
from tests.test_batched_engine import FakeCtx, _engine_with


def _mk_engine(decode_script=None):
    ctx = FakeCtx(decode_script=decode_script or [0] * 64)
    eng = _engine_with(ctx, samplers={}, n_batch=8, seats=2)
    return eng, ctx


def _bare_slot(seq=0):
    s = SeqSlot(seq=seq, _n_ctx=4096)
    s.n_tokens = 0
    s.input_ids = []
    return s


# ── eval_tokens_on_slot ──────────────────────────────────────────────


def test_text_install_rows_positions_and_chunking():
    eng, ctx = _mk_engine()
    slot = _bare_slot(seq=1)
    toks = list(range(100, 119))  # 19 tokens at n_batch 8 -> 3 decodes
    eng.eval_tokens_on_slot(slot, toks)
    assert slot.n_tokens == 19
    assert slot.input_ids == toks
    assert slot.has_media is False
    assert len(ctx.decoded_batches) == 3
    flat = [r for b in ctx.decoded_batches for r in b]
    assert [r[0] for r in flat] == toks
    assert [r[1] for r in flat] == list(range(19))  # sequential positions
    assert all(r[2] == (1,) for r in flat)  # the SEAT's seq
    assert not any(r[3] for r in flat)  # no logits during install


def test_text_install_decode_failure_raises_and_stops():
    eng, ctx = _mk_engine(decode_script=[0, 2])
    slot = _bare_slot()
    with pytest.raises(RuntimeError, match="text decode ret=2"):
        eng.eval_tokens_on_slot(slot, list(range(16)))
    # the first sub-batch landed; the second did not book anything
    assert slot.n_tokens == 8
    assert len(slot.input_ids) == 8


# ── eval_embd_on_slot ────────────────────────────────────────────────


def test_embd_install_sentinels_and_media_mark():
    np = pytest.importorskip("numpy")
    eng, ctx = _mk_engine()
    slot = _bare_slot(seq=1)
    slot.n_tokens = 5  # text1 already installed
    slot.input_ids = [9] * 5
    n_embd, n_rows = 16, 11  # 11 rows at n_batch 8 -> 2 sub-batches
    embd = np.arange(n_rows * n_embd, dtype=np.float32)
    eng.eval_embd_on_slot(slot, embd, n_rows, n_embd)
    assert slot.n_tokens == 16
    assert slot.input_ids[:5] == [9] * 5
    assert slot.input_ids[5:] == [BatchedEngine.MEDIA_SENTINEL] * n_rows
    assert all(t < 0 for t in slot.input_ids[5:])  # never a real vocab id
    assert slot.has_media is True
    # the real LlamaBatch holds the LAST sub-batch: 3 rows at pos 13..15
    raw = eng._embd_batch.batch
    assert raw.n_tokens == 3
    assert [raw.pos[j] for j in range(3)] == [13, 14, 15]
    assert [raw.seq_id[j][0] for j in range(3)] == [1, 1, 1]
    assert [raw.logits[j] for j in range(3)] == [0, 0, 0]


# ── prepare_seat interplay ───────────────────────────────────────────


def test_prepare_seat_clears_media_and_serves_empty_vision_head():
    eng, ctx = _mk_engine()
    eng._persona_heads["vision"] = PersonaHead(name="vision", seq=-1, tokens=[])
    slot = _bare_slot(seq=0)
    slot.has_media = True
    slot.input_ids = [BatchedEngine.MEDIA_SENTINEL] * 4
    slot.n_tokens = 4
    eng.prepare_seat(slot, "vision")
    assert slot.has_media is False
    assert slot.n_tokens == 0 and slot.input_ids == []
    # the empty head must never memory_seq_cp from its (fake) seq -1
    assert not any(src == -1 for src, *_ in ctx.seq_cp_calls)


# ── install orchestrator ─────────────────────────────────────────────


class _InlineEngine:
    """control() executes the fn inline and returns a done Future —
    the decode-thread contract without the thread."""

    MEDIA_SENTINEL = BatchedEngine.MEDIA_SENTINEL

    class _MemCtx:
        """Just enough context for the install preflight: an empty seq."""

        def memory_seq_pos_max(self, seq):
            return -1

        def memory_seq_rm(self, seq, p0, p1):
            return True

    def __init__(self):
        self.ops = []
        self._llama = type("L", (), {})()
        self._llama._ctx = self._MemCtx()

    def control(self, fn):
        fut: Future = Future()
        try:
            self.ops.append(fn)
            fut.set_result(fn())
        except Exception as exc:  # noqa: BLE001 — mirror engine behaviour
            fut.set_exception(exc)
        return fut

    def eval_tokens_on_slot(self, slot, tokens):
        slot.input_ids.extend(tokens)
        slot.n_tokens += len(tokens)

    def eval_embd_on_slot(self, slot, embd, n_rows, n_embd):
        slot.input_ids.extend([self.MEDIA_SENTINEL] * n_rows)
        slot.n_tokens += n_rows
        slot.has_media = True


class _FakeEncoder:
    def __init__(self, atomic_delta=None):
        self.encoded = []
        self.atomic_delta = atomic_delta

    def encode(self, chunk, n_embd_inp):
        self.encoded.append(chunk)
        return b"embd"  # opaque; inline engine ignores it

    def decode_image_atomic(self, lctx, chunk, embd, n_past, seq_id, n_batch):
        delta = self.atomic_delta if self.atomic_delta is not None else chunk.n_tokens
        return n_past + delta


def _chunk(n_tokens, atomic=False):
    ch = MediaChunk(ptr=object(), n_tokens=n_tokens, needs_atomic=atomic)
    ch._freed = True  # no real C pointer to free
    return ch


def test_install_orchestrates_text_then_embd_then_position():
    eng = _InlineEngine()
    slot = _bare_slot(seq=1)
    split = SplitPrompt(pre=[[5, 6, 7], _chunk(10), [8, 9]], text2=[1])
    asyncio.run(install_multimodal_prefix(eng, _FakeEncoder(), slot, split, 16, 8))
    assert slot.n_tokens == 15
    assert slot.input_ids[:3] == [5, 6, 7]
    assert slot.input_ids[3:13] == [BatchedEngine.MEDIA_SENTINEL] * 10
    assert slot.input_ids[13:] == [8, 9]
    assert slot.has_media is True


def test_atomic_install_books_mrope_divergence_as_cell_debt():
    """An M-RoPE install whose n_past advance diverges from the cell count
    (paddle: a page is 1,240 cells moving n_past by 40) must keep
    input_ids POSITION-dense — exactly `delta` sentinels — and book the
    remaining cells as cell_debt so occupancy sees the physical cache.
    The original refusal here was the 2026-08-29 recheck's finding: the
    guard was the ONLY thing keeping paddle off the batched path."""
    eng = _InlineEngine()
    slot = _bare_slot(seq=1)
    split = SplitPrompt(pre=[_chunk(10, atomic=True)], text2=[1])
    asyncio.run(
        install_multimodal_prefix(eng, _FakeEncoder(atomic_delta=7), slot, split, 16, 8)
    )
    assert slot.n_tokens == 7, "position authority is new_n_past"
    assert slot.input_ids == [BatchedEngine.MEDIA_SENTINEL] * 7
    assert slot.cell_debt == 3, "10 cells - 7 positions"
    assert slot.has_media is True


def test_atomic_install_muse_shape_is_byte_identical_to_before():
    """delta == n_tokens (every 1-pos-per-token family, muse included)
    must produce EXACTLY the pre-cell-debt state: n_tokens sentinels,
    zero debt. One code path; the figtext campaign's behaviour is the
    regression fence."""
    eng = _InlineEngine()
    slot = _bare_slot(seq=1)
    split = SplitPrompt(pre=[_chunk(10, atomic=True)], text2=[1])
    asyncio.run(install_multimodal_prefix(eng, _FakeEncoder(), slot, split, 16, 8))
    assert slot.n_tokens == 10
    assert slot.input_ids == [BatchedEngine.MEDIA_SENTINEL] * 10
    assert slot.cell_debt == 0
    assert slot.has_media is True


def test_atomic_install_still_refuses_impossible_position_advances():
    """delta <= 0 or delta > n_tokens is not a divergence, it is
    corruption — the install must fail (pool fallback), never book it."""
    eng = _InlineEngine()
    slot = _bare_slot(seq=1)
    split = SplitPrompt(pre=[_chunk(10, atomic=True)], text2=[1])
    with pytest.raises(VisionInstallError, match="position law"):
        asyncio.run(
            install_multimodal_prefix(
                eng, _FakeEncoder(atomic_delta=0), slot, split, 16, 8
            )
        )
    eng2 = _InlineEngine()
    slot2 = _bare_slot(seq=1)
    split2 = SplitPrompt(pre=[_chunk(10, atomic=True)], text2=[1])
    with pytest.raises(VisionInstallError, match="position law"):
        asyncio.run(
            install_multimodal_prefix(
                eng2, _FakeEncoder(atomic_delta=11), slot2, split2, 16, 8
            )
        )


# ── run_vision_completion routing (P2) ───────────────────────────────


@pytest.mark.asyncio
async def test_flag_off_never_touches_the_batched_path(monkeypatch):
    """vision_batched=False must leave the pool path as the ONLY path —
    the cousin of test_vision_path's inertness golden."""
    from core import inference as ci

    called = {"batched": 0}

    async def _boom(*a, **k):
        called["batched"] += 1
        raise AssertionError("batched path entered with the flag off")

    monkeypatch.setattr(ci, "_run_vision_batched", _boom)

    class _Cfg:
        vision_batched = False
        mmproj_path = ""
        family = "muse-glimmer"
        name = "test"

    # No mmproj configured -> the pool path's own guard raises RuntimeError
    # BEFORE any batched consideration; the batched fn must stay uncalled.
    class _Backend:
        config = type("C", (), {"model": _Cfg()})()

    async def _gb(model=None):
        return _Backend()

    monkeypatch.setattr(ci, "_get_backend", _gb)
    with pytest.raises(RuntimeError, match="vision is not configured"):
        await ci.run_vision_completion(
            messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        )
    assert called["batched"] == 0


@pytest.mark.asyncio
async def test_batched_failure_falls_back_to_pool(monkeypatch):
    """A None from the batched helper must fall THROUGH to the pool path
    (here: reach the pool path's intake and fail on its own terms), never
    surface a batched-shaped error."""
    from core import inference as ci

    seen = {"batched": 0}

    async def _none(*a, **k):
        seen["batched"] += 1
        return None

    monkeypatch.setattr(ci, "_run_vision_batched", _none)

    class _Cfg:
        vision_batched = True
        mmproj_path = "/nonexistent/mmproj.gguf"
        family = "muse-glimmer"
        name = "test"
        vision_image_roots = []
        vision_max_image_bytes = 1024

    class _Gen:
        max_tokens_default = 32
        temperature_default = 0.0

    class _Backend:
        config = type("C", (), {"model": _Cfg(), "generation": _Gen()})()

    async def _gb(model=None):
        return _Backend()

    def _sentinel(*a, **k):
        raise RuntimeError("pool-path-reached")

    _Backend.acquire_vision_instance = _sentinel
    monkeypatch.setattr(ci, "_get_backend", _gb)
    # A tiny valid data URI gets past intake; the batched helper returns
    # None; the request must then reach the POOL path's checkout (our
    # sentinel) — proving fall-through, not error surfacing.
    uri = "data:image/png;base64," + base64.b64encode(b"png?").decode()
    with pytest.raises(RuntimeError, match="pool-path-reached"):
        await ci.run_vision_completion(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "x"},
                        {"type": "image_url", "image_url": {"url": uri}},
                    ],
                }
            ]
        )
    assert seen["batched"] == 1


def test_install_preflight_scrubs_disagreeing_kv():
    """The 2026-08-26 19:15 race: a seq holding stale KV while the slot
    reads empty must be scrubbed BEFORE row one, never decoded onto."""
    eng = _InlineEngine()
    scrubbed = {"n": 0}

    class _DirtyCtx(_InlineEngine._MemCtx):
        def memory_seq_pos_max(self, seq):
            return 2783 if scrubbed["n"] == 0 else -1

        def memory_seq_rm(self, seq, p0, p1):
            scrubbed["n"] += 1
            return True

    eng._llama._ctx = _DirtyCtx()
    slot = _bare_slot(seq=4)
    split = SplitPrompt(pre=[[5, 6], _chunk(3)], text2=[1])
    asyncio.run(install_multimodal_prefix(eng, _FakeEncoder(), slot, split, 16, 8))
    assert scrubbed["n"] == 1  # stale KV removed before any install row
    assert slot.n_tokens == 5  # 2 text + 3 media, from a clean base


# ── encoder pool (per-stream encode, 2026-08-29) ─────────────────────


@pytest.mark.asyncio
async def test_encoder_pool_leases_are_parallel_and_disjoint(monkeypatch):
    """At size 2, two concurrent leases hold DIFFERENT members — the whole
    point of the pool: encode was the serial 38% of a crop request."""
    from inference.vision_batched import MtmdEncoder, MtmdEncoderPool

    monkeypatch.setattr(MtmdEncoder, "ensure", lambda self, m: None)
    pool = MtmdEncoderPool(mmproj_path="/fake.gguf", size=2)
    await pool.ensure(object())
    async with pool.lease() as a:
        async with pool.lease() as b:
            assert a is not b


@pytest.mark.asyncio
async def test_encoder_pool_size_one_serializes(monkeypatch):
    """size=1 must behave like the old single encoder: a second lease
    waits until the first releases."""
    import asyncio

    from inference.vision_batched import MtmdEncoder, MtmdEncoderPool

    monkeypatch.setattr(MtmdEncoder, "ensure", lambda self, m: None)
    pool = MtmdEncoderPool(mmproj_path="/fake.gguf", size=1)
    await pool.ensure(object())
    order: list[str] = []

    async def first():
        async with pool.lease():
            order.append("first-in")
            await asyncio.sleep(0.02)
            order.append("first-out")

    async def second():
        await asyncio.sleep(0.005)  # start after `first` holds the lease
        async with pool.lease():
            order.append("second-in")

    await asyncio.gather(first(), second())
    assert order == ["first-in", "first-out", "second-in"]


@pytest.mark.asyncio
async def test_encoder_pool_releases_on_exception(monkeypatch):
    """A leaked lease silently shrinks the pool until vision deadlocks —
    the acquire_vision_instance failure shape. Release must survive a
    raise inside the lease."""
    from inference.vision_batched import MtmdEncoder, MtmdEncoderPool

    monkeypatch.setattr(MtmdEncoder, "ensure", lambda self, m: None)
    pool = MtmdEncoderPool(mmproj_path="/fake.gguf", size=1)
    await pool.ensure(object())
    with pytest.raises(RuntimeError):
        async with pool.lease():
            raise RuntimeError("boom")
    async with pool.lease() as enc:  # would hang forever on a leak
        assert enc is not None


@pytest.mark.asyncio
async def test_encoder_pool_ensure_is_idempotent(monkeypatch):
    from inference.vision_batched import MtmdEncoder, MtmdEncoderPool

    calls = {"n": 0}

    def _ensure(self, m):
        calls["n"] += 1

    monkeypatch.setattr(MtmdEncoder, "ensure", _ensure)
    pool = MtmdEncoderPool(mmproj_path="/fake.gguf", size=3)
    await pool.ensure(object())
    await pool.ensure(object())
    assert calls["n"] == 3, "each member once, second ensure a no-op"
