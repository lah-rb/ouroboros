"""rawCompletion must honor `reasoning` and report `finished` honestly.

Both were silent no-ops until 2026-07-26, and the first one cost a 2.5-hour,
4,107-request corpus regeneration:

* `reasoning` was DROPPED. The GraphQL CompletionRequest advertises the field
  and the raw resolver accepted it, so a caller driving level comparisons
  through rawCompletion got N identical generations while believing it had N
  levels. Measured symptom in the adaptive_thinking counterfactual corpus:
  mean CoT of 764 / 779 / 781 chars for low / medium / high — pure noise,
  because every request ran on the default head.

* `finished` was hardcoded True. Consumers use it as a truncation flag, so a
  constant True asserts "no request has ever been truncated". Worse than
  absent: an absent field is visibly missing, a constant one looks measured.

Both are the same failure shape as the seam gate and the dead router — an
input accepted and ignored, output identical to the working case.
"""

from __future__ import annotations

import pytest

import core.inference as inference


class _RecordingBackend:
    """Captures the kwargs generate_async actually receives."""

    class _Caps:
        manual_pooling = False

    capabilities = _Caps()

    def __init__(self, produced: int = 5):
        self.calls: list[dict] = []
        self.produced = produced

    async def generate_async(self, **kwargs):
        self.calls.append(kwargs)
        return "x " * self.produced


@pytest.fixture
def backend(monkeypatch):
    b = _RecordingBackend()

    async def _get_backend():
        return b

    monkeypatch.setattr(inference, "_get_backend", _get_backend)
    # Signature carries `prepopulated` since the prompt+generation clamp; this
    # double ignores it (these tests are about reasoning kwargs, not budgets).
    monkeypatch.setattr(
        inference, "resolve_max_tokens", lambda v, prepopulated=0: v or 128
    )
    monkeypatch.setattr(inference, "resolve_temperature", lambda v: v or 0.7)
    monkeypatch.setattr(
        inference.static_tokens_manager, "get_static_tokens", lambda: [1, 2, 3]
    )
    monkeypatch.setattr(inference, "get_cached_tokenizer", lambda: object())
    monkeypatch.setattr(inference, "build_full_prompt", lambda p, t, **k: [4, 5, 6])
    return b


@pytest.mark.asyncio
async def test_reasoning_reaches_the_backend(backend):
    """THE regression: a level passed in must arrive at generate_async."""
    await inference.run_raw_completion(prompt="hi", reasoning="high")
    assert backend.calls, "generate_async was never called"
    assert backend.calls[0].get("reasoning") == "high", (
        "rawCompletion dropped `reasoning` — level comparisons driven through "
        "this path silently compare identical generations"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["low", "medium", "high"])
async def test_every_level_is_forwarded_distinctly(backend, level):
    await inference.run_raw_completion(prompt="hi", reasoning=level)
    assert backend.calls[0]["reasoning"] == level


@pytest.mark.asyncio
async def test_absent_reasoning_sends_no_kwarg(backend):
    """None must not become the string 'None' — the default head is the
    correct behavior when no level is requested."""
    await inference.run_raw_completion(prompt="hi")
    assert "reasoning" not in backend.calls[0]


@pytest.mark.asyncio
async def test_grammar_and_reasoning_coexist(backend):
    await inference.run_raw_completion(
        prompt="hi", grammar="root ::= .", reasoning="low"
    )
    call = backend.calls[0]
    assert call["grammar"] == "root ::= ."
    assert call["reasoning"] == "low"
