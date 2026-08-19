"""Session-layer behavior on a degenerate turn (the Gemma-4 repetition gate).

Guards the safety-critical invariant: when the backend raises
DegenerateGenerationError mid-generation, ``session_turn`` must
  * NOT persist the degenerate turn (history never extended, no state saved),
  * leave turn_count untouched,
  * re-raise so the failure surfaces cleanly upstream.

And the happy path advances the turn. Non-resident sessions are FULL REPLAY
by construction since 2026-07-30 — the legacy save_state/load_state splice
was deleted (OPEN_TASKS §4); guards below pin the deletion.

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
        self.reset_calls = 0

    def load_state(self, state):
        self.load_calls.append(state)

    def save_state(self):
        self.save_calls += 1
        return "POST_STATE"

    def reset(self):
        self.reset_calls += 1


class FakeBackend:
    """Backend whose generate_stream_async is supplied per-test.

    Carries its own config (Phase 2a: the manager reads
    self._backend.config, never the global)."""

    def __init__(self, gen_factory, config=None):
        self._gen_factory = gen_factory
        self.config = config if config is not None else _FakeConfig()

    def generate_stream_async(self, **kwargs):
        return self._gen_factory()


class _FakeRenderer:
    def render_turn_transition_segments(self):
        return []

    def render_user_segments(self, prompt):
        return [(prompt, False)]

    def render_generation_prompt_segments(self, reasoning=None):
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
    monkeypatch.setattr(sm, "_get_format_renderer", lambda family: _FakeRenderer())
    monkeypatch.setattr(sm, "get_cached_tokenizer", lambda: object())
    monkeypatch.setattr(sm, "tokenize_segments", lambda tok, segs: [1, 2, 3])


def _make_session(mgr):
    inst = FakeInstance()
    sess = SessionState(instance=inst)
    mgr._sessions["s1"] = sess
    return inst, sess


def test_degenerate_turn_never_persists_and_never_splices():
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
    assert inst.save_calls == 0, "the legacy splice is deleted — nothing saves"
    assert inst.load_calls == [], "no evolving state may ever be loaded"
    assert inst.reset_calls == 1, "full-replay restore (no static_state stub)"
    assert sess.token_history == [], "a degenerate turn never enters history"
    assert sess.turn_count == 0, "a degenerate turn must not advance turn_count"


def test_normal_turn_advances_without_state_surgery():
    async def good_gen():
        for ch in ("hello ", "world"):
            yield ch

    mgr = SessionManager(FakeBackend(good_gen))
    inst, sess = _make_session(mgr)

    async def drive():
        return [c async for c in mgr.session_turn("s1", "hi", max_tokens=64)]

    chunks = asyncio.run(drive())

    assert "".join(chunks) == "hello world"
    assert inst.save_calls == 0, "the legacy splice is deleted — nothing saves"
    assert sess.token_history == [1, 2, 3], "the turn enters history instead"
    assert sess.turn_count == 1
    assert sess.last_assistant_text == "hello world"


def test_legacy_splice_is_deleted():
    """The save_state/load_state per-turn splice must stay gone: no
    current_state field, no load of evolving session state in the source."""
    import inspect

    assert "current_state" not in {
        f.name for f in __import__("dataclasses").fields(SessionState)
    }
    src = inspect.getsource(sm.SessionManager.session_turn)
    assert "session.current_state" not in src
    assert "save_state()" not in src, "no state may be captured mid-turn"
    assert "instance.load_state" not in src.replace(
        "instance.load_state, static", ""
    ), "the only load_state target allowed is the pristine static"


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

    mgr = SessionManager(_SpyBackend(good_gen, config=_FloorConfig()))
    inst, sess = _make_session(mgr)
    sess.turn_count = 2  # third turn — floor engages

    async def drive():
        return [
            c
            async for c in mgr.session_turn("s1", "hi", max_tokens=64, temperature=0.2)
        ]

    chunks = asyncio.run(drive())

    assert chunks == ["ok"]  # the turn completes (no NameError)
    assert seen_temps == [0.5]  # and the floored temperature reached the backend


# ── full-replay session policy (hybrid/recurrent models) ─────────────


class _ReplayModel(_FakeModel):
    pass


class _ReplayConfig(_FakeConfig):
    model = _ReplayModel()


class _SpyBackend(FakeBackend):
    """Records every generate call's prompt_tokens; exposes static_state."""

    static_state = None  # manager falls back to instance.reset()

    def __init__(self, gen_factory):
        super().__init__(gen_factory)
        self.prompts_seen: list[list[int]] = []

    def generate_stream_async(self, **kwargs):
        self.prompts_seen.append(list(kwargs.get("prompt_tokens") or []))
        return self._gen_factory()


class _ReplayInstance(FakeInstance):
    def __init__(self):
        super().__init__()
        self.reset_calls = 0
        self._last_completion_tokens = [9]  # generated ids each turn

    def reset(self):
        self.reset_calls += 1


def _with_replay_config(backend, fn):
    # Phase 2a: the manager reads its backend's config, so the replay
    # policy is injected there — no global swapping.
    backend.config = _ReplayConfig()
    return fn()


