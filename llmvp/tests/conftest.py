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


class FakeGenLlama:
    """Pool-instance double whose ``generate`` mirrors the binding's order.

    The binding's ``Llama.generate`` evaluates the prompt, then for each
    sampled token YIELDS it and evaluates it only when the consumer RESUMES
    the generator. A consumer that breaks after a yield (budget, stop text)
    therefore leaves the last yielded token UNEVALUATED — the KV is one short
    of the completion. The double keeps that order so the off-by-one is
    observable in tests; ``input_ids``/``n_tokens`` advance exactly as the
    real eval does.
    """

    def __init__(
        self,
        script: list[int],
        *,
        n_tokens: int = 0,
        n_ctx: int = 4096,
        piece: bytes = b"x",
    ) -> None:
        import numpy as np
        from types import SimpleNamespace

        self.script = list(script)
        self.n_tokens = n_tokens
        self._n_ctx = n_ctx
        self.input_ids = np.zeros(n_ctx, dtype=np.intc)
        self._model = SimpleNamespace(vocab=object())
        self._ctx = RecordingCtx(can_shift=True)
        self._persona = "default"
        self._piece = piece
        self.eval_calls: list[list[int]] = []
        self.generate_kwargs: dict = {}

    def eval(self, toks) -> None:
        toks = [int(t) for t in toks]
        self.eval_calls.append(toks)
        self.input_ids[self.n_tokens : self.n_tokens + len(toks)] = toks
        self.n_tokens += len(toks)

    def generate(self, tokens, **kwargs):
        self.generate_kwargs = dict(kwargs)
        self.eval(tokens)
        for t in self.script:
            yield t
            self.eval([t])  # only on resume — the binding's yield-then-eval

    def detokenize(self, tokens, prev_tokens=None, special=False):
        return self._piece * len(list(tokens))

    def reset(self) -> None:
        self.n_tokens = 0


class HybridCtx:
    """A hybrid (attention + recurrent) memory with llama.cpp's semantics.

    ``cells`` is the attention/indexer cache — one entry per position, tail
    removable. ``recr`` is what the recurrent state has absorbed: it cannot be
    partially erased, so ``memory_seq_rm(0, p, -1)`` with ``0 < p <= last
    absorbed position`` returns False and changes nothing (llama-memory-
    recurrent.cpp seq_rm with n_rs_seq == 0). PARTIAL_ONLY state I/O moves ONLY
    the recurrent part; restoring it rewinds ``recr`` to the saved prefix,
    after which the tail rm succeeds. A failed write drops the seq entirely —
    hybrid-idx's state_drop. Registered checkpoint caches are invalidated the
    way the binding's LlamaContext wrapper invalidates them.
    """

    def __init__(self, state_size: int = 64, recurrent: bool = True) -> None:
        self.recurrent = recurrent  # False: an attention-only memory
        self.cells: list[int] = []
        self.recr: list[int] = []
        self.ops: list[tuple] = []
        self.state_size = state_size
        self.short_read = False
        self.short_write = False
        self.caches: list = []
        self._snapshots: dict[int, list[int]] = {}

    # the model side
    def absorb(self, toks) -> None:
        self.cells += list(toks)
        if self.recurrent:
            self.recr += list(toks)

    def _invalidate(self, seq_id=-1, suffix_start=None, keep_seq_id=None):
        for c in list(self.caches):
            c._invalidate_memory(seq_id, suffix_start, keep_seq_id)

    # the binding's LlamaContext surface
    def _register_checkpoint_cache(self, cache) -> None:
        self.caches.append(cache)

    def memory_clear(self, data: bool) -> None:
        self.ops.append(("clear",))
        self._invalidate()
        self.cells, self.recr = [], []

    def memory_seq_rm(self, seq, p0, p1) -> bool:
        self.ops.append(("rm", seq, p0, p1))
        assert p1 == -1, "the fake models tail removal only"
        last = len(self.recr) - 1
        if 0 < p0 <= last:
            return False  # the recurrent half cannot un-mix a tail
        self.cells = self.cells[:p0]
        if p0 <= last:
            self.recr = []
        self._invalidate(seq, suffix_start=p0)
        return True

    def memory_seq_pos_max(self, seq) -> int:
        if not self.recurrent:
            return len(self.cells) - 1
        return min(len(self.cells), len(self.recr)) - 1

    def get_state_seq_size_ext(self, seq, flags) -> int:
        self.ops.append(("size",))
        return self.state_size

    def get_state_seq_data_ext(self, buf, size, seq, flags) -> int:
        self.ops.append(("get",))
        n = len(self.recr)
        self._snapshots[n] = list(self.recr)
        buf[:8] = list(n.to_bytes(8, "little"))
        return size - 1 if self.short_read else size

    def set_state_seq_data_ext(self, buf, size, seq, flags) -> int:
        self.ops.append(("set",))
        self._invalidate(seq)
        if self.short_write:
            self.cells, self.recr = [], []  # state_drop
            return 0
        n = int.from_bytes(bytes(buf[:8]), "little")
        self.recr = list(self._snapshots[n])
        return size


class HybridLlama(FakeGenLlama):
    """FakeGenLlama over a HybridCtx: every eval is absorbed by the memory."""

    def __init__(self, script, **kw) -> None:
        super().__init__(script, **kw)
        self._ctx = HybridCtx()
        self._ctx.absorb(list(self.input_ids[: self.n_tokens]))

    def eval(self, toks) -> None:
        super().eval(toks)
        self._ctx.absorb([int(t) for t in toks])

    def reset(self) -> None:
        self._ctx.memory_clear(True)
        self.n_tokens = 0


def make_pool_backend(config=None):
    """A real ``LlamaCppBackend`` that never loads a model — for driving
    ``generate_stream_sync`` against a ``FakeGenLlama``."""
    from inference.backends.llama_cpp_backend import LlamaCppBackend

    backend = LlamaCppBackend(config or make_config())
    backend._resident_static_len = 0
    # initialize() opens the readiness gate; generation_guard waits on it.
    backend._ready_event.set()
    return backend


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
