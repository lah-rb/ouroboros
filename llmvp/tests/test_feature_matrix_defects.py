"""Six defects surfaced by the 2026-07-30 FEATURE × STRATEGY audit.

They share a shape: a config requests a feature, the run does not deliver it,
and nothing reconciles the two. Same class as the transient-files declaration
drift — the record and the behaviour disagree, silently.

The serious one is the seat leak. The rest are observability: at the default
log level a batched config could claim four features it never received.

See dev/caching/FEATURE_MATRIX.md §5.
"""

from __future__ import annotations

import inspect

import pytest

# ── 1. the seat leak ──────────────────────────────────────────────────


class TestColdForkSeatLeak:
    """`start_session` acquires a seat and marks it `pinned` BEFORE forking.
    Under batched, a cold snapshot entry makes `rebuild_snapshot_cold` raise —
    and every entry goes cold after any context refresh. The exception escaped
    before the session was registered, so nothing ever released the seat, and
    the reaper skips pinned seats by design. One of 128, permanently, per call.
    """

    def _src(self) -> str:
        from core.session_manager import SessionManager

        return inspect.getsource(SessionManager.start_session)

    def test_a_failed_fork_releases_the_seat(self):
        src = self._src()
        assert (
            "except BaseException:" in src
        ), "the fork block must catch and release — a bare raise strands the seat"
        assert "release_instance(instance)" in src

    def test_it_unpins_before_releasing(self):
        """release_instance on a still-pinned seat is a no-op in the batched
        backend, which would re-leak it. Order is load-bearing."""
        src = self._src()
        unpin = src.index("instance.pinned = False")
        release = src.index("release_instance(instance)")
        assert unpin < release, "must unpin BEFORE releasing"

    def test_the_failure_is_loud(self):
        src = self._src()
        assert "FAILED — seat released" in src

    def test_the_original_exception_still_propagates(self):
        """The caller must still see the failure — this fixes the leak, not
        the error."""
        src = self._src()
        tail = src[src.index("except BaseException:") :]
        assert "raise" in tail


# ── 2-3. what batched will not do, said once, at boot ─────────────────


class TestBatchedDeclaresItsRefusals:
    def _src(self) -> str:
        from inference.backends.llama_cpp_backend import LlamaCppBackend

        return inspect.getsource(LlamaCppBackend._warm_batched)

    def test_flow_band_log_reads_the_map_not_the_intent(self):
        """`_flow_band` was computed in __init__ from
        (_flow_resident or _session_flow_fork) and never recomputed after
        _session_flow_fork became False — so every batched config with
        flow_kv_cache: false announced an 8-slot band that did not exist. The
        seq map sizes flow_slots from flow_kv_cache ALONE."""
        src = self._src()
        assert "_flow_slots = len(seq_map.flow_seqs)" in src
        assert "if _flow_slots:" in src
        assert (
            "self._flow_hot_set" not in src.split("flow band ACTIVE")[0][-400:]
        ), "the log must not report the configured hot-set size"

    def test_it_warns_when_the_flag_is_on_but_no_slots_were_allocated(self):
        src = self._src()
        assert "inert this boot" in src

    def test_pool_only_features_are_named_at_boot(self):
        """Previously log.debug per request — invisible at the default level."""
        src = self._src()
        assert "POOL-ONLY, ignored under batched" in src
        assert "resident_strip_reasoning" in src
        assert "degen_retry" in src

    def test_the_flow_fork_disable_is_a_warning_not_an_info(self):
        src = self._src()
        head = src[: src.index("self._session_flow_fork = False")]
        assert "log.warning" in head


# ── 4. the deleted path stops appearing in telemetry ──────────────────


class TestNoDeadLegacyStrategy:
    def test_strategy_can_only_be_resident_or_full_replay(self):
        from inference.backends.llama_cpp_backend import LlamaCppBackend

        src = inspect.getsource(LlamaCppBackend._session_strategy)
        assert "legacy_save_state" not in src, (
            "the splice was deleted and the validator refuses "
            "session_full_replay: false — a third value cannot occur"
        )

    def test_the_denial_warning_names_the_real_fallback(self):
        """It said 'falling back to the legacy save_state path'. That path does
        not exist; the fallback is full replay, and the operator needs to know
        it costs 7.8-9.8x more prefill."""
        from inference.backends.llama_cpp_backend import LlamaCppBackend

        src = inspect.getsource(LlamaCppBackend)
        assert "falling back to the legacy save_state path" not in src
        assert "falling back to FULL REPLAY" in src


