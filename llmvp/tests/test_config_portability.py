"""Per-machine portability: one loader for boot, hot-load and swap.

THE INCIDENT. `loadModel("paddle-ocr-vl")` on the second machine failed with
"cannot size weights at /home/lah-rb/models/PaddleOCR-VL-1.6.Q8_0.gguf" — the
FIRST machine's absolute path, carried by a tracked yaml. LLMVP_MODELS_ROOT
exists for exactly this and was one call away, but resident_models.load and
model_swap read the raw yaml straight into Config(): no redirect, and no
`extends:` either (Config is extra="forbid", so a variant could not hot-load
at all). A second defect sat underneath: the redirect looked for
`vision.mmproj_path`, a section no config has — every mmproj_path lives under
`model:` — so even on boot the projector was never re-rooted.

These tests pin the redirect for BOTH weights keys, its never-second-guess
rule, the named loader's inheritance and its refusal to touch the served
config, and that hot-load actually goes through it.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import config as cfgmod  # noqa: E402
from core.config import _redirect_model_paths, load_named_config  # noqa: E402

LOG = logging.getLogger("test")


def _base(gguf: str, mmproj: str, **model_extra) -> dict:
    return {
        "app": {"host": "127.0.0.1", "port": 8008, "log_level": "info"},
        "model": {
            "name": "base-model",
            "family": "chatml",
            "path": gguf,
            "mmproj_path": mmproj,
            "n_ctx": 4096,
            "n_gpu_layers": -1,
            "seed": -1,
            "verbose": False,
            **model_extra,
        },
        "prompt": {},
        "generation": {"max_tokens_default": 111},
        "knowledge": {"tokens_bin": "./data/test.tokens.bin", "token_limit": 1024},
        "resources": {"cpu_threads": 1, "max_concurrent_requests": 1},
        "logging": {"enabled": False, "directory": "./logs"},
    }


@pytest.fixture
def models_root(tmp_path):
    root = tmp_path / "models-here"
    root.mkdir()
    (root / "w.gguf").write_bytes(b"x")
    (root / "p.gguf").write_bytes(b"x")
    return root


# ── the redirect ──────────────────────────────────────────────────────


def test_redirect_rewrites_both_weights_keys_when_absent(models_root, monkeypatch):
    monkeypatch.setenv("LLMVP_MODELS_ROOT", str(models_root))
    raw = _base("/other/machine/w.gguf", "/other/machine/p.gguf")
    _redirect_model_paths(raw, LOG)
    assert raw["model"]["path"] == str(models_root / "w.gguf")
    assert raw["model"]["mmproj_path"] == str(
        models_root / "p.gguf"
    ), "the projector lives under model:, and used to be skipped"


def test_redirect_never_touches_a_path_that_resolves(
    models_root, tmp_path, monkeypatch
):
    """A correct config is never second-guessed, even when the root also has
    a file of that name."""
    here = tmp_path / "local.gguf"
    here.write_bytes(b"y")
    (models_root / "local.gguf").write_bytes(b"z")
    monkeypatch.setenv("LLMVP_MODELS_ROOT", str(models_root))
    raw = _base(str(here), "/absent/p.gguf")
    _redirect_model_paths(raw, LOG)
    assert raw["model"]["path"] == str(here)
    assert raw["model"]["mmproj_path"] == str(models_root / "p.gguf")


def test_redirect_is_silent_when_unset_or_when_the_basename_is_absent(
    models_root, monkeypatch
):
    monkeypatch.delenv("LLMVP_MODELS_ROOT", raising=False)
    raw = _base("/other/w.gguf", "/other/p.gguf")
    _redirect_model_paths(raw, LOG)
    assert raw["model"]["path"] == "/other/w.gguf"
    monkeypatch.setenv("LLMVP_MODELS_ROOT", str(models_root))
    raw = _base("/other/nope.gguf", "/other/p.gguf")
    _redirect_model_paths(raw, LOG)
    assert (
        raw["model"]["path"] == "/other/nope.gguf"
    ), "no such basename here -> untouched"


# ── the named loader ─────────────────────────────────────────────────


@pytest.fixture
def catalog(tmp_path, models_root):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "base.yaml").write_text(
        yaml.safe_dump(_base("/other/machine/w.gguf", "/other/machine/p.gguf"))
    )
    (configs / "base-mac.yaml").write_text(
        yaml.safe_dump(
            {
                "extends": "base",
                "notes": "documentation only",
                "model": {"name": "base-mac", "main_gpu": 0, "vision_pool_size": 1},
            }
        )
    )
    return configs


def test_named_loader_applies_extends_and_the_redirect(
    catalog, models_root, monkeypatch
):
    monkeypatch.setenv("LLMVP_MODELS_ROOT", str(models_root))
    cfg = load_named_config("base-mac", root=catalog)
    assert cfg.model.name == "base-mac"
    assert cfg.model.family == "chatml", "inherited"
    assert cfg.model.vision_pool_size == 1
    # ModelConfig coerces to Path; compare as paths.
    assert Path(cfg.model.path) == models_root / "w.gguf"
    assert Path(cfg.model.mmproj_path) == models_root / "p.gguf"


def test_named_loader_does_not_apply_the_primary_n_ctx_ceiling_by_default(
    catalog, monkeypatch
):
    """LLMVP_N_CTX is the host's ceiling for the model it SERVES. A hot
    secondary keeps its own (deliberately minimal) n_ctx unless asked."""
    monkeypatch.setenv("LLMVP_N_CTX", "65536")
    assert load_named_config("base", root=catalog).model.n_ctx == 4096
    assert (
        load_named_config("base", root=catalog, apply_n_ctx=True).model.n_ctx == 65536
    )


def test_named_loader_never_touches_the_served_config(catalog, monkeypatch):
    calls = []
    monkeypatch.setattr(cfgmod, "set_config", lambda c: calls.append(c))
    load_named_config("base-mac", root=catalog)
    assert calls == []


def test_unknown_name_is_a_keyerror_pointing_at_the_models_query(catalog):
    with pytest.raises(KeyError, match="unknown model config 'nope'"):
        load_named_config("nope", root=catalog)


def test_hot_load_goes_through_the_named_loader(monkeypatch):
    """resident_models.load must call load_named_config — the raw
    yaml.safe_load it replaced is the whole incident."""
    import inspect

    from core import resident_models as rm

    src = inspect.getsource(rm.load)
    assert "load_named_config(" in src
    assert "yaml.safe_load(" not in src, "the raw read is gone (a comment may name it)"
