"""Batched single-context decode engine (decode_mode: "batched").

Stage 0 coverage: config preconditions, seq-map arithmetic, engine
inbox/pause plumbing (no model, no llama_cpp import). The step loop's
behavior tests land with Stage 1 (FakeCtx/FakeSampler).
"""

from __future__ import annotations

import pytest

from core.config import Config
from inference.batched_engine import (
    BatchedEngine,
    OutputBridge,
    RetriableEngineError,
    SeqSlot,
    StreamPhase,
    plan_seq_map,
)

# ── config preconditions ──────────────────────────────────────────────


def _cfg(decode_mode="pool", model_extra=None, resources_extra=None) -> dict:
    return {
        "app": {"host": "0.0.0.0", "port": 1, "log_level": "info"},
        "model": {
            "name": "m",
            "family": "harmony",
            "path": "/nonexistent.gguf",
            "n_ctx": 4096,
            "n_gpu_layers": 0,
            "seed": -1,
            "verbose": False,
            **(model_extra or {}),
        },
        "prompt": {"persona_file": "./knowledge/SOUL.md"},
        "generation": {},
        "knowledge": {"tokens_bin": "./data/m.tokens.bin", "token_limit": 1024},
        "resources": {
            "cpu_threads": 1,
            "max_concurrent_requests": 2,
            "decode_mode": decode_mode,
            **(resources_extra or {}),
        },
        "logging": {"enabled": False},
    }


_BATCHED_MODEL_FLAGS = {
    "resident_seq_cache": True,
    "swa_full": True,
    "kv_unified": True,
}


def test_decode_mode_defaults_to_pool_and_old_configs_load():
    c = Config.model_validate(_cfg())
    assert c.resources.decode_mode == "pool"
    # A config dict without the key at all (every existing yaml) must load.
    raw = _cfg()
    del raw["resources"]["decode_mode"]
    assert Config.model_validate(raw).resources.decode_mode == "pool"


def test_batched_requires_resident_stack():
    with pytest.raises(ValueError, match="resident_seq_cache"):
        Config.model_validate(_cfg("batched"))
    # Each missing flag is named.
    with pytest.raises(ValueError, match="kv_unified"):
        Config.model_validate(
            _cfg("batched", {"resident_seq_cache": True, "swa_full": True})
        )
    # Full stack passes.
    c = Config.model_validate(_cfg("batched", _BATCHED_MODEL_FLAGS))
    assert c.resources.decode_mode == "batched"


def test_batched_rejects_speculative():
    with pytest.raises(ValueError, match="speculative"):
        Config.model_validate(
            _cfg("batched", {**_BATCHED_MODEL_FLAGS, "speculative": True})
        )


def test_batched_warns_and_keeps_slot_personas(caplog):
    import logging as _logging

    with caplog.at_level(_logging.WARNING):
        c = Config.model_validate(
            _cfg(
                "batched",
                _BATCHED_MODEL_FLAGS,
                {"slot_personas": ["default", "default"]},
            )
        )
    assert c.resources.decode_mode == "batched"
    assert any("ignores resources.slot_personas" in r.message for r in caplog.records)


def test_unknown_decode_mode_raises():
    with pytest.raises(ValueError, match="decode_mode"):
        Config.model_validate(_cfg("turbo"))


# ── seq map ───────────────────────────────────────────────────────────


def test_seq_map_layout_and_bounds():
    m = plan_seq_map(2, ["user_sim"], ["low", "high"])
    assert list(m.working_seqs()) == [0, 1]
    assert m.persona_seqs == {"default": 2, "user_sim": 3}
    assert m.reasoning_seqs == {"low": 4, "high": 5}
    assert m.n_seq_max == 6


def test_seq_map_default_dedup_and_minimums():
    # "default" passed explicitly must not double-allocate.
    m = plan_seq_map(1, ["default"], [])
    assert m.persona_seqs == {"default": 1}
    assert m.n_seq_max == 2
    with pytest.raises(ValueError, match="n_working"):
        plan_seq_map(0, [], [])


