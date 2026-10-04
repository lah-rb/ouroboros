"""Replay: a recorded prompt goes back to a model, and the answer is a new row.

No server: a fake InferenceEffect records exactly what it was sent. The tests
pin that a stateless turn is re-sent as recorded (static prefix + flow key
when the flow-KV split was used), with the recorded config unless overridden;
that a session turn refuses without ``--flatten-session`` and flattens in seq
order with it; that the new row links to the original in its own run; and
that a metrics-only turn has nothing to replay.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.effects.protocol import InferenceResult
from agent.history import reader
from agent.history.replay import format_results, plan_replay, replay
from agent.history.store import HistoryStore
from agent.trace import InferenceCall

_LOOP = asyncio.new_event_loop()


class _Fake:
    calls: list[dict] = []
    closed = 0

    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def run_inference(
        self, prompt, config_overrides=None, static_prefix=None, flow_key=None
    ):
        _Fake.calls.append(
            {
                "endpoint": self.endpoint,
                "prompt": prompt,
                "config": dict(config_overrides or {}),
                "static": static_prefix,
                "flow_key": flow_key,
            }
        )
        return InferenceResult(
            text="REPLAYED",
            tokens_generated=1,
            finished=True,
            request_id="ouro-r1",
            generated_tokens=1,
            end_reason="stop",
        )

    async def fetch_thinking(self, request_id=""):
        return "new thinking"

    async def close(self):
        _Fake.closed += 1


def _seed(tmp_path, *events, mode="full") -> str:
    (tmp_path / ".agent").mkdir(exist_ok=True)
    store = HistoryStore(str(tmp_path), "m1", mode)
    for e in events:
        store.ingest(e)
    _LOOP.run_until_complete(store.close("completed"))
    return str(tmp_path / ".agent")


def test_a_stateless_turn_is_resent_as_recorded_with_its_config(tmp_path):
    _Fake.calls.clear()
    agent_dir = _seed(
        tmp_path,
        InferenceCall(
            mission_id="m1",
            cycle=4,
            flow="design",
            step="draft",
            prompt_content="STATIC+DYN",
            prompt_static="STATIC+",
            prompt_dynamic="DYN",
            flow_key="design:draft:ab",
            response_content="ORIGINAL",
            tokens_out=7,
            temperature=0.3,
            max_tokens=500,
            reasoning="high",
            model="",
            endpoint="http://srv/graphql",
        ),
    )
    (orig,) = reader.load_turns(agent_dir)
    results = replay(str(tmp_path), orig["turn_id"], inference_factory=_Fake)
    assert len(results) == 1 and _Fake.calls == [
        {
            "endpoint": "http://srv/graphql",
            "prompt": "DYN",
            "config": {"temperature": 0.3, "max_tokens": 500, "reasoning": "high"},
            "static": "STATIC+",
            "flow_key": "design:draft:ab",
        }
    ]
    r = results[0]
    assert (
        r.mode == "stateless_cached"
        and r.response == "REPLAYED"
        and 0.0 <= r.ratio <= 1.0
    )
    turns = reader.load_turns(agent_dir)
    new = [t for t in turns if t["turn_id"] == r.new_turn_id][0]
    assert new["purpose"] == "replay" and new["replay_of"] == orig["turn_id"]
    assert (
        new["_history"]["run_id"].startswith("cli-replay-")
        and new["replay_mode"] == "stateless_cached"
    )
    assert (
        new["prompt_content"] == "STATIC+DYN"
        and new["thinking_content"] == "new thinking"
    )
    assert new["flow"] == "design" and new["step"] == "draft" and new["cycle"] == 4
    # the original run is untouched
    assert len(reader.load_turns(agent_dir, orig["_history"]["run_id"])) == 1
    assert _Fake.closed >= 1
    assert "similarity" in format_results(results)


def test_overrides_and_n_replays(tmp_path):
    _Fake.calls.clear()
    agent_dir = _seed(
        tmp_path,
        InferenceCall(
            mission_id="m1", prompt_content="P", response_content="R", temperature=0.7
        ),
    )
    (orig,) = reader.load_turns(agent_dir)
    results = replay(
        str(tmp_path),
        orig["turn_id"],
        model="other-model",
        temperature=0.0,
        max_tokens=64,
        endpoint="http://x/graphql",
        n=2,
        inference_factory=_Fake,
    )
    assert len(results) == 2 and len(_Fake.calls) == 2
    assert _Fake.calls[0] == {
        "endpoint": "http://x/graphql",
        "prompt": "P",
        "config": {"temperature": 0.0, "max_tokens": 64, "model": "other-model"},
        "static": None,
        "flow_key": None,
    }
    new = sorted(
        (t for t in reader.load_turns(agent_dir) if t.get("replay_of")),
        key=lambda t: t["_history"]["seq"],
    )
    assert [t["call_attempt"] for t in new] == [1, 2] and all(
        t["model"] == "other-model" for t in new
    )


def test_a_session_turn_refuses_without_flatten_and_flattens_in_order(tmp_path):
    _Fake.calls.clear()
    agent_dir = _seed(
        tmp_path,
        InferenceCall(
            mission_id="m1",
            session_id="s1",
            purpose="session_inference",
            prompt_content="Q1",
            response_content="A1",
            turn_committed=True,
        ),
        InferenceCall(
            mission_id="m1",
            session_id="s1",
            purpose="session_inference",
            prompt_content="Q-dropped",
            response_content="",
            turn_committed=False,
        ),
        InferenceCall(
            mission_id="m1",
            session_id="other",
            purpose="session_inference",
            prompt_content="X",
            response_content="Y",
        ),
        InferenceCall(
            mission_id="m1",
            session_id="s1",
            purpose="session_inference",
            prompt_content="Q2",
            response_content="A2",
            turn_committed=True,
        ),
        InferenceCall(
            mission_id="m1",
            session_id="s1",
            purpose="session_inference",
            prompt_content="Q3",
            response_content="A3",
        ),
    )
    turns = reader.load_turns(agent_dir)
    target = turns[-1]
    with pytest.raises(ValueError, match="flatten-session"):
        replay(str(tmp_path), target["turn_id"], inference_factory=_Fake)
    (r,) = replay(
        str(tmp_path), target["turn_id"], flatten_session=True, inference_factory=_Fake
    )
    assert r.mode == "session_flattened"
    assert (
        _Fake.calls[0]["prompt"] == "Q1\n\nA1\n\nQ2\n\nA2\n\nQ3"
    ), "prior COMMITTED turns of the same session, in order"


def test_a_metrics_only_turn_has_nothing_to_replay(tmp_path):
    agent_dir = _seed(
        tmp_path,
        InferenceCall(mission_id="m1", prompt_content="P", response_content="R"),
        mode="metrics",
    )
    (orig,) = reader.load_turns(agent_dir)
    with pytest.raises(ValueError, match="metrics"):
        plan_replay(agent_dir, orig["turn_id"])
    with pytest.raises(ValueError, match="no turn"):
        plan_replay(agent_dir, "nope")
