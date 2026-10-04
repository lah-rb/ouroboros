"""Guard G1 sized to the serving window (#14, 2026-09-26).

The fixed 480,000-character ceiling protected no window in particular and
never saw session turns — the overflow that lost a verdict (262,715 tokens
of a 262,144 window) was a session turn. The backstop now reads the window
LLMVP serves, subtracts what the context already holds and the turn's
output reserve, covers session turns, and records a PromptBackstop row when
it fires.
"""

from __future__ import annotations

import pytest

from agent.effects.inference import PROMPT_CHAR_CEILING, InferenceEffect


class _Served(InferenceEffect):
    """A server with a reported window that counts ~4 chars per token."""

    def __init__(self, n_ctx: int, **kw):
        super().__init__(**kw)
        self._n = n_ctx
        self.health_calls = 0
        self.count_calls = 0
        self.reports: list[dict] = []
        self.on_backstop = self.reports.append

    async def cache_health(self) -> dict:
        self.health_calls += 1
        return {"nCtxSeq": self._n} if self._n else {}

    async def token_count(self, texts, model="", timeout=15.0):
        self.count_calls += 1
        return [len(t) // 4 for t in texts]


@pytest.mark.asyncio
async def test_a_small_prompt_never_asks_the_server():
    eff = _Served(262144)
    assert await eff.fit_prompt("hello") == "hello"
    assert eff.health_calls == 0 and eff.count_calls == 0


@pytest.mark.asyncio
async def test_a_prompt_that_fits_the_window_is_untouched():
    eff = _Served(262144)
    big = "x" * 400_000  # ~100k tokens: over the smallest window, inside this one
    assert await eff.fit_prompt(big) is big
    assert eff.reports == []


@pytest.mark.asyncio
async def test_a_session_turn_counts_what_the_session_holds():
    eff = _Served(262144)
    prompt = "A" + "y" * 80_000 + "Z"  # ~20k tokens
    out = await eff.fit_prompt(
        prompt, used=250_000, config_overrides={"max_tokens": 2048}, session_id="s1"
    )
    assert out != prompt and out.startswith("A") and out.endswith("Z")
    assert "[BACKSTOP:" in out and "262,144-token window" in out
    (r,) = eff.reports
    assert r["session_id"] == "s1" and r["used"] == 250_000 and r["reserve"] == 2048
    assert r["bounded"] is True and r["how"] == "exact"
    # What was kept fits what was free.
    free = 262144 - 250_000 - 2048
    assert (len(out) // 4) * 1.1 <= free + 100


@pytest.mark.asyncio
async def test_a_full_context_is_reported_and_sent_as_it_is():
    eff = _Served(32768)
    prompt = "p" * 40_000
    out = await eff.fit_prompt(prompt, used=32_000)
    assert out is prompt
    (r,) = eff.reports
    assert r["bounded"] is False


@pytest.mark.asyncio
async def test_the_static_prefix_is_never_cut():
    eff = _Served(65536)
    static = "S" * 100_000  # ~27.5k tokens with the margin
    prompt = "B" + "d" * 200_000 + "E"
    out = await eff.fit_prompt(prompt, static)
    assert out.startswith("B") and out.endswith("E") and len(out) < len(prompt)
    assert eff.reports[0]["static_tokens"] == int(25_000 * 1.1)


@pytest.mark.asyncio
async def test_an_unknown_window_falls_back_to_the_character_ceiling():
    eff = _Served(0)
    big = "x" * (PROMPT_CHAR_CEILING + 1000)
    out = await eff.fit_prompt(big)
    assert len(out) <= PROMPT_CHAR_CEILING + 200 and "BACKSTOP" in out  # + marker


@pytest.mark.asyncio
async def test_a_named_registry_model_is_not_sized_by_the_local_window():
    eff = _Served(32768, model="remote-provider-model")
    big = "x" * 200_000
    assert await eff.fit_prompt(big) is big  # under the character ceiling
    assert eff.health_calls == 0


@pytest.mark.asyncio
async def test_the_window_is_cached():
    eff = _Served(262144)
    for _ in range(3):
        await eff.fit_prompt("x" * 200_000)
    assert eff.health_calls == 1


@pytest.mark.asyncio
async def test_local_effects_passes_session_occupancy_and_records_the_row(tmp_path):
    from agent.effects.local import LocalEffects
    from agent.effects.protocol import InferenceResult

    fx = LocalEffects(str(tmp_path), history_mode="off")
    seen: dict = {}

    class _Fake(_Served):
        async def session_turn(
            self, session_id, prompt, config_overrides=None, *, session_used=0
        ):
            seen["used"] = session_used
            await self.fit_prompt(prompt, used=session_used, session_id=session_id)
            return InferenceResult(text="ok", tokens_generated=1)

    fake = _Fake(32768)
    fake.on_backstop = fx._record_backstop
    events: list = []

    async def _emit(ev):
        events.append(ev)

    fx._inference = fake
    fx.emit_trace = _emit
    fx._session_tokens["s9"] = 30_000
    await fx.session_inference("s9", "q" * 20_000)
    assert seen["used"] == 30_000
    rows = [e for e in events if getattr(e, "event_type", "") == "prompt_backstop"]
    assert len(rows) == 1 and rows[0].session_id == "s9" and rows[0].bounded is False
