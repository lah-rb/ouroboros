"""ctx_checkpoints is settable, and unset passes nothing (2026-10-01).

An SWA model run without swa_full is "hybrid" to the binding, which then keeps
up to 16 host-RAM context checkpoints. gemma-4-12b on the 3060 box (a 10 GB VM)
was OOM-killed by them on its third stateless pack turn. The knob lets a config
turn the cache off; every config that does not set it must keep the binding's
default, so nothing is passed.
"""

from __future__ import annotations

import types

from core.config import load_named_config
from inference.backends.llama_cpp_backend import LlamaCppBackend


def _backend(**model) -> LlamaCppBackend:
    b = LlamaCppBackend.__new__(LlamaCppBackend)  # no __init__: no model load
    b.config = types.SimpleNamespace(model=types.SimpleNamespace(**model))
    return b


def test_unset_passes_nothing():
    assert _backend()._checkpoint_kwargs() == {}
    assert _backend(ctx_checkpoints=None)._checkpoint_kwargs() == {}


def test_zero_is_passed_through():
    assert _backend(ctx_checkpoints=0)._checkpoint_kwargs() == {"ctx_checkpoints": 0}


def test_the_3060_gemma_config_disables_the_cache():
    assert load_named_config("gemma-4-12b-3060").model.ctx_checkpoints == 0


def test_muse_keeps_the_binding_default():
    assert load_named_config("muse-glimmer-30b-cuda").model.ctx_checkpoints is None