# ── engine inbox plumbing (no model) ──────────────────────────────────


def _mk_engine() -> BatchedEngine:
    return BatchedEngine(llama=None, seq_map=plan_seq_map(2, [], []), n_batch=8)


def test_control_ops_run_on_decode_thread_and_return_futures():
    import threading

    eng = _mk_engine()
    eng.start()
    try:
        ran_on = eng.control(lambda: threading.current_thread().name).result(5)
        assert ran_on == "llmvp-decode"
        boom = eng.control(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            boom.result(5)
    finally:
        eng.shutdown()


def test_pause_parks_at_step_boundary_and_resume_continues():
    eng = _mk_engine()
    eng.start()
    try:
        eng.pause(timeout=5)
        # Control ops queued during pause run after resume.
        fut = eng.control(lambda: "post-resume")
        eng.resume()
        assert fut.result(5) == "post-resume"
    finally:
        eng.shutdown()


def test_shutdown_rejects_new_work_and_fails_streams():
    eng = _mk_engine()
    eng.start()
    eng.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        eng.control(lambda: None)
    assert isinstance(RetriableEngineError("x"), RuntimeError)


def test_health_shape():
    eng = _mk_engine()
    h = eng.health()
    assert h["decode_mode"] == "batched"
    assert h["active_streams"] == 0
    for key in (
        "engine_steps",
        "prefill_budget",
        "kv_pressure_events",
        "kv_evictions",
        "engine_fatal",
    ):
        assert key in h


# ── SeqSlot telemetry contract ────────────────────────────────────────


def test_seqslot_duck_types_instance_telemetry_surface():
    """core/inference.py + session_manager read these attrs off the handle
    after a generate call — the SeqSlot facade must carry every one."""
    slot = SeqSlot(seq=0)
    for attr in (
        "n_tokens",
        "_n_ctx",
        "input_ids",
        "_needs_context_refresh",
        "_last_completion_tokens",
        "_last_gen_start_pos",
        "_last_kv_base",
        "_last_dynamic_len",
        "_last_flow_hit",
        "_last_flow_key",
        "_last_cache_hit",
    ):
        assert hasattr(slot, attr), attr
    assert StreamPhase.PREFILL is not StreamPhase.DECODING


# ── step loop with fakes (no llama_cpp, no thread — drive _admit/_step) ─


class FakeCtx:
    """Records KV ops; decode returns scripted codes (default 0)."""

    def __init__(self, decode_script=None):
        self.decode_script = list(decode_script or [])
        self.decoded_batches = []  # list of row tuples per decode call
        self.seq_rm_calls = []
        self.seq_cp_calls = []
        self.seq_add_calls = []

    def decode(self, batch):
        self.decoded_batches.append(list(batch.rows))
        if self.decode_script:
            code = self.decode_script.pop(0)
            if isinstance(code, Exception):
                raise code
            return code
        return 0

    def memory_seq_rm(self, seq, p0, p1):
        self.seq_rm_calls.append((seq, p0, p1))

    def memory_seq_cp(self, src, dst, p0, p1):
        self.seq_cp_calls.append((src, dst, p0, p1))

    def memory_seq_add(self, seq, p0, p1, delta):
        self.seq_add_calls.append((seq, p0, p1, delta))


class FakeBatch:
    def __init__(self):
        self.rows = []

    def reset(self):
        self.rows = []

    def add_token(self, token, pos, seq_ids, logits):
        self.rows.append((token, pos, tuple(seq_ids), bool(logits)))

    def n_tokens(self):
        return len(self.rows)


class FakeSampler:
    """Pops tokens from a script; records accepts."""

    def __init__(self, script):
        self.script = list(script)
        self.accepted = []
        self.sampled_at = []  # idx values used

    def sample(self, ctx, idx):
        self.sampled_at.append(idx)
        return self.script.pop(0)

    def accept(self, token, accept_grammar):
        self.accepted.append((token, accept_grammar))


class FakeLlama:
    def __init__(self, ctx):
        self._ctx = ctx
        self._seed = 42
        self._n_ctx = 4096

    def detokenize(self, tokens, prev_tokens=None, special=False):
        # token N -> b"<N>"; token 999 -> invalid UTF-8 continuation byte
        out = b""
        for t in tokens:
            out += b"\xa9" if t == 999 else f"<{t}>".encode()
        return out


class FakeBridge:
    def __init__(self):
        self.chunks = []
        self.error = None
        self.done = False
        self.closed = False

    def emit(self, text):
        self.chunks.append(text)

    def finish(self, error=None):
        self.error = error
        self.done = True


EOG = 7777


def _engine_with(
    ctx,
    *,
    samplers,
    n_batch=64,
    prefill_chunk=None,
    seats=2,
    capture_dir="/tmp/llmvp-test-captures",
):
    from inference.batched_engine import BatchedEngine, PersonaHead

    heads = {"default": PersonaHead(name="default", seq=seats, tokens=[1, 2])}
    eng = BatchedEngine(
        FakeLlama(ctx),
        plan_seq_map(seats, [], []),
        n_batch=n_batch,
        prefill_chunk=prefill_chunk,
        persona_heads=heads,
        capture_dir=capture_dir,
        sampler_factory=lambda req: samplers[req.request_id],
        is_eog=lambda tok: tok == EOG,
    )
    eng._batch = FakeBatch()
    eng._repetition_guard_factory = lambda: None  # guards tested separately
    eng._long_cycle_enabled = False
    return eng


def _slot(seq, n_tokens=2):
    s = SeqSlot(seq=seq, _n_ctx=4096)
    s.n_tokens = n_tokens
    s.static_len = n_tokens
    s.input_ids = [1, 2][:n_tokens]
    return s


def _req(rid, prompt, max_tokens=8, slot=None, stop_texts=None):
    from inference.batched_engine import StreamRequest

    return StreamRequest(
        prompt_tokens=list(prompt),
        max_tokens=max_tokens,
        sampling_kwargs={"temp": 0.0},
        out=FakeBridge(),
        slot=slot or _slot(0),
        stop_texts=stop_texts or [],
        request_id=rid,
    )


def test_single_stream_prefill_decode_eog_and_telemetry():
    ctx = FakeCtx()
    sampler = FakeSampler([10, 11, EOG])
    eng = _engine_with(ctx, samplers={"a": sampler})
    req = _req("a", [100, 101, 102])
    req._stream_id = "a"
    eng._admit(req)

    eng._step()  # prefill all 3 tokens (positions 2,3,4), logits on last
    assert ctx.decoded_batches[0] == [
        (100, 2, (0,), False),
        (101, 3, (0,), False),
        (102, 4, (0,), True),
    ]
    assert sampler.sampled_at == [2]  # last prompt row

    eng._step()  # feeds tok 10 at pos 5, samples 11
    assert ctx.decoded_batches[1] == [(10, 5, (0,), True)]
    eng._step()  # feeds 11, samples EOG -> retire
    assert req.out.done and req.out.error is None
    assert "".join(req.out.chunks) == "<10><11>"

    slot = req.slot
    assert slot._last_completion_tokens == [10, 11]
    assert slot._last_gen_start_pos == 5  # static 2 + prompt 3
    assert slot._last_dynamic_len == 3
    # KV parity: EOG was sampled but never fed — n_tokens excludes it.
    assert slot.n_tokens == 7  # 2 static + 3 prompt + 2 decoded
    assert slot.input_ids == [1, 2, 100, 101, 102, 10, 11]
    assert not eng._streams


def test_prefill_interleaves_with_decoding_stream():
    ctx = FakeCtx()
    s_a, s_b = FakeSampler([10, 11, 12, EOG]), FakeSampler([20, EOG])
    eng = _engine_with(ctx, samplers={"a": s_a, "b": s_b}, prefill_chunk=2)

    ra = _req("a", [100], slot=_slot(0))
    ra._stream_id = "a"
    eng._admit(ra)
    eng._step()  # a prefills+samples

    rb = _req("b", [200, 201, 202], slot=_slot(1))
    rb._stream_id = "b"
    eng._admit(rb)

    eng._step()  # a decodes; b prefills first 2 (budget)
    rows = ctx.decoded_batches[1]
    assert rows[0] == (10, 3, (0,), True)  # a's generation row FIRST
    assert rows[1:] == [(200, 2, (1,), False), (201, 3, (1,), False)]

    eng._step()  # a decodes; b prefills last token (logits)
    rows = ctx.decoded_batches[2]
    assert rows[0][2] == (0,) and rows[1] == (202, 4, (1,), True)
    # Sampled from correct absolute rows.
    assert s_b.sampled_at == [1]
    # Both streams decode together next step.
    eng._step()
    assert [r[2] for r in ctx.decoded_batches[3]] == [(0,), (1,)]


def test_kv_pressure_rewinds_exactly_and_halves_budget():
    # Script: first decode -> 1 (KV full), then success.
    ctx = FakeCtx(decode_script=[1, 0])
    sampler = FakeSampler([10, EOG])
    eng = _engine_with(ctx, samplers={"a": sampler}, prefill_chunk=64)
    req = _req("a", [100, 101, 102])
    req._stream_id = "a"
    eng._admit(req)
    slot = req.slot

    eng._step()  # decode fails with 1
    s = eng._streams["a"]
    # Bookkeeping rewound to the mark; KV truncated at the mark position.
    assert (s.n_past, s.prompt_pos) == (2, 0)
    assert slot.input_ids == [1, 2]
    assert ctx.seq_rm_calls[-1] == (0, 2, -1)
    assert s.phase.value == "prefill"
    assert eng._live_prefill_budget == 32  # halved from 64
    assert eng.health()["kv_pressure_events"] == 1

    eng._step()  # retry succeeds; sampler fires on last prompt row
    assert sampler.sampled_at == [2]
    assert s.phase.value == "decoding"


def test_kv_pressure_evicts_largest_stateless_stream_when_no_prefill():
    ctx = FakeCtx(decode_script=[1])
    s_a, s_b = FakeSampler([10, 11, 12]), FakeSampler([20])
    eng = _engine_with(ctx, samplers={"a": s_a, "b": s_b})
    # Two DECODING streams (no prefill left): a is deeper than b.
    for rid, sampler, seq, depth in (("a", s_a, 0, 30), ("b", s_b, 1, 5)):
        slot = _slot(seq)
        req = _req(rid, [100], slot=slot)
        req._stream_id = rid
        eng._admit(req)
        s = eng._streams[rid]
        s.phase = StreamPhase.DECODING
        s.prompt_pos = 1
        s.last_token = 9
        s.n_past = depth
    eng._live_prefill_budget = 16  # already at floor

    eng._step()
    a, b = eng._streams.get("a"), eng._streams.get("b")
    assert a is None and b is not None  # deepest stateless stream evicted
    assert eng.health()["kv_evictions"] == 1


def test_stop_text_and_max_tokens_retirement():
    ctx = FakeCtx()
    # "<10><11>" contains stop text "<11>"
    s_a = FakeSampler([10, 11, 12, 13])
    eng = _engine_with(ctx, samplers={"a": s_a})
    req = _req("a", [100], stop_texts=["<11>"])
    req._stream_id = "a"
    eng._admit(req)
    for _ in range(3):
        eng._step()
    assert req.out.done and "".join(req.out.chunks) == "<10><11>"

    # max_tokens cap
    s_b = FakeSampler([20, 21, 22, 23])
    eng2 = _engine_with(FakeCtx(), samplers={"b": s_b})
    rb = _req("b", [100], max_tokens=2)
    rb._stream_id = "b"
    eng2._admit(rb)
    for _ in range(4):
        eng2._step()
    assert rb.out.done and "".join(rb.out.chunks) == "<20><21>"


def test_degenerate_detok_aborts_stream_cleanly(tmp_path):
    ctx = FakeCtx()
    s_a = FakeSampler([999, 999, 999])  # invalid UTF-8 bytes, valid detok
    eng = _engine_with(ctx, samplers={"a": s_a}, capture_dir=str(tmp_path))

    # Wire a guard that trips on the 3rd repeat.
    class TripGuard:
        def __init__(self):
            self.n = 0

        def observe(self, tok):
            self.n += 1
            return "run" if self.n >= 3 else None

    eng._repetition_guard_factory = TripGuard
    req = _req("a", [100])
    req._stream_id = "a"
    eng._admit(req)
    for _ in range(4):
        if not eng._streams:
            break
        eng._step()
    from inference.repetition import DegenerateGenerationError

    assert isinstance(req.out.error, DegenerateGenerationError)


def test_closed_bridge_swept_before_step():
    ctx = FakeCtx()
    s_a = FakeSampler([10, 11, 12, 13, 14])
    eng = _engine_with(ctx, samplers={"a": s_a})
    req = _req("a", [100])
    req._stream_id = "a"
    eng._admit(req)
    eng._step()
    req.out.closed = True  # consumer abandoned
    eng._sweep_closed_bridges()
    assert not eng._streams
    assert req.out.done


def test_fatal_decode_fails_all_streams_and_parks_engine():
    boom = RuntimeError(
        "llama_decode failed (code -3): Graph computation failed internally"
    )
    ctx = FakeCtx(decode_script=[boom])
    s_a, s_b = FakeSampler([10]), FakeSampler([20])
    eng = _engine_with(ctx, samplers={"a": s_a, "b": s_b})
    ra, rb = _req("a", [100], slot=_slot(0)), _req("b", [200], slot=_slot(1))
    ra._stream_id, rb._stream_id = "a", "b"
    eng._admit(ra)
    eng._admit(rb)
    try:
        eng._step()
    except RuntimeError as exc:
        eng._on_fatal(exc)
    assert isinstance(ra.out.error, RetriableEngineError)
    assert isinstance(rb.out.error, RetriableEngineError)
    # No rebuild_fn wired -> engine parks unavailable.
    assert eng.health()["engine_fatal"] is not None
    with pytest.raises(RetriableEngineError):
        eng.submit(_req("c", [1]))


def test_fatal_with_rebuild_heals_and_flags_pinned_seats():
    boom = RuntimeError(
        "llama_decode failed (code -3): Graph computation failed internally"
    )
    ctx = FakeCtx(decode_script=[boom])
    s_a, s_b = FakeSampler([10]), FakeSampler([20])
    eng = _engine_with(ctx, samplers={"a": s_a, "b": s_b})
    pinned, stateless = _slot(0), _slot(1)
    pinned.pinned = True
    rebuilds = []
    eng._seats = [pinned, stateless]
    eng._rebuild_fn = lambda: rebuilds.append(1)

    ra = _req("a", [100], slot=pinned)
    rb = _req("b", [200], slot=stateless)
    ra._stream_id, rb._stream_id = "a", "b"
    eng._admit(ra)
    eng._admit(rb)
    try:
        eng._step()
    except RuntimeError as exc:
        eng._on_fatal(exc)

    assert rebuilds == [1]
    assert isinstance(ra.out.error, RetriableEngineError)
    # Pinned session seat stays flagged (its KV is gone) — the session
    # layer's deferred-teardown path reads exactly this flag.
    assert pinned._needs_context_refresh and pinned.dead
    # Stateless seat is clean immediately (next prepare re-forks).
    assert not stateless._needs_context_refresh and not stateless.dead
    # Engine healed: accepting work again, counters reflect the event.
    h = eng.health()
    assert h["engine_fatal"] is None
    assert h["decode_failures"] == 1 and h["latch_heals"] == 1
    rc = _req("c", [1], slot=_slot(1))
    eng.submit(rc)  # must not raise


def test_failed_rebuild_parks_engine():
    boom = RuntimeError("llama_decode failed (code -3): x")
    ctx = FakeCtx(decode_script=[boom])
    eng = _engine_with(ctx, samplers={"a": FakeSampler([10])})

    def _bad_rebuild():
        raise RuntimeError("rebuild exploded")

    eng._rebuild_fn = _bad_rebuild
    ra = _req("a", [100])
    ra._stream_id = "a"
    eng._admit(ra)
    try:
        eng._step()
    except RuntimeError as exc:
        eng._on_fatal(exc)
    assert "rebuild exploded" in (eng.health()["engine_fatal"] or "")


def test_retire_stashes_per_stream_timing():
    ctx = FakeCtx()
    sampler = FakeSampler([10, EOG])
    eng = _engine_with(ctx, samplers={"a": sampler})
    req = _req("a", [100, 101])
    req._stream_id = "a"
    eng._admit(req)
    for _ in range(3):
        if not eng._streams:
            break
        eng._step()
    slot = req.slot
    assert slot._last_prefill_s >= 0.0
    assert slot._last_decode_s >= 0.0
    assert slot._last_completion_tokens == [10]


def test_prepare_and_clear_seat_fork_semantics():
    ctx = FakeCtx()
    eng = _engine_with(ctx, samplers={})
    slot = SeqSlot(seq=1, _n_ctx=4096)
    eng.prepare_seat(slot, "default")
    # Cleared then forked from the persona head seq (2 = first head seq).
    assert ctx.seq_rm_calls[-1] == (1, 0, -1)
    assert ctx.seq_cp_calls[-1] == (2, 1, -1, -1)
    assert slot.n_tokens == 2 and slot.input_ids == [1, 2]
    with pytest.raises(KeyError):
        eng.prepare_seat(slot, "ghost")
    eng.clear_seat(slot)
    assert slot.n_tokens == 0 and not slot.pinned


# ── Stage 2: per-seq session surgeries (control ops, live thread) ─────


def _running_engine_with_ctx():
    ctx = FakeCtx()
    eng = _engine_with(ctx, samplers={})
    eng.start()
    return ctx, eng


def test_purge_to_truncates_seq_and_bookkeeping():
    ctx, eng = _running_engine_with_ctx()
    try:
        slot = SeqSlot(seq=1, _n_ctx=4096)
        slot._engine_ref = eng
        slot.n_tokens = 10
        slot.input_ids = list(range(10))
        slot.purge_to(6)
        assert (1, 6, -1) in ctx.seq_rm_calls
        assert slot.n_tokens == 6 and slot.input_ids == [0, 1, 2, 3, 4, 5]
    finally:
        eng.shutdown()


def test_window_seat_drops_oldest_half_beyond_static_head():
    ctx, eng = _running_engine_with_ctx()
    try:
        slot = SeqSlot(seq=0, _n_ctx=4096)
        slot.n_tokens = 10
        slot.static_len = 2
        slot.input_ids = list(range(10))
        new_n = eng.window_seat_sync(slot, n_keep=2)
        # n_discard = (10-2)//2 = 4: drop [2,6), shift [6,10) down by 4.
        assert new_n == 6 and slot.n_tokens == 6
        assert (0, 2, 6) in ctx.seq_rm_calls
        assert slot.input_ids == [0, 1, 6, 7, 8, 9]
        assert eng.health()["kv_forced_windows"] == 1
        # No-op below the discard threshold.
        slot2 = SeqSlot(seq=1, _n_ctx=4096)
        slot2.n_tokens = 3
        slot2.input_ids = [0, 1, 2]
        assert eng.window_seat_sync(slot2, n_keep=2) == 3
    finally:
        eng.shutdown()


def test_install_head_replaces_seat_content():
    from inference.batched_engine import PersonaHead

    ctx, eng = _running_engine_with_ctx()
    try:
        slot = SeqSlot(seq=1, _n_ctx=4096)
        slot.n_tokens = 50
        head = PersonaHead(name="low", seq=5, tokens=[7, 8, 9])
        eng.install_head_sync(slot, head)
        assert (1, 0, -1) in ctx.seq_rm_calls
        assert (5, 1, -1, -1) in ctx.seq_cp_calls
        assert slot.n_tokens == 3 and slot.static_len == 3
        assert slot.input_ids == [7, 8, 9]
    finally:
        eng.shutdown()


def test_pinned_session_is_windowed_not_destroyed_under_pressure():
    """A pinned seat is the LAST victim, and windowing it is not eviction.

    This asserted "never touched" and passed by doing nothing — which is a
    LIVELOCK: with only pinned streams resident, _relieve_pressure logged an
    error and returned, _step retried, and pressure recurred forever with no
    progress. (The old code said as much: "future: force-window the largest
    session instead.")

    The turn now ends early, but the SESSION SURVIVES — _retire's pinned branch
    advances `slot.n_tokens` to what was actually decoded and leaves the KV
    live, so the next turn continues from a consistent position. That is the
    invariant worth protecting; "the stream object stays in the dict" was not.
    """
    ctx = FakeCtx(decode_script=[1])
    s_a = FakeSampler([10])
    eng = _engine_with(ctx, samplers={"a": s_a})
    slot = _slot(0)
    slot.pinned = True  # session seat
    req = _req("a", [100], slot=slot)
    req._stream_id = "a"
    eng._admit(req)
    s = eng._streams["a"]
    s.phase = StreamPhase.DECODING
    s.prompt_pos = 1
    s.last_token = 9
    eng._live_prefill_budget = 16
    eng._step()  # terminal pressure: only a pinned stream is resident

    assert "a" not in eng._streams, "the turn ends rather than livelocking"
    assert (
        s.end_reason == "kv_pressure_truncated"
    ), "the caller must be able to tell this from a natural stop"
    assert slot.pinned, "the seat stays a session seat"
    assert slot.n_tokens == s.n_past, "session KV survives at the decoded position"


def test_unpinned_streams_are_windowed_before_pinned_ones():
    """Ordering guard: a stateless borrow is always preferred as the victim."""
    ctx = FakeCtx(decode_script=[1])
    eng = _engine_with(ctx, samplers={"a": FakeSampler([10]), "b": FakeSampler([11])})
    pinned_slot, free_slot = _slot(0), _slot(1)
    pinned_slot.pinned = True
    for sid, slot in (("a", pinned_slot), ("b", free_slot)):
        req = _req(sid, [100], slot=slot)
        req._stream_id = sid
        eng._admit(req)
        st = eng._streams[sid]
        st.phase = StreamPhase.DECODING
        st.prompt_pos = 1
        st.last_token = 9
    # Make the pinned stream the LARGEST so "largest wins" would pick it if the
    # pinned preference were not applied first.
    eng._streams["a"].n_past = 9_000
    eng._streams["b"].n_past = 10
    eng._live_prefill_budget = 16
    eng._step()

    assert "a" in eng._streams, "the session must outlive the stateless borrow"
    assert "b" not in eng._streams


def test_output_bridge_end_and_error_semantics():
    import asyncio

    async def scenario():
        loop = asyncio.get_running_loop()
        ok = OutputBridge(loop)
        ok.emit("a")
        ok.emit("b")
        ok.finish()
        got = [chunk async for chunk in ok]
        assert got == ["a", "b"]

        bad = OutputBridge(loop)
        bad.emit("partial")
        bad.finish(RetriableEngineError("engine rebuilt"))
        seen = []
        with pytest.raises(RetriableEngineError):
            async for chunk in bad:
                seen.append(chunk)
        assert seen == ["partial"]

    asyncio.run(scenario())
