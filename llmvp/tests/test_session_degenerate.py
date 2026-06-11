"""Session-layer behavior on a degenerate turn (the Gemma-4 repetition gate).

Guards the safety-critical invariant: when the backend raises
DegenerateGenerationError mid-generation, ``session_turn`` must
  * NOT persist the degenerate KV (skip save_state), and
  * PURGE the live instance by reloading the pre-turn snapshot,
  * leave turn_count / current_state untouched,
  * re-raise so the failure surfaces cleanly upstream.

And the happy path still saves state + advances the turn.

The tokenizer/renderer/config are stubbed so the test needs no model.
"""

import asyncio

import pytest

import core.session_manager as sm
from core.session_manager import SessionManager, SessionState
from inference.repetition import DegenerateGenerationError


class FakeInstance:
    def __init__(self):
        self.load_calls = []
        self.save_calls = 0

    def load_state(self, state):
        self.load_calls.append(state)

    def save_state(self):
        self.save_calls += 1
        return "POST_STATE"


class FakeBackend:
    """Backend whose generate_stream_async is supplied per-test."""

    def __init__(self, gen_factory):
        self._gen_factory = gen_factory

    def generate_stream_async(self, **kwargs):
        return self._gen_factory()


class _FakeRenderer:
    def render_turn_transition_segments(self):
        return []

    def render_user_segments(self, prompt):
        return [(prompt, False)]

    def render_generation_prompt_segments(self):
        return []

    def stop_tokens(self, mode=None):
        return []


class _FakeModel:
    family = "chatml"


class _FakeConfig:
    model = _FakeModel()


@pytest.fixture(autouse=True)
def _stub_session_deps(monkeypatch):
    """Stub the model-dependent helpers session_turn calls."""
    monkeypatch.setattr(sm, "get_config", lambda: _FakeConfig())
    monkeypatch.setattr(sm, "_get_format_renderer", lambda family: _FakeRenderer())
    monkeypatch.setattr(sm, "get_cached_tokenizer", lambda: object())
    monkeypatch.setattr(sm, "tokenize_segments", lambda tok, segs: [1, 2, 3])
    # Skip the Factor-4 reasoning strip (irrelevant here; needs a real tokenizer).
    monkeypatch.setenv("LLMVP_THINK_STRIP", "0")


def _make_session(mgr):
    inst = FakeInstance()
    sess = SessionState(instance=inst, current_state="PRE_STATE")
    mgr._sessions["s1"] = sess
    return inst, sess


def test_degenerate_turn_purges_and_skips_save():
    async def degen_gen():
        yield "partial-before-collapse"
        raise DegenerateGenerationError("run-length 48 of token 5", tokens_generated=48)

    mgr = SessionManager(FakeBackend(degen_gen))
    inst, sess = _make_session(mgr)

    async def drive():
        chunks = []
        with pytest.raises(DegenerateGenerationError):
            async for c in mgr.session_turn("s1", "hello", max_tokens=64):
                chunks.append(c)
        return chunks

    chunks = asyncio.run(drive())

    assert chunks == ["partial-before-collapse"]  # partial yielded, then abort
    assert inst.save_calls == 0, "degenerate KV must NOT be persisted"
    # load_state called twice with the pre-turn snapshot: restore at start + purge
    assert inst.load_calls == ["PRE_STATE", "PRE_STATE"]
    assert sess.current_state == "PRE_STATE", "current_state must stay pre-turn"
    assert sess.turn_count == 0, "a degenerate turn must not advance turn_count"


def test_normal_turn_saves_and_advances():
    async def good_gen():
        for ch in ("hello ", "world"):
            yield ch

    mgr = SessionManager(FakeBackend(good_gen))
    inst, sess = _make_session(mgr)

    async def drive():
        return [c async for c in mgr.session_turn("s1", "hi", max_tokens=64)]

    chunks = asyncio.run(drive())

    assert "".join(chunks) == "hello world"
    assert inst.save_calls == 1, "successful turn must persist state"
    assert sess.current_state == "POST_STATE"
    assert sess.turn_count == 1
    assert sess.last_assistant_text == "hello world"


# ── session temperature floor ─────────────────────────────────────────


def test_session_temp_floor_engages_at_depth():
    from core.session_manager import _effective_session_temperature

    class Cfg:
        session_temp_floor = 0.5
        session_temp_floor_after_turn = None  # default 2

    # Shallow turns keep the requested temperature.
    assert _effective_session_temperature(0.2, 0, Cfg()) == 0.2
    assert _effective_session_temperature(0.2, 1, Cfg()) == 0.2
    # Third turn onward is floored.
    assert _effective_session_temperature(0.2, 2, Cfg()) == 0.5
    assert _effective_session_temperature(0.2, 7, Cfg()) == 0.5
    # Requested temps above the floor are untouched.
    assert _effective_session_temperature(0.8, 7, Cfg()) == 0.8


def test_session_temp_floor_disabled_when_unset():
    from core.session_manager import _effective_session_temperature

    class Cfg:
        session_temp_floor = None
        session_temp_floor_after_turn = None

    assert _effective_session_temperature(0.1, 9, Cfg()) == 0.1


def test_session_temp_floor_custom_depth():
    from core.session_manager import _effective_session_temperature

    class Cfg:
        session_temp_floor = 0.4
        session_temp_floor_after_turn = 0  # every session turn

    assert _effective_session_temperature(0.1, 0, Cfg()) == 0.4


def test_floored_deep_turn_completes_through_session_turn():
    """REGRESSION: the floor's log line referenced an undefined name
    (`logger` vs this module's `log`), so the first time the floor ever
    APPLIED in production it raised NameError mid-turn, leaked the
    pinned session, and active=1/limit=1 deadlocked every session flow.
    The pure-function tests above never execute the logging branch —
    this drives the floor through session_turn itself."""

    class _GenCfg:
        session_temp_floor = 0.5
        session_temp_floor_after_turn = None  # default 2

    class _FloorConfig(_FakeConfig):
        generation = _GenCfg()

    async def good_gen(**kwargs):
        yield "ok"

    seen_temps = []

    class _SpyBackend(FakeBackend):
        def generate_stream_async(self, **kwargs):
            seen_temps.append(kwargs.get("temperature"))
            return self._gen_factory()

    mgr = SessionManager(_SpyBackend(good_gen))
    inst, sess = _make_session(mgr)
    sess.turn_count = 2  # third turn — floor engages

    import core.session_manager as _sm

    orig = _sm.get_config
    _sm.get_config = lambda: _FloorConfig()
    try:

        async def drive():
            return [
                c
                async for c in mgr.session_turn(
                    "s1", "hi", max_tokens=64, temperature=0.2
                )
            ]

        chunks = asyncio.run(drive())
    finally:
        _sm.get_config = orig

    assert chunks == ["ok"]  # the turn completes (no NameError)
    assert seen_temps == [0.5]  # and the floored temperature reached the backend
