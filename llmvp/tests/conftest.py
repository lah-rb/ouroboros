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
