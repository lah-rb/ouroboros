"""Band-layout planner tests: the pool seq map must tile exactly."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.seq_layout import (  # noqa: E402
    SEQ_FLOW_BASE,
    SEQ_STATIC,
    SEQ_WORKING,
    plan_pool_seq_map,
)


def test_full_layout_tiles_exactly():
    m = plan_pool_seq_map(
        flow_hot_set=8, snapshot_max=2, reasoning_levels=["low", "high"]
    )
    assert (SEQ_WORKING, SEQ_STATIC) == (0, 1)
    assert m.flow_base == SEQ_FLOW_BASE == 2
    assert m.snap_base == 10
    assert m.reasoning_base == 12
    assert m.n_seq_max == 14  # the production a5 shape


def test_disabled_bands_collapse():
    m = plan_pool_seq_map(flow_hot_set=0, snapshot_max=0, reasoning_levels=[])
    assert m.snap_base == m.flow_base == SEQ_FLOW_BASE
    assert m.reasoning_base == SEQ_FLOW_BASE
    assert m.n_seq_max == 2  # just working + static


def test_bands_never_overlap():
    m = plan_pool_seq_map(flow_hot_set=3, snapshot_max=5, reasoning_levels=["low"])
    flow = set(range(m.flow_base, m.flow_base + m.flow_count))
    snap = set(range(m.snap_base, m.snap_base + m.snap_count))
    reason = set(range(m.reasoning_base, m.reasoning_base + m.reasoning_count))
    assert not (flow & snap or snap & reason or flow & reason)
    assert max(flow | snap | reason) < m.n_seq_max


def test_negative_sizes_rejected():
    with pytest.raises(ValueError):
        plan_pool_seq_map(flow_hot_set=-1, snapshot_max=0, reasoning_levels=[])
