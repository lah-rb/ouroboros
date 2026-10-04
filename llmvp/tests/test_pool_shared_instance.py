"""A pool slot shares the primary's weights — it must never reload them.

llama_cpp.Llama defines __getstate__/__setstate__, so copy.copy(primary) re-ran
Llama.__init__ and loaded the model again for every slot. Under the 0.3.46
fork's MMAP default that went unnoticed; on the 0.4.0 fork (load_mode AUTO) a
4-slot qwen3-next pool built slots 1 and 2 as whole 46 GB copies and failed on
slot 3 (Mac, 2026-10-04). _create_shared_instance now makes a real shallow copy.
"""

from __future__ import annotations

import contextlib
import types

import numpy as np
import pytest

llama_cpp = pytest.importorskip("llama_cpp")

from inference.backends.llama_cpp_backend import LlamaCppBackend  # noqa: E402


class _ReloadingLlama:
    """Stands in for llama_cpp.Llama: restoring state means loading weights."""

    def __getstate__(self):
        return {"model_path": "weights.gguf"}

    def __setstate__(self, state):
        raise AssertionError("the pool slot reloaded the model")


class _Fake:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _primary() -> _ReloadingLlama:
    p = _ReloadingLlama()
    p.__dict__.update(
        _model=object(),
        _stack=contextlib.ExitStack(),
        context_params=types.SimpleNamespace(n_seq_max=1),
        n_batch=512,
        _n_ctx=4096,
        _logits_all=False,
        _n_vocab=32,
        input_ids=np.zeros(4096, dtype=np.intc),
        chat_handler=None,
        _hybrid_cache_mgr=None,
        # LLMVP's own per-slot attributes, set on the primary before the pool
        _persona="default",
        _static_len=10,
        _static_tokens=[1, 2, 3],
        _turn_ckpt_store=object(),
    )
    return p


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(llama_cpp.internals, "LlamaContext", _Fake)
    monkeypatch.setattr(llama_cpp.internals, "LlamaBatch", _Fake)
    monkeypatch.setattr(llama_cpp.internals, "LlamaTokenDataArray", _Fake)
    be = LlamaCppBackend.__new__(LlamaCppBackend)
    be.config = types.SimpleNamespace(
        model=types.SimpleNamespace(name="qwen3-next", split_mode="layer")
    )
    be._stream_ctx_limit = lambda: 0
    be._seq_ctx_limit = lambda ctx, params: 0
    be._make_draft = lambda: None
    return be


def test_a_pool_slot_shares_the_weights_and_owns_its_state(backend):
    primary = _primary()
    inst = backend._create_shared_instance(primary)  # __setstate__ would raise

    assert type(inst) is type(primary)
    assert inst._model is primary._model  # one copy of the weights
    assert inst._ctx.model is primary._model
    assert inst._ctx is not getattr(primary, "_ctx", None)
    assert inst.input_ids is not primary.input_ids
    assert inst._stack is not primary._stack
    assert inst.n_tokens == 0 and inst.cache is None


def test_llmvp_per_slot_attributes_are_not_inherited(backend):
    inst = backend._create_shared_instance(_primary())
    for attr in LlamaCppBackend._SLOT_PRIVATE_ATTRS:
        assert attr not in inst.__dict__, attr
