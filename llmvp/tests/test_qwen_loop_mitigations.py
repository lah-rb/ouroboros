"""Qwen3 deliberation-orbit mitigations (dev/qwen3_loop_research.md).

Three layers, each opt-in per config so other models' sampling is
untouched: a widened penalty window + DRY sampler passthrough in the
backend's generate kwargs, and a one-shot degeneration retry at the
vendor recovery recipe (temp 1.0 + presence 1.5) in the session layer.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conftest import make_config
from core.session_manager import SessionManager
from inference.backends.llama_cpp_backend import LlamaCppBackend
from inference.repetition import DegenerateGenerationError

DRY_GEN = {
    "penalty_last_n": 2048,
    "dry_multiplier": 0.8,
    "dry_base": 1.75,
    "dry_allowed_length": 2,
    "dry_penalty_last_n": -1,
}


# ── backend kwargs passthrough ─────────────────────────────────────────


def test_generate_kwargs_default_omits_dry_and_window():
    backend = LlamaCppBackend(make_config())
    kwargs = backend._build_generate_kwargs(0.7)
    assert "dry_multiplier" not in kwargs
    assert "penalty_last_n" not in kwargs  # library default (64) untouched


def test_generate_kwargs_pass_dry_and_window_when_configured():
    backend = LlamaCppBackend(make_config(generation_extra=DRY_GEN))
    kwargs = backend._build_generate_kwargs(0.7)
    assert kwargs["penalty_last_n"] == 2048
    assert kwargs["dry_multiplier"] == 0.8
    assert kwargs["dry_base"] == 1.75
    assert kwargs["dry_allowed_length"] == 2
    assert kwargs["dry_penalty_last_n"] == -1


def test_generate_kwargs_dry_defaults_fill_in():
    backend = LlamaCppBackend(make_config(generation_extra={"dry_multiplier": 0.5}))
    kwargs = backend._build_generate_kwargs(0.7)
    assert kwargs["dry_multiplier"] == 0.5
    assert kwargs["dry_base"] == 1.75
    assert kwargs["dry_allowed_length"] == 2
    assert kwargs["dry_penalty_last_n"] == -1


# ── qwen configs actually carry the posture ────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "qwen3-next-coder-80b-a3",
        "qwen3.5-122b-a10",
        "qwen3.6-35b-a3",
        "qwen3.6-27b",
        "qwen3-next-coder-80b-a3-jit",
    ],
)
def test_qwen_configs_carry_loop_posture(name):
    import yaml
    from pathlib import Path

    from core.config import Config

    path = Path(__file__).resolve().parents[1] / "configs" / f"{name}.yaml"
    cfg = Config.model_validate(yaml.safe_load(path.read_text()))
    gen = cfg.generation
    assert gen.dry_multiplier and gen.dry_multiplier > 0
    assert gen.penalty_last_n == 2048
    assert gen.degen_retry_enabled is True
    assert (gen.temperature_floor or 0) >= 0.7  # orbit survives 0.5


# ── degeneration retry (session layer) ─────────────────────────────────


def make_manager(generation_extra: dict | None = None) -> SessionManager:
    backend = SimpleNamespace(
        config=make_config(
            model_extra={"family": "chatml"},
            generation_extra=generation_extra,
        )
    )
    return SessionManager(backend)


def install_turns(mgr, monkeypatch, outcomes):
    """Replace session_turn with a script: each entry is an exception to
    raise or a text to yield. Records (temperature, sampling_overrides)."""
    calls = []

    def fake_session_turn(
        session_id,
        prompt,
        max_tokens=256,
        temperature=0.7,
        grammar=None,
        raw=False,
        reasoning=None,
        sampling_overrides=None,
    ):
        outcome = outcomes[len(calls)]
        calls.append((temperature, sampling_overrides))

        async def agen():
            if isinstance(outcome, Exception):
                raise outcome
            yield outcome

        return agen()

    monkeypatch.setattr(mgr, "session_turn", fake_session_turn)
    monkeypatch.setattr("core.interaction_logger.log_raw_generation", lambda **k: None)
    monkeypatch.setattr("core.session_manager.log_interaction", lambda **k: None)
    # Pin the no-delimiter path: other test modules leave thinking-enabled
    # renderers/configs installed globally, which routes the raw text
    # through the FSM strip and empties it. The retry logic under test is
    # upstream of extraction.
    monkeypatch.setattr(
        "core.session_manager._get_format_renderer",
        lambda family: SimpleNamespace(delimiter_pattern=lambda: None),
    )
    return calls


def test_degen_retry_redrives_once_at_recovery_recipe(monkeypatch):
    mgr = make_manager(
        {
            "degen_retry_enabled": True,
            "penalty_last_n": 2048,
        }
    )
    calls = install_turns(
        mgr,
        monkeypatch,
        [DegenerateGenerationError("long-cycle repetition: test"), "recovered"],
    )

    async def main():
        return await mgr.session_turn_complete("s1", "prompt", 128, 0.5)

    text, tokens, cache = asyncio.run(main())
    assert text == "recovered"
    assert len(calls) == 2
    assert calls[0] == (0.5, None)  # first attempt: caller's sampling
    retry_temp, retry_overrides = calls[1]
    assert retry_temp == 1.0
    assert retry_overrides == {"present_penalty": 1.5, "penalty_last_n": 2048}


def test_degen_retry_disabled_by_default(monkeypatch):
    mgr = make_manager()
    calls = install_turns(
        mgr, monkeypatch, [DegenerateGenerationError("degenerate: test")]
    )

    async def main():
        await mgr.session_turn_complete("s1", "prompt", 128, 0.5)

    with pytest.raises(DegenerateGenerationError):
        asyncio.run(main())
    assert len(calls) == 1


def test_degen_retry_gives_up_after_second_failure(monkeypatch):
    mgr = make_manager({"degen_retry_enabled": True})
    calls = install_turns(
        mgr,
        monkeypatch,
        [
            DegenerateGenerationError("degenerate: test"),
            DegenerateGenerationError("degenerate: again"),
        ],
    )

    async def main():
        await mgr.session_turn_complete("s1", "prompt", 128, 0.5)

    with pytest.raises(DegenerateGenerationError):
        asyncio.run(main())
    assert len(calls) == 2


def test_degen_retry_custom_recipe(monkeypatch):
    mgr = make_manager(
        {
            "degen_retry_enabled": True,
            "degen_retry_temperature": 0.9,
            "degen_retry_presence_penalty": 2.0,
        }
    )
    calls = install_turns(
        mgr,
        monkeypatch,
        [DegenerateGenerationError("degenerate: test"), "ok"],
    )

    async def main():
        return await mgr.session_turn_complete("s1", "prompt", 128, 0.5)

    text, _, _ = asyncio.run(main())
    assert text == "ok"
    assert calls[1][0] == 0.9
    assert calls[1][1]["present_penalty"] == 2.0
