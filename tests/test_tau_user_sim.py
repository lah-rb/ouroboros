"""S1 of τ piece 4: persona plumbing + PersonaSession + SessionUserSim.

Offline: InferenceEffect is faked (no server, no llama). The live path is
exercised by dev/tau_episode smoke (S4)."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("tau_bench", reason="tau-bench not installed")


# ── persona plumbing: InferenceEffect.start_session sends persona ──────


def test_start_session_forwards_persona(monkeypatch):
    from agent.effects.inference import InferenceEffect

    sent = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"startSession": {"sessionId": "s-1"}}}

    class FakeClient:
        async def post(self, endpoint, json):
            sent.update(json)
            return FakeResponse()

    fx = InferenceEffect(endpoint="http://x/graphql")

    async def fake_get_client():
        return FakeClient()

    monkeypatch.setattr(fx, "_get_client", fake_get_client)

    sid = asyncio.run(
        fx.start_session(config={"ttl_seconds": 60, "persona": "user_sim"})
    )
    assert sid == "s-1"
    cfg = sent["variables"]["config"]
    assert cfg["persona"] == "user_sim" and cfg["ttlSeconds"] == 60

    # Absent persona → field not sent (default-persona behavior unchanged).
    sent.clear()
    asyncio.run(fx.start_session(config={"ttl_seconds": 60}))
    assert "persona" not in sent["variables"]["config"]


# ── PersonaSession: defaults, heal-once, teardown ──────────────────────


class FakeFx:
    def __init__(self, fail_first_turn=False):
        self.fail_first_turn = fail_first_turn
        self.turn_calls = []
        self.started = 0
        self.ended = 0
        self.closed = False

    async def start_session(self, config=None, **kw):
        self.started += 1
        self.last_config = config
        return f"s-{self.started}"

    async def session_turn(self, session_id, prompt, cfg):
        from agent.effects.inference import InferenceError

        self.turn_calls.append((session_id, prompt, dict(cfg or {})))
        if self.fail_first_turn and len(self.turn_calls) == 1:
            raise InferenceError("decode wedged")

        class R:
            text = f"reply-to::{prompt[:24]}"

        return R()

    async def end_session(self, session_id):
        self.ended += 1
        return True

    async def close(self):
        self.closed = True


def _mk_session(fake) -> "object":
    from agent.chat.session import PersonaSession

    s = PersonaSession("http://x/graphql", "tau_boss",
                       temperature=0.35, max_tokens=400)
    s._fx = fake
    return s


def test_persona_session_turn_defaults_and_lazy_start():
    fake = FakeFx()
    s = _mk_session(fake)
    out = asyncio.run(s.turn("hello"))
    assert out.startswith("reply-to::")
    assert fake.last_config == {"ttl_seconds": 3600, "persona": "tau_boss"}
    _, _, cfg = fake.turn_calls[0]
    assert cfg == {"temperature": 0.35, "max_tokens": 400}
    # Per-turn override wins.
    asyncio.run(s.turn("again", max_tokens=64))
    assert fake.turn_calls[1][2]["max_tokens"] == 64


def test_persona_session_heals_once_on_inference_error():
    fake = FakeFx(fail_first_turn=True)
    s = _mk_session(fake)
    out = asyncio.run(s.turn("hello"))
    assert out.startswith("reply-to::")
    assert s.heals == 1 and fake.started == 2 and fake.ended == 1
    assert "interrupted by a technical" in fake.turn_calls[1][1]


def test_persona_session_close_is_idempotent_and_never_raises():
    fake = FakeFx()
    s = _mk_session(fake)
    asyncio.run(s.turn("x"))
    asyncio.run(s.close())
    asyncio.run(s.close())
    assert fake.closed and fake.ended == 1


# ── SessionUserSim: contract + stop-rule injection ─────────────────────


def test_session_user_sim_reset_injects_scenario_and_stop_rule(monkeypatch):
    from tau_adapter import user_sim as us

    prompts = []

    class FakePersonaSession:
        def __init__(self, *a, **kw):
            pass

        async def turn(self, prompt, **kw):
            prompts.append(prompt)
            return "  I'd like to exchange my keyboard.  "

        async def close(self):
            pass

    monkeypatch.setattr(us, "PersonaSession", FakePersonaSession)
    sim = us.SessionUserSim(endpoint="http://x/graphql")
    try:
        opening = sim.reset(instruction="You are Yusuf; exchange order #W1.")
        assert opening == "I'd like to exchange my keyboard."
        assert "###STOP###" in prompts[0]  # the injected stop rule
        assert "You are Yusuf" in prompts[0]
        assert us.OPENER in prompts[0]

        reply = sim.step("Sure — what's your order number?")
        assert "The service agent says:" in prompts[1]
        assert reply == "I'd like to exchange my keyboard."
        assert sim.get_total_cost() == 0.0
    finally:
        sim.close()


def test_make_env_session_mode_swaps_user_without_llm(monkeypatch):
    """user='session' must construct with the no-LLM human strategy and
    install SessionUserSim — zero network at construction."""
    from tau_adapter import env as tau_env

    class FakeSim:
        pass

    created = {}

    def fake_session_user_sim(*a, **kw):
        created["yes"] = True
        return FakeSim()

    import tau_adapter.user_sim as us

    monkeypatch.setattr(us, "SessionUserSim", fake_session_user_sim)
    env = tau_env.make_env("retail", task_index=0, user="session")
    assert created.get("yes") and isinstance(env.user, FakeSim)
    # tools/tasks intact (real env underneath).
    assert len(env.tools_info) > 5 and env.wiki
