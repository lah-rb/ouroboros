"""adapters.tau: offline coverage (no LLM, no server).

The official Env constructor fires a user-sim LLM call, so these tests
exercise the layers BELOW it directly: domain data + tools, DB reset
isolation, the grader's hash primitive, EpisodeHandle mechanics over a
FakeEnv, and policy export. The live path (user sim on the LLMVP shim +
gold replay through the real Env) is dev/tau_gold_replay.py.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "tau_bench", reason="tau-bench not installed (uv pip install -e ~/Repos/tau-bench)"
)


# ── domain data + tools (real, offline) ───────────────────────────────


def test_retail_data_loads_and_tools_have_schemas():
    from tau_bench.envs.retail.data import load_data
    from tau_bench.envs.retail.tools import ALL_TOOLS

    data = load_data()
    assert {"orders", "users", "products"} <= set(data)
    infos = [t.get_info() for t in ALL_TOOLS]
    names = [i["function"]["name"] for i in infos]
    assert "get_order_details" in names and "cancel_pending_order" in names
    for i in infos:  # every tool presents an OpenAI function schema
        assert i["type"] == "function" and "parameters" in i["function"]


def test_retail_read_tool_invokes_against_data():
    from tau_bench.envs.retail.data import load_data
    from tau_bench.envs.retail.tools import ALL_TOOLS

    data = load_data()
    get_order = next(
        t for t in ALL_TOOLS if t.get_info()["function"]["name"] == "get_order_details"
    )
    order_id = next(iter(data["orders"]))
    out = get_order.invoke(data=data, order_id=order_id)
    assert order_id.strip("#") in out  # returns the order record


def test_write_tool_mutates_only_its_copy_and_reload_resets():
    from tau_bench.envs.retail.data import load_data
    from tau_bench.envs.retail.tools import ALL_TOOLS

    data = load_data()
    cancel = next(
        t
        for t in ALL_TOOLS
        if t.get_info()["function"]["name"] == "cancel_pending_order"
    )
    order_id = next(
        oid for oid, o in data["orders"].items() if o["status"] == "pending"
    )
    cancel.invoke(data=data, order_id=order_id, reason="no longer needed")
    assert data["orders"][order_id]["status"] == "cancelled"
    fresh = load_data()  # per-episode DB isolation = a fresh load
    assert fresh["orders"][order_id]["status"] == "pending"


def test_grader_hash_is_order_insensitive_and_content_sensitive():
    from tau_bench.envs.base import consistent_hash, to_hashable

    a = {"x": [1, 2], "y": {"k": "v"}}
    b = {"y": {"k": "v"}, "x": [1, 2]}
    assert consistent_hash(to_hashable(a)) == consistent_hash(to_hashable(b))
    c = {"x": [1, 3], "y": {"k": "v"}}
    assert consistent_hash(to_hashable(a)) != consistent_hash(to_hashable(c))


# ── EpisodeHandle over a FakeEnv ──────────────────────────────────────


class _FakeInfo:
    source = "fake"

    def model_dump(self):
        return {"source": self.source}


class _FakeRes:
    def __init__(self, obs, reward=0.0, done=False):
        self.observation = obs
        self.reward = reward
        self.done = done
        self.info = _FakeInfo()


class _FakeEnv:
    def __init__(self):
        self.tools_info = [{"type": "function", "function": {"name": "t"}}]
        self.wiki = "policy"
        self.stepped = []

    def reset(self, task_index=None):
        class R:
            observation = "hi, I need help"
            info = _FakeInfo()

        return R()

    def step(self, action):
        self.stepped.append((action.name, action.kwargs))
        if action.name == "respond" or action.name.endswith("respond"):
            return _FakeRes("###STOP###", reward=1.0, done=True)
        return _FakeRes(f"ran {action.name}")


def test_episode_handle_moves_and_termination():
    from adapters.tau.runner import EpisodeHandle

    h = EpisodeHandle(_FakeEnv())
    opening = h.reset(0)
    assert opening == "hi, I need help"
    out = h.call_tool("get_order_details", order_id="#W1")
    assert out == "ran get_order_details" and not h.done
    reply = h.respond("done!")
    assert "###STOP###" in reply and h.done and h.reward == 1.0
    with pytest.raises(RuntimeError, match="finished"):
        h.call_tool("t")
    r = h.result(0)
    assert r.reward == 1.0 and r.steps == 2
    assert [e["source"] for e in r.transcript][0] == "user"


# ── policy export ──────────────────────────────────────────────────────


def test_export_policy_writes_domain_wiki(tmp_path):
    from adapters.tau.env import export_policy

    out = export_policy("retail", out_path=tmp_path / "TAU_RETAIL.md")
    text = out.read_text()
    assert len(text) > 2000  # the real policy wiki, not a stub
    assert "cancel" in text.lower()
    with pytest.raises(ValueError, match="unknown tau domain"):
        export_policy("banking")


def test_respond_channel_ends_gracefully_when_handle_already_done():
    """A mission's tool calls can trip the episode step cap mid-turn; the
    channel must end the episode, not let respond() raise."""
    from adapters.tau.episode import _RespondChannel

    class DoneHandle:
        done = True

        def respond(self, text):
            raise AssertionError("respond must not be called when done")

    reply, done = _RespondChannel(DoneHandle()).deliver("anything")
    assert done is True and reply == ""
