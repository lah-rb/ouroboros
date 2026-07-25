"""Shared builders for the llmvp suite (TESTING.md roadmap, 2026-07-21).

``make_config`` is the canonical minimal-valid Config builder (promoted
from test_batched_engine's local ``_cfg``); ``installed_config`` installs
it as the process config and restores the prior one afterwards — the
pattern for API-layer tests that exercise module-level config views.
New shared fakes/fixtures go here, not in individual files.
"""

from __future__ import annotations

import pytest

from core.config import Config, get_config, set_config


def make_config(
    decode_mode: str = "pool",
    model_extra: dict | None = None,
    resources_extra: dict | None = None,
    generation_extra: dict | None = None,
) -> Config:
    """Minimal valid Config with targeted overrides per section."""
    return Config.model_validate(
        {
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
            "generation": {**(generation_extra or {})},
            "knowledge": {"tokens_bin": "./data/m.tokens.bin", "token_limit": 1024},
            "resources": {
                "cpu_threads": 1,
                "max_concurrent_requests": 2,
                "decode_mode": decode_mode,
                **(resources_extra or {}),
            },
            "logging": {"enabled": False},
        }
    )


class FakeTok:
    """Minimal tokenizer: maps a few marker strings to single ids.

    Promoted from test_session_framing (2026-07-25). It is the standing
    answer to "that test needs a real tokenizer" — it does not; it needs
    these four markers to resolve to stable ids.
    """

    _IDS = {
        "</think>": [99],
        "<think>\n": [88, 10],
        "<channel|>": [101],
        "<|channel|>": [200005],
    }

    def tokenize(self, b, add_bos=False, special=False):
        return self._IDS.get(b.decode("utf-8"), [1, 2, 3])


class RecordingCtx:
    """llama context double that records the seq ops performed on it.

    ``memory_can_shift`` is constructor-controlled: the reasoning strip and
    the snapshot machinery both branch on it, and a hybrid/recurrent model
    (Qwen3.5/Qwen3-Next) reports False, which must read as "skip" rather
    than "corrupt the recurrent state".
    """

    def __init__(self, can_shift: bool = True) -> None:
        self.ops: list[tuple] = []
        self._can_shift = can_shift

    def memory_can_shift(self) -> bool:
        return self._can_shift

    def memory_seq_rm(self, seq, p0, p1):
        self.ops.append(("rm", seq, p0, p1))

    def memory_seq_cp(self, src, dst, p0, p1):
        self.ops.append(("cp", src, dst, p0, p1))

    def memory_seq_add(self, seq, p0, p1, delta):
        self.ops.append(("add", seq, p0, p1, delta))


def make_instance(n_tokens: int = 8, n_ctx: int = 4096, can_shift: bool = True):
    """A llama-instance double with a recording ctx and a working ``eval``.

    ``eval`` appends to ``eval_calls`` and advances ``n_tokens`` the way the
    real one does, so truncate-and-replay arithmetic is observable.
    """
    import numpy as np
    from collections import OrderedDict
    from types import SimpleNamespace

    inst = SimpleNamespace(
        _ctx=RecordingCtx(can_shift=can_shift),
        input_ids=np.zeros(n_ctx, dtype=np.intc),
        n_tokens=n_tokens,
        _n_ctx=n_ctx,
        _snap_seqs=OrderedDict(),
        _flow_seqs=OrderedDict(),
    )
    seed = [11, 12, 13, 40, 41, 42, 43, 44][:n_tokens]
    inst.input_ids[: len(seed)] = np.array(seed, dtype=np.intc)
    inst.eval_calls = []
    inst.eval = lambda toks: (
        inst.eval_calls.append(list(toks)),
        setattr(inst, "n_tokens", inst.n_tokens + len(toks)),
    )
    return inst


@pytest.fixture
def installed_config():
    """Install a make_config() as the process config; restore afterwards."""
    prev = getattr(get_config, "_config", None)
    cfg = make_config()
    set_config(cfg)
    yield cfg
    if prev is not None:
        set_config(prev)
    else:
        get_config._config = None
