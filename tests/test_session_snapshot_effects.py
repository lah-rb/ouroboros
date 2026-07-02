"""Agent-side snapshot plumbing: mock parity, traces, GraphQL variables.

The effects layer's contract for the semi-permanent snapshot tier:
session_snapshot/purge_inference_snapshot/start_inference_session(
from_snapshot=) behave identically (shape-wise) on MockEffects and
LocalEffects, and the GraphQL client sends the exact variable shapes
the server's schema expects.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.effects.mock import MockEffects

# ── mock parity ───────────────────────────────────────────────────────


def test_mock_snapshot_fork_seeds_turns():
    fx = MockEffects(inference_responses=["r1", "r2", "r3"])

    async def drive():
        sid = await fx.start_inference_session()
        await fx.session_inference(sid, "ingest the paper")
        info = await fx.session_snapshot(sid, "paper:x")
        await fx.end_inference_session(sid)

        forked = await fx.start_inference_session(from_snapshot="paper:x")
        await fx.session_inference(forked, "pack pass")
        return info, forked

    info, forked = asyncio.run(drive())
    assert info["key"] == "paper:x" and info["turn_count"] == 1
    # Fork seeded with the snapshot's turn, then its own appended.
    turns = fx._mock_sessions[forked]
    assert [t["prompt"] for t in turns] == ["ingest the paper", "pack pass"]


def test_mock_snapshot_survives_session_end_until_purge():
    fx = MockEffects(inference_responses=["a"])

    async def drive():
        sid = await fx.start_inference_session()
        await fx.session_snapshot(sid, "k")
        await fx.end_inference_session(sid)
        # Snapshot survives the session end...
        s2 = await fx.start_inference_session(from_snapshot="k")
        await fx.end_inference_session(s2)
        # ...until the explicit purge.
        assert await fx.purge_inference_snapshot("k") is True
        assert await fx.purge_inference_snapshot("k") is False
        with pytest.raises(KeyError):
            await fx.start_inference_session(from_snapshot="k")

    asyncio.run(drive())


def test_mock_duplicate_snapshot_key_raises():
    fx = MockEffects()

    async def drive():
        sid = await fx.start_inference_session()
        await fx.session_snapshot(sid, "k")
        with pytest.raises(RuntimeError, match="already exists"):
            await fx.session_snapshot(sid, "k")

    asyncio.run(drive())


# ── trace emission ────────────────────────────────────────────────────


def test_snapshot_trace_events_emitted_in_step_context():
    from agent.trace import step_context

    fx = MockEffects(inference_responses=["a"])
    events = []
    fx.emit_trace = lambda e: (events.append(e), _async_none())[1]

    async def drive():
        with step_context("m", 1, "curate_paper", "ingest"):
            sid = await fx.start_inference_session(from_snapshot=None)
            await fx.session_snapshot(sid, "paper:x")
            s2 = await fx.start_inference_session(from_snapshot="paper:x")
            await fx.end_inference_session(s2)

    asyncio.run(drive())
    kinds = [type(e).__name__ for e in events]
    assert "SessionSnapshot" in kinds
    snap = next(e for e in events if type(e).__name__ == "SessionSnapshot")
    assert snap.key == "paper:x"
    starts = [e for e in events if type(e).__name__ == "SessionStart"]
    assert starts[0].from_snapshot == "" and starts[1].from_snapshot == "paper:x"


async def _async_none():
    return None


# ── GraphQL variable shapes ───────────────────────────────────────────


def test_graphql_client_sends_expected_variables(monkeypatch):
    from agent.effects.inference import InferenceEffect

    sent = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            q = sent[-1]["json"]["query"]
            if "startSession" in q:
                return {"data": {"startSession": {"sessionId": "s1"}}}
            if "sessionSnapshot" in q:
                return {
                    "data": {
                        "sessionSnapshot": {
                            "key": "k",
                            "tokens": 42,
                            "resident": True,
                            "turnCount": 2,
                        }
                    }
                }
            return {"data": {"purgeSnapshot": True}}

    class _Client:
        async def post(self, endpoint, json=None):
            sent.append({"endpoint": endpoint, "json": json})
            return _Resp()

    client = InferenceEffect("http://x/graphql")

    async def _get_client():
        return _Client()

    monkeypatch.setattr(client, "_get_client", _get_client)

    async def drive():
        sid = await client.start_session({"ttl_seconds": 600}, from_snapshot="paper:x")
        info = await client.session_snapshot(sid, "k")
        purged = await client.purge_snapshot("k")
        return sid, info, purged

    sid, info, purged = asyncio.run(drive())
    assert sid == "s1" and purged is True
    assert info == {"key": "k", "tokens": 42, "resident": True, "turn_count": 2}

    start_vars = sent[0]["json"]["variables"]["config"]
    assert start_vars == {"ttlSeconds": 600, "fromSnapshot": "paper:x"}
    snap_vars = sent[1]["json"]["variables"]
    assert snap_vars == {"sessionId": "s1", "key": "k"}
    assert "fromSnapshot: String" not in sent[0]["json"]["query"]  # via $config
    assert "purgeSnapshot" in sent[2]["json"]["query"]
