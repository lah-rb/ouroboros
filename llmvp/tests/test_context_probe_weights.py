"""context_probe weight sizing must follow the SHARD SET, not the directory.

2026-08-03: the probe globbed `*.gguf` beside the model and excluded only
`mmproj`, so a sibling MODEL counted as part of ours. DeepSeek-V4-Flash
(4 shards, 104.2GB) sits next to an unrelated 10.9GB Q8_0 build; the probe
computed 115.1GB, found negative headroom under the ceiling, and clamped
its opening rung to n_ctx 2048 — unusable. The backend's own
weights_bytes_total docstring had warned about exactly this.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.context_probe import calc_start, weights_gb  # noqa: E402


def _shards(tmp_path, stem, sizes):
    n = len(sizes)
    first = None
    for i, size in enumerate(sizes, start=1):
        p = tmp_path / f"{stem}-{i:05d}-of-{n:05d}.gguf"
        p.write_bytes(b"\0" * size)
        first = first or p
    return first


def test_sibling_model_is_not_counted(tmp_path):
    first = _shards(tmp_path, "Model-A", [1_000, 50_000, 49_000, 5_000])
    (tmp_path / "unrelated-other-model-Q8_0.gguf").write_bytes(b"\0" * 900_000)
    (tmp_path / "mmproj-vision.gguf").write_bytes(b"\0" * 800_000)
    got = weights_gb(str(first)) * 1e9
    assert round(got) == 105_000, f"summed {got} — a neighbour leaked in"


def test_single_file_model(tmp_path):
    p = tmp_path / "solo.gguf"
    p.write_bytes(b"\0" * 4_242)
    assert round(weights_gb(str(p)) * 1e9) == 4_242


def test_opening_rung_is_usable_when_a_neighbour_exists(tmp_path):
    """The regression in the form it actually bit: an over-count drives
    calc_start's headroom negative and the ladder opens at the floor."""
    first = _shards(tmp_path, "Big", [1_000, 50_000_000_000, 49_000_000_000])
    n_clean, _ = calc_start(str(first), 88_064, 1_048_576, 118.0, 2048)
    (tmp_path / "roommate-Q8_0.gguf").write_bytes(b"\0" * 10_900_000)
    n_with_neighbour, _ = calc_start(str(first), 88_064, 1_048_576, 118.0, 2048)
    assert n_clean == n_with_neighbour, "a neighbour moved the opening rung"
    assert n_clean > 2048, "ladder opened at the floor — the bug's signature"
