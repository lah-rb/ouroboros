"""S2 of τ piece 4: MenuBoss (JSON menu decisions) + ChatEnv (episode loop).

Fully offline — scripted fake sessions/workers/channels, no server."""

from __future__ import annotations

import asyncio

from agent.chat.boss import MenuBoss, MenuOption
from agent.chat.env import ChatEnv, WorkerReport


# ── MenuBoss ───────────────────────────────────────────────────────────


class ScriptedSession:
    """Returns queued strings turn by turn; records prompts."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def turn(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.replies.pop(0)


_OPTS = [
    MenuOption("instruct", "dispatch a work order", arg="directive"),
    MenuOption("end_episode", "close the case", arg="reason"),
]


def _boss(replies, **kw):
    return MenuBoss(ScriptedSession(replies), _OPTS, briefing="BRIEF", **kw)


def test_boss_parses_compound_choice_and_arg():
    b = _boss(['{"choice": "instruct", "directive": "look up order #W1"}'])
    d = asyncio.run(b.decide("customer wants an exchange"))
    assert d.choice == "instruct" and d.arg == "look up order #W1"
    assert d.attempts == 1 and not d.fallback
    # Briefing rode the first prompt; the update too.
    assert "BRIEF" in b.session.prompts[0]
    assert "customer wants an exchange" in b.session.prompts[0]


def test_boss_briefing_only_on_first_turn():
    b = _boss([
        '{"choice": "instruct", "directive": "a"}',
        '{"choice": "end_episode", "reason": "done"}',
    ])
    asyncio.run(b.decide("u1"))
    asyncio.run(b.decide("u2"))
    assert "BRIEF" in b.session.prompts[0]
    assert "BRIEF" not in b.session.prompts[1]


def test_boss_retries_then_parses():
    b = _boss([
        "I think we should probably look up the order first, no JSON here",
        '{"choice": "instruct", "directive": "verify identity"}',
    ])
    d = asyncio.run(b.decide("u"))
    assert d.choice == "instruct" and d.attempts == 2 and not d.fallback
    assert "not a single valid JSON" in b.session.prompts[1]  # retry nudge


def test_boss_falls_back_to_safe_default_after_exhausting_retries():
    b = _boss(["nope", "still prose", "no json at all"],
              default_choice="instruct", default_arg="proceed per policy")
    d = asyncio.run(b.decide("u"))
    assert d.fallback and d.choice == "instruct" and d.arg == "proceed per policy"
    assert b.fallback_rate == 1.0


def test_boss_handles_fenced_and_prefixed_json():
    b = _boss(['analysis... ```json\n{"choice":"end_episode","reason":"resolved"}\n```'])
    d = asyncio.run(b.decide("u"))
    assert d.choice == "end_episode" and d.arg == "resolved"


# ── ChatEnv ──────────────────────────────────────────────────────────


class FakeBoss:
    """Emits a scripted list of (choice, arg) decisions."""

    def __init__(self, script):
        self.script = list(script)
        self.seen_updates = []

    async def decide(self, update):
        self.seen_updates.append(update)
        choice, arg = self.script.pop(0)

        class D:
            pass

        d = D()
        d.choice, d.arg, d.fallback = choice, arg, False
        return d


class FakeWorker:
    def __init__(self, replies=None, ok=True):
        self.replies = list(replies) if replies else None
        self.ok = ok
        self.directives = []

    async def execute(self, directive, transcript):
        self.directives.append(directive)
        reply = self.replies.pop(0) if self.replies else f"handled: {directive}"
        return WorkerReport(reply=reply, ok=self.ok, notes="called get_order_details")


class FakeChannel:
    """Returns queued (user_reply, done) tuples."""

    def __init__(self, script):
        self.script = list(script)
        self.delivered = []

    def deliver(self, reply):
        self.delivered.append(reply)
        return self.script.pop(0)


def test_episode_terminates_on_user_stop():
    boss = FakeBoss([("instruct", "d1"), ("instruct", "d2")])
    worker = FakeWorker()
    channel = FakeChannel([("tell me more", False), ("thanks! ###STOP###", True)])
    rec = asyncio.run(ChatEnv(boss, worker, channel, max_turns=5).run_episode("hi"))
    assert rec.termination == "user_stop" and rec.turns == 2
    assert worker.directives == ["d1", "d2"]
    # Second boss update carried the operator note + the customer reply.
    assert "get_order_details" in boss.seen_updates[1]
    assert "tell me more" in boss.seen_updates[1]


def test_episode_terminates_on_boss_end_with_courtesy_reply():
    boss = FakeBoss([("instruct", "d1"), ("end_episode", "resolved")])
    worker = FakeWorker(replies=["working on it", "you're all set, goodbye"])
    channel = FakeChannel([("ok", False), ("bye", False)])
    rec = asyncio.run(ChatEnv(boss, worker, channel, max_turns=5).run_episode("hi"))
    assert rec.termination == "boss_end"
    # The closing directive ran as a worker turn (the courtesy message).
    assert worker.directives[-1].startswith("The supervisor has closed")
    assert rec.transcript[-2]["closing"] is True


def test_episode_hits_turn_cap():
    boss = FakeBoss([("instruct", f"d{i}") for i in range(5)])
    worker = FakeWorker()
    channel = FakeChannel([("more", False)] * 5)
    rec = asyncio.run(ChatEnv(boss, worker, channel, max_turns=3).run_episode("hi"))
    assert rec.termination == "turn_cap" and rec.turns == 3


def test_episode_counts_worker_failures_and_flags_update():
    boss = FakeBoss([("instruct", "d1"), ("instruct", "d2")])
    worker = FakeWorker(ok=False)
    channel = FakeChannel([("hmm", False), ("ok ###STOP###", True)])
    rec = asyncio.run(ChatEnv(boss, worker, channel, max_turns=5).run_episode("hi"))
    assert rec.worker_failures == 2
    assert "technical fault" in boss.seen_updates[1]
