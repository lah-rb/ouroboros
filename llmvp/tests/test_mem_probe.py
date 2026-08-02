"""Rebuild memory black box (2026-08-03).

The swarm kill left no evidence because nothing recorded memory across the
one window that matters: a context rebuild frees a multi-GB KV allocation and
immediately makes another. These pin the instrument's contract — above all
that it can never be the thing that breaks a rebuild.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.mem_probe import (  # noqa: E402
    MAX_RECORDS,
    RebuildWatch,
    health_block,
    sample,
)


class TestSample:
    def test_total_is_the_headline_and_wired_is_flagged_noisy(self):
        s = sample()
        if not s:  # psutil absent — valid degrade
            return
        assert "used_mb" in s and "total_mb" in s and "available_mb" in s
        # used = total - available, net of reclaimable file cache.
        assert abs((s["total_mb"] - s["available_mb"]) - s["used_mb"]) < 1.0
        # Wired must not present itself as a clean signal.
        assert "wired_mb" not in s
        if any(k.startswith("wired") for k in s):
            assert "wired_mb_noisy" in s

    def test_returns_mapping_of_floats(self):
        s = sample()
        # psutil may be absent; an empty reading is a valid degrade.
        assert isinstance(s, dict)
        for k, v in s.items():
            assert isinstance(v, (int, float)), (k, v)

    def test_never_raises_without_psutil(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _no_psutil(name, *a, **kw):
            if name == "psutil":
                raise ImportError("no psutil")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_psutil)
        assert sample() == {}


class TestRebuildWatch:
    def test_records_marks_and_publishes_to_the_backend(self):
        be = SimpleNamespace()
        with RebuildWatch("batched", backend=be) as w:
            w.mark("closed")
            w.mark("allocated")
        recs = be._mem_rebuild_records
        assert len(recs) == 1
        rec = recs[0]
        assert rec["kind"] == "batched"
        assert rec["failed"] is False
        assert set(rec["marks"]) == {"start", "closed", "allocated", "end"}
        assert rec["duration_s"] >= 0

    def test_marks_carry_elapsed_offsets(self):
        be = SimpleNamespace()
        with RebuildWatch("pool", backend=be) as w:
            w.mark("closed")
        marks = be._mem_rebuild_records[0]["marks"]
        if marks.get("closed"):  # skipped entirely when psutil is absent
            assert marks["closed"]["at_s"] >= marks["start"]["at_s"]

    def test_exception_is_recorded_and_never_suppressed(self):
        be = SimpleNamespace()
        try:
            with RebuildWatch("batched", backend=be):
                raise RuntimeError("rebuild blew up")
        except RuntimeError:
            pass
        else:  # pragma: no cover
            raise AssertionError("watch suppressed the exception")
        assert be._mem_rebuild_records[0]["failed"] is True

    def test_ring_is_bounded(self):
        be = SimpleNamespace()
        for _ in range(MAX_RECORDS + 5):
            with RebuildWatch("batched", backend=be):
                pass
        assert len(be._mem_rebuild_records) == MAX_RECORDS

    def test_survives_a_backend_that_rejects_attributes(self):
        class Frozen:
            __slots__ = ()

        # Must not raise: instrumentation is never the thing that breaks a
        # rebuild.
        with RebuildWatch("pool", backend=Frozen()) as w:
            w.mark("closed")

    def test_works_with_no_backend_at_all(self):
        with RebuildWatch("batched") as w:
            w.mark("closed")
        assert w.record()["kind"] == "batched"


class TestHealthBlock:
    def test_reports_instrumented_but_empty(self):
        out = health_block(SimpleNamespace())
        assert out["rebuilds_recorded"] == 0
        assert "last_rebuild" not in out
        assert "current" in out  # distinguishes instrumented from absent

    def test_exposes_the_trend_across_rebuilds(self):
        be = SimpleNamespace(
            _mem_rebuild_records=[
                {"headroom_low_mb": 900.0, "used_peak_mb": 100.0},
                {"headroom_low_mb": 400.0, "used_peak_mb": 600.0},
                {"headroom_low_mb": 120.0, "used_peak_mb": 880.0},
            ]
        )
        out = health_block(be)
        assert out["rebuilds_recorded"] == 3
        # The downward walk is the signal the swarm kill had no way to show.
        assert out["headroom_low_mb_trend"] == [900.0, 400.0, 120.0]
        assert out["headroom_low_mb_min"] == 120.0
        # And the same shape read from the committed side — TOTAL is the
        # measure; wired is bimodal on unified memory and must not be alarmed
        # on (operator, 2026-08-03).
        assert out["used_peak_mb_trend"] == [100.0, 600.0, 880.0]
        assert out["used_peak_mb_max"] == 880.0

    def test_tolerates_records_without_headroom(self):
        be = SimpleNamespace(_mem_rebuild_records=[{"kind": "batched"}])
        out = health_block(be)
        assert out["rebuilds_recorded"] == 1
        assert "headroom_low_mb_trend" not in out
