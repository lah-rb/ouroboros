"""The engine's capacity snapshot agrees with its own admission arithmetic.

ONE SOURCE OF TRUTH. A scheduler in another process will admit work
against `free_cells`; the engine admits against `_free_cells()`. If those
two ever diverge the scheduler over-admits for exactly as long as nobody
notices. These tests pin them equal under the states that actually differ:
live entitlement, pinned floors, retirement, and a parked engine.

Fixtures come from the existing accounting suite rather than being forked
— test_admission_pool_accounting.py already builds live streams with the
correct post-admission field semantics.
"""

from __future__ import annotations

import pytest

from tests.test_admission_pool_accounting import POOL, _live, _seat
from tests.test_batched_engine import EOG, FakeCtx, FakeSampler, _engine_with


def _eng():
    eng = _engine_with(
        FakeCtx(decode_script=[0]),
        samplers={k: FakeSampler([1, 2, 3, 4, EOG]) for k in ("a", "b", "c")},
    )
    eng._seats = [_seat(0), _seat(1)]
    eng._llama._n_ctx = POOL
    return eng


def test_snapshot_free_cells_equals_engine_free_cells():
    eng = _eng()
    _live(eng, "a", 0, gen_start=5_000, budget=8_192)
    fields = eng.capacity_fields()
    assert fields["free_cells"] == eng._free_cells(POOL)
    # And it is the documented arithmetic, not a coincidence.
    #
    # Occupancy is a PER-SEAT max, SUMMED — not a global
    # max(live_total, pinned_total). The global form under-counted every seat
    # but the largest, which is how admission came to believe 33,904 cells
    # were free while the cache could not seat 2,048 rows (2026-08-25).
    # Bands (flow prefixes, snapshot pins) hold real cells and count too.
    expected = POOL - eng._occupancy() - eng._band_occupancy() - fields["pool_slack"]
    assert fields["free_cells"] == max(0, expected)


def test_snapshot_charges_entitlement_not_position():
    """The whole reason phantom KV exists — the snapshot must report it
    the same way the engine does, or a client would 'free' cells the
    engine still considers held."""
    eng = _eng()
    _live(eng, "a", 0, gen_start=1_000, budget=16_384)
    fields = eng.capacity_fields()
    assert fields["live_occupancy"] == 1_000 + 16_384
    assert fields["free_cells"] == eng._free_cells(POOL)


def test_snapshot_tracks_retirement_and_queue_depth():
    eng = _eng()
    s = _live(eng, "a", 0, gen_start=2_000, budget=4_000)
    before = eng.capacity_fields()
    assert before["active_streams"] == 1

    eng._retire(s)
    after = eng.capacity_fields()
    assert after["active_streams"] == 0
    assert after["live_occupancy"] == 0
    assert after["free_cells"] > before["free_cells"]


def test_snapshot_reports_pinned_floor():
    eng = _eng()
    eng._seats = [_seat(0, pinned=True, n_tokens=3_000), _seat(1)]
    fields = eng.capacity_fields()
    assert fields["pinned_occupancy"] == eng._pinned_occupancy() >= 3_000
    assert fields["free_cells"] == eng._free_cells(POOL)


def test_paused_engine_reports_not_serving():
    """A rebuild leaves cells looking free; a scheduler must not dispatch
    into it. serving=False is the only signal that says so."""
    eng = _eng()
    assert eng.capacity_fields()["serving"] is True
    eng._paused = True
    assert eng.capacity_fields()["serving"] is False


def test_fatal_engine_reports_not_serving():
    eng = _eng()
    eng._fatal = RuntimeError("metal latch")
    fields = eng.capacity_fields()
    assert fields["serving"] is False
    assert "metal latch" in (fields["engine_fatal"] or "")


def test_health_exposes_the_same_numbers():
    """health() is the poll fallback; it must not drift from the push."""
    eng = _eng()
    _live(eng, "a", 0, gen_start=3_000, budget=2_000)
    h, c = eng.health(), eng.capacity_fields()
    for key in ("active_streams", "waiting", "free_cells", "kv_pool_tokens"):
        assert h[key] == c[key], key


def test_publish_survives_a_broken_bus():
    """Telemetry must never break decode (the report_completion rule)."""
    import inference.capacity as cap

    eng = _eng()
    orig = cap.BUS.publish
    cap.BUS.publish = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        eng._publish_capacity()  # must not raise
    finally:
        cap.BUS.publish = orig


def test_capacity_module_never_dereferences_a_live_context():
    """Three server kills (KERN_INVALID_ADDRESS in the native context
    accessors) are why health paths read cached ints only. A publish fires
    far more often than a health poll, so the rule binds harder here.

    Checked over the AST, not the text — the module's own prose explains
    the rule and would trip a substring search.
    """
    import ast
    import inspect

    import inference.capacity as cap

    tree = ast.parse(inspect.getsource(cap))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.name.split(".")[0] for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    assert "ctypes" not in names
    assert not [n for n in names if n.startswith(("llama_", "_ctx"))], names