# ── 5. an inert flow cache is refused at load ─────────────────────────


class TestInertFlowCacheWarns:
    """Since the blob path was deleted the flow cache is seq-ops only, so
    without resident it serves from the static base and counts a fallback
    forever — a flag that reads as a feature and buys nothing."""

    BASE = """
app: {host: 0.0.0.0, port: 8008, log_level: info, cors_origins: ['*'], backend_timeout: 180}
model:
  name: t
  family: chatml
  path: /tmp/t.gguf
  n_ctx: 4096
  n_gpu_layers: -1
  seed: -1
  verbose: false
  thinking: false
  flow_kv_cache: %s
  resident_seq_cache: %s
prompt: {system_prompt: hello}
generation: {max_tokens_default: 512, temperature_default: 1.0}
knowledge: {tokens_bin: ./data/t.tokens.bin, token_limit: 1024}
resources: {decode_mode: pool, cpu_threads: 2}
logging: {enabled: true, directory: ./logs}
"""

    def _load(self, tmp_path, flow: bool, resident: bool):
        import yaml

        from core.config import Config

        body = self.BASE % (str(flow).lower(), str(resident).lower())
        return Config(**yaml.safe_load(body))

    def test_flow_on_without_resident_warns(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            self._load(tmp_path, flow=True, resident=False)
        assert any("flow_kv_cache is ON" in r.message for r in caplog.records)

    def test_flow_on_with_resident_is_quiet(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            self._load(tmp_path, flow=True, resident=True)
        assert not any("flow_kv_cache is ON" in r.message for r in caplog.records)

    def test_flow_off_is_quiet(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            self._load(tmp_path, flow=False, resident=False)
        assert not any("flow_kv_cache is ON" in r.message for r in caplog.records)


# ── 6. the fleet has no inert flags left ──────────────────────────────


def _served_configs():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "configs"
    return sorted(p for p in root.glob("*.yaml") if p.name != "reference.yaml")


@pytest.mark.parametrize("path", _served_configs(), ids=lambda p: p.stem)
def test_no_served_config_requests_an_inert_flow_cache(path):
    """The fleet-wide form of the validation above. qwen3.6-35b-a3 carried
    flow_kv_cache: true with resident off until 2026-07-30."""
    import yaml

    m = (yaml.safe_load(path.read_text()) or {}).get("model", {}) or {}
    if m.get("flow_kv_cache"):
        assert m.get("resident_seq_cache"), (
            f"{path.name}: flow_kv_cache is on without resident_seq_cache — "
            "it will serve from the static base and count a fallback per request"
        )


# ── 7. an evicted generation must not lose its text ───────────────────


class TestEvictionCapturesPartialText:
    """The 2026-07-30 sweep's most interesting generation left NO record.

    laguna-s-2.1 produced 60,659 tokens over 33 minutes on the batch
    structural step and was guillotined by the 1800s timed context refresh.
    Only the consumer-abandoned path captured partial text, so an
    eviction-retired stream dropped its tokens on the floor: nothing in the
    trace, nothing in runaway_captures, nothing in interactions.jsonl.

    That text is the whole question. Token-level guards (which did not fire)
    rule out repetition, but they cannot tell "elaborately writing seven
    files" from "writing forty nobody asked for" — only the output can.
    """

    def _src(self) -> str:
        import inspect

        from inference.batched_engine import BatchedEngine

        return inspect.getsource(BatchedEngine.evict_all_streams)

    def test_eviction_dumps_a_capture_before_retiring(self):
        src = self._src()
        assert "dump_capture" in src
        cap = src.index("dump_capture")
        ret = src.index("self._retire(s, error=RetriableEngineError(reason))")
        assert cap < ret, "capture BEFORE retire — retire clears the pipeline"

    def test_it_reuses_the_same_threshold_as_the_abandoned_path(self):
        """Not a new policy — the same CHECK_INTERVAL floor, so a trivial
        stream does not spam the capture directory."""
        src = self._src()
        assert "runaway_capture.CHECK_INTERVAL" in src

    def test_the_reason_names_the_eviction(self):
        src = self._src()
        assert "evicted mid-generation" in src

    def test_the_counter_still_moves(self):
        src = self._src()
        assert "h_runaway_captures += 1" in src
