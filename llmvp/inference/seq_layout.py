"""Pool-mode KV sequence-band layout — one planner, unit-testable.

The pool context packs several resident bands into n_seq_max:

    seq 0                  SEQ_WORKING — the live generation / session seq
    seq 1                  SEQ_STATIC — pristine static-prefix fork source
    [2, 2+flow)            per-flow resident prefixes (flow_kv band)
    [snap_base, +snap)     semi-permanent session snapshots
    [reason_base, +levels) pinned reasoning heads (head-swap)

Before this planner existed the same arithmetic lived in three places
(_create_primary_instance's n_seq_max expression, _snap_seq_base,
_reasoning_seq_base) — resizing a band in one and not the others would
make seqs collide or overrun n_seq_max silently. This mirrors what the
batched engine already does with plan_seq_map(); the pool layout now has
the same single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

SEQ_WORKING = 0
SEQ_STATIC = 1
SEQ_FLOW_BASE = 2


@dataclass(frozen=True)
class PoolSeqMap:
    flow_base: int
    flow_count: int
    snap_base: int
    snap_count: int
    reasoning_base: int
    reasoning_count: int
    n_seq_max: int

    def __post_init__(self) -> None:
        # Bands must tile [SEQ_FLOW_BASE, n_seq_max) exactly — any gap or
        # overlap is a planner bug, better dead at construction than as a
        # silent KV collision mid-run.
        assert self.flow_base == SEQ_FLOW_BASE
        assert self.snap_base == self.flow_base + self.flow_count
        assert self.reasoning_base == self.snap_base + self.snap_count
        assert self.n_seq_max == self.reasoning_base + self.reasoning_count


def plan_pool_seq_map(
    flow_hot_set: int,
    snapshot_max: int,
    reasoning_levels: List[str],
) -> PoolSeqMap:
    """Lay out the pool bands. Pure function — pass 0/[] for disabled bands
    (a disabled flow band contributes no seqs; same for snapshots and
    reasoning heads)."""
    if flow_hot_set < 0 or snapshot_max < 0:
        raise ValueError("band sizes must be >= 0")
    flow_base = SEQ_FLOW_BASE
    snap_base = flow_base + flow_hot_set
    reasoning_base = snap_base + snapshot_max
    return PoolSeqMap(
        flow_base=flow_base,
        flow_count=flow_hot_set,
        snap_base=snap_base,
        snap_count=snapshot_max,
        reasoning_base=reasoning_base,
        reasoning_count=len(reasoning_levels),
        n_seq_max=reasoning_base + len(reasoning_levels),
    )