def test_full_replay_reprefills_history_and_skips_state_surgery():
    """Turn N's prompt_tokens = full history + new turn; no save_state,
    no evolving load_state — only reset (static fallback) per turn. The
    one rollback a recurrent model supports."""

    async def good_gen(**kwargs):
        yield "ok"

    backend = _SpyBackend(good_gen)
    mgr = SessionManager(backend)
    inst = _ReplayInstance()
    sess = SessionState(instance=inst)
    mgr._sessions["s1"] = sess

    def drive():
        async def turns():
            out = []
            for prompt in ("first", "second"):
                out.append(
                    [c async for c in mgr.session_turn("s1", prompt, max_tokens=8)]
                )
            return out

        return asyncio.run(turns())

    _with_replay_config(backend, drive)

    # tokenize_segments is stubbed to [1,2,3] per turn.
    assert backend.prompts_seen[0] == [1, 2, 3]
    # Turn 2 re-prefills turn 1's tokens + its generated ids, then turn 2.
    assert backend.prompts_seen[1] == [1, 2, 3, 9, 1, 2, 3]
    assert inst.save_calls == 0, "full replay must never save_state"
    assert inst.load_calls == [], "must never load evolving session state"
    assert inst.reset_calls == 2, "static fallback reset once per turn"
    assert sess.turn_count == 2
    assert sess.token_history == [1, 2, 3, 9, 1, 2, 3, 9]


def test_full_replay_degenerate_turn_drops_from_history():
    calls = {"n": 0}

    def gen_factory(**kwargs):
        async def degen():
            yield "partial"
            raise DegenerateGenerationError("cycle period 3 x 12", tokens_generated=36)

        async def good(**kw):
            yield "ok"

        calls["n"] += 1
        return degen() if calls["n"] == 1 else good()

    backend = _SpyBackend(gen_factory)
    mgr = SessionManager(backend)
    inst = _ReplayInstance()
    sess = SessionState(instance=inst)
    mgr._sessions["s1"] = sess

    def drive():
        async def run():
            with pytest.raises(DegenerateGenerationError):
                async for _ in mgr.session_turn("s1", "bad", max_tokens=8):
                    pass
            return [c async for c in mgr.session_turn("s1", "good", max_tokens=8)]

        return asyncio.run(run())

    _with_replay_config(backend, drive)

    assert sess.turn_count == 1, "degenerate turn must not advance"
    # The degenerate turn's tokens never entered history: the good turn's
    # prompt is pristine (no [1,2,3] residue from the failed attempt).
    assert backend.prompts_seen[1] == [1, 2, 3]
    assert inst.load_calls == [], "no purge load needed in full replay"


def test_global_temperature_floor_clamps_any_request():
    from core.session_manager import _global_temperature_floor

    class Cfg:
        temperature_floor = 0.4

    assert _global_temperature_floor(0.08, Cfg()) == 0.4
    assert _global_temperature_floor(0.24, Cfg()) == 0.4
    assert _global_temperature_floor(0.7, Cfg()) == 0.7

    class Off:
        temperature_floor = None

    assert _global_temperature_floor(0.08, Off()) == 0.08


# ══════════════════════════════════════════════════════════════════════
# Append fast path (2026-08-18) — full-replay sessions CONTINUE forward
# ══════════════════════════════════════════════════════════════════════
#
# The replay stream is append-only (turn N+1 == turn N + new tokens), so a
# session whose live KV still matches token_history can feed ONLY the new
# turn instead of restoring the static snapshot and re-feeding everything.
# Measured on a memory_can_shift=False model (dev/append_continuation_probe.py):
# 17.4x fewer tokens fed at depth 24, needle recalled identically on both
# paths. `memory_can_shift` gates REMOVAL/SHIFTING, which continuing does not
# need. The one divergence source is a degenerate turn: its span stays in the
# KV while token_history never took it, so the session is marked dirty and the
# NEXT turn restores + replays once before resuming the fast path.


def _ok_gen_factory(text="fine"):
    async def gen():
        yield text

    return gen


def _drive(mgr, prompt="hi"):
    async def run():
        return [c async for c in mgr.session_turn("s1", prompt, max_tokens=16)]

    return asyncio.run(run())


def test_first_turn_restores_static_then_later_turns_append():
    mgr = SessionManager(FakeBackend(_ok_gen_factory()))
    inst, sess = _make_session(mgr)
    mgr._backend.static_state = "STATIC"

    _drive(mgr)
    assert inst.load_calls == ["STATIC"], "turn 0 must establish the static prefix"
    assert sess.token_history, "history must record turn 0"

    # Turn 1: KV is live and clean — no restore, and the fed stream is the new
    # turn only (not history + turn).
    inst.n_tokens = 128
    _drive(mgr)
    assert inst.load_calls == ["STATIC"], "clean continuation must NOT restore"


def test_a_degenerate_turn_dirties_the_kv_and_forces_one_replay():
    """The degenerate span is resident in the KV but absent from history, so
    continuing forward would decode against poisoned state."""

    async def degen():
        yield "collapse"
        raise DegenerateGenerationError("long-cycle", tokens_generated=48)

    mgr = SessionManager(FakeBackend(_ok_gen_factory()))
    inst, sess = _make_session(mgr)
    mgr._backend.static_state = "STATIC"
    _drive(mgr)  # turn 0 — clean
    inst.n_tokens = 128

    mgr._backend._gen_factory = degen
    with pytest.raises(DegenerateGenerationError):
        _drive(mgr)
    assert sess.kv_dirty is True, "degenerate turn must dirty the KV"

    # Next turn restores the static snapshot and replays the clean history,
    # then clears the flag so the fast path resumes.
    mgr._backend._gen_factory = _ok_gen_factory()
    _drive(mgr)
    assert inst.load_calls == ["STATIC", "STATIC"], "dirty session must restore"
    assert sess.kv_dirty is False, "flag must clear after the replay turn"
