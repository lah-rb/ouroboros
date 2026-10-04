"""Whole if it fits, otherwise an index to drill into (agent/context_fit.py).

Operator ruling (2026-09-26): anything a model reads is shown WHOLE when it is
within 25% of the serving window AND fits in the free context; otherwise the
model gets an INDEX (a symbol outline, a data file's entries by pointer, a
text's line ranges) and drills into the part it needs. Never a silent cut.
These pin the rule on the operator's own cases and on each site that used to
cut: the escalation and router tools, data_patch, and the play-tester's world.
"""

from __future__ import annotations

import json

import pytest

from agent import context_fit as cf
from agent.effects.mock import MockEffects
from agent.effects.protocol import InferenceResult
from agent.models import FlowMeta, StepInput


class _Window(MockEffects):
    """A server that reports its window and counts ~4 chars per token."""

    def __init__(self, n_ctx: int, **kw):
        super().__init__(**kw)
        self._n = n_ctx

    async def cache_health(self):
        return {"nCtxSeq": self._n}

    async def token_count(self, texts, model=""):
        return [len(t) // 4 for t in texts]


# ── the rule ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "used,tokens,whole",
    [
        (5000, 2400, True),  # under the 2.5k share and fits the 5k free
        (8000, 2400, False),  # only 2k free
        (2000, 3000, False),  # over the 2.5k share even though 8k is free
    ],
)
def test_the_operators_cases(used, tokens, whole):
    assert cf.decide(tokens, window=10000, used=used) is whole


@pytest.mark.asyncio
async def test_fit_measures_with_the_server_and_adds_the_reserve():
    fx = _Window(40000)
    f = await cf.fit(fx, "x" * 4000, used=1000, reserve=500)
    assert f.window == 40000 and f.window_reported
    assert f.how == "exact" and f.used == 1500
    assert f.tokens == int(1000 * 1.10)  # the chat-template margin
    assert f.whole


@pytest.mark.asyncio
async def test_without_a_server_it_assumes_the_smallest_window_and_estimates():
    f = await cf.fit(MockEffects(), "x" * 400)
    assert f.window == cf.UNKNOWN_WINDOW and not f.window_reported
    assert f.how == "estimated" and f.tokens == 130


# ── indexes and parts ─────────────────────────────────────────────────

_WORLD = json.dumps(
    {
        "starting_room": "room_a",
        "rooms": [
            {"id": "room_a", "name": "Hall", "exits": [{"direction": "north"}]},
            {"id": "room_b", "name": "Crypt"},
        ],
        "monsters": {"monster_thrall": {"id": "monster_thrall", "weakness": "vial"}},
    },
    indent=2,
)


def test_a_data_index_lists_entries_by_pointer_with_their_ids():
    idx = cf.data_index("world.json", _WORLD, "/rooms")
    assert "/rooms/0" in idx and "id='room_a'" in idx and "name='Crypt'" in idx


@pytest.mark.parametrize(
    "path,content",
    [
        ("w.yaml", "rooms:\n  - id: a\n  - id: b\n"),
        ("w.toml", '[[rooms]]\nid = "a"\n\n[[rooms]]\nid = "b"\n'),
    ],
)
def test_pointers_work_on_every_supported_format(path, content):
    text, found = cf.read_data(path, content, "/rooms/1")
    assert found and "b" in text and "a" not in text


def test_a_wrong_pointer_returns_the_nearest_level_as_a_map():
    text, found = cf.read_data("world.json", _WORLD, "/rooms/9/name")
    assert not found and "no data at /rooms/9/name" in text and "/rooms/1" in text


def test_the_overview_reaches_two_levels():
    ov = cf.data_overview("world.json", _WORLD)
    assert "/rooms/1" in ov and "/monsters/monster_thrall" in ov


def test_read_part_reads_lines_symbols_and_pointers():
    src = "class A:\n    def f(self):\n        return 1\n\n\ndef g():\n    pass\n"
    assert cf.read_part("m.py", src, "2-3") == (
        "2:     def f(self):\n3:         return 1",
        True,
    )
    body, found = cf.read_part("m.py", src, "A.f")
    assert found and "return 1" in body
    miss, found = cf.read_part("m.py", src, "nope")
    assert not found and "g" in miss  # a wrong guess answers with the outline
    assert cf.read_part("world.json", _WORLD, "rooms/1")[1] is True


# ── tool reads and output (escalation, router) ───────────────────────


@pytest.mark.asyncio
async def test_read_file_view_is_whole_when_it_fits_and_an_index_when_not():
    big = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(400))
    fx = _Window(8192, files={"small.py": "def a():\n    pass\n", "big.py": big})
    whole, err = await cf.read_file_view(fx, "small.py", used=0)
    assert not err and "def a()" in whole
    indexed, err = await cf.read_file_view(fx, "big.py", used=0)
    assert not err and "too large to show whole here" in indexed
    assert "f399" in indexed and "return 399" not in indexed  # outline, no bodies
    part, err = await cf.read_file_view(fx, "big.py:f399", used=0)
    assert not err and "return 399" in part


@pytest.mark.asyncio
async def test_output_too_large_is_saved_and_indexed_then_readable_by_range():
    out = "".join(f"line {i}\n" for i in range(4000)) + "Traceback: the real error\n"
    fx = _Window(8192)
    shown = await cf.output_view(
        fx, out, used=0, save_path=".agent/outputs/x.txt", label="the output"
    )
    assert "too large to show whole here" in shown and "4001 lines." in shown
    assert len(shown) < len(out) and "line 1999\n" not in shown  # indexed, not cut
    assert "4001-4001  starts: Traceback" in shown  # the index points at the error
    view, err = await cf.read_file_view(fx, ".agent/outputs/x.txt:3990-4001", used=0)
    assert not err and "the real error" in view


@pytest.mark.asyncio
async def test_the_escalation_tools_read_parts_and_index_big_output():
    from agent.actions.escalation_actions import (
        action_escalation_read,
        action_escalation_run,
    )
    from agent.effects.protocol import CommandResult

    class _Fx(_Window):
        async def run_command(self, cmd, **kw):
            return CommandResult(
                command="make test",
                return_code=1,
                stdout="".join(f"step {i}\n" for i in range(6000)) + "FAIL: boom\n",
                stderr="",
            )

    fx = _Fx(16384, files={"world.json": _WORLD})

    def _si(arg):
        return StepInput(
            context={"escalation_choice_arg": arg, "escalation_session_id": "e1"},
            meta=FlowMeta(flow_name="escalate", step_id="work"),
            effects=fx,
        )

    out = await action_escalation_read(_si("world.json:/rooms/1"))
    assert out.result["action_ok"] and "Crypt" in str(out.context_updates)
    out = await action_escalation_run(_si("make test"))
    note = str(out.context_updates)
    assert "too large to show whole here" in note
    assert ".agent/outputs/escalation-e1-0-run.txt" in note
    saved = await fx.read_file(".agent/outputs/escalation-e1-0-run.txt")
    assert saved.content.endswith("FAIL: boom")  # nothing lost


# ── session occupancy ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_effects_track_what_a_session_holds(tmp_path):
    from agent.effects.local import LocalEffects

    class _Inf:
        async def session_turn(self, sid, prompt, cfg=None, *, session_used=0):
            return InferenceResult(
                text="ok", tokens_generated=10, prompt_tokens=900, generated_tokens=100
            )

        async def end_session(self, sid):
            return True

    (tmp_path / ".agent").mkdir()
    eff = LocalEffects(str(tmp_path), history_mode="off")
    eff._get_inference = lambda domain="": _Inf()  # type: ignore[method-assign]
    assert eff.session_tokens("s1") == 0
    await eff.session_inference("s1", "p")
    assert eff.session_tokens("s1") == 1000
    assert await cf.session_used(eff, "s1") == 1000
    await eff.end_inference_session("s1")
    assert eff.session_tokens("s1") == 0


# ── data_patch ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("n_ctx,whole", [(262144, True), (4096, False)])
async def test_data_patch_shows_the_file_whole_or_by_pointer(n_ctx, whole):
    from agent.actions.data_ops_actions import action_translate_data_ops_turn

    rooms = [{"id": f"room_{i}", "desc": "d" * 60} for i in range(80)]
    content = json.dumps({"rooms": rooms}, indent=2)
    prompts: list[str] = []

    class _Fx(_Window):
        async def run_inference(self, prompt, config_overrides=None, **kw):
            prompts.append(prompt)
            return InferenceResult(text="[]", tokens_generated=1)

    fx = _Fx(n_ctx, files={"world.json": content})
    await action_translate_data_ops_turn(
        StepInput(
            params={"target_file_path": "world.json", "change_spec": "rename room_3"},
            meta=FlowMeta(flow_name="data_patch", step_id="translate"),
            effects=fx,
        )
    )
    (prompt,) = prompts
    assert (content in prompt) is whole
    if not whole:
        assert "/rooms/79" in prompt and "id='room_79'" in prompt


# ── the play-tester's world ───────────────────────────────────────────


def _ictx(world: str) -> dict:
    return {
        "objective": "Build a game.",
        "run_command": "python main.py",
        "data_file_contents": {"world.json": world},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("n_ctx,indexed", [(262144, False), (8192, True)])
async def test_the_world_is_whole_when_it_fits_else_an_overview(n_ctx, indexed):
    from agent.actions.interactive_actions import action_size_world_view

    rooms = [
        {"id": f"room_{i}", "name": f"Room {i}", "d": "x" * 200} for i in range(40)
    ]
    world = json.dumps({"starting_room": "room_0", "rooms": rooms})
    out = await action_size_world_view(
        StepInput(
            inputs={"interaction_context": _ictx(world), "flow_directive": "Test go."},
            meta=FlowMeta(flow_name="interact", step_id="size_world"),
            effects=_Window(n_ctx),
        )
    )
    assert out.result["world_indexed"] is indexed
    shown = out.context_updates["interaction_context_view"]["data_file_contents"]
    if indexed:
        assert shown["world.json"] != world and "/rooms/39" in shown["world.json"]
    else:
        assert shown["world.json"] == world


@pytest.mark.asyncio
async def test_a_world_entry_is_pulled_whole_and_a_wrong_one_answers_with_a_map():
    from agent.actions.interactive_actions import action_fetch_world_entry

    def _si(ref, pulls=None):
        return StepInput(
            context={"world_request_arg": ref, "world_pulls": pulls or []},
            inputs={"interaction_context": _ictx(_WORLD)},
            meta=FlowMeta(flow_name="interact", step_id="fetch_world_entry"),
            effects=_Window(262144),
        )

    out = await action_fetch_world_entry(_si("world.json:/rooms/1"))
    assert out.result["fetched"] and "Crypt" in out.context_updates["world_pulls_block"]
    out2 = await action_fetch_world_entry(
        _si("world.json:rooms/0", out.context_updates["world_pulls"])
    )
    block = out2.context_updates["world_pulls_block"]
    assert "── world.json:/rooms/1 ──" in block and "── world.json:/rooms/0 ──" in block
    miss = await action_fetch_world_entry(_si("world.json:/rooms/5"))
    assert not miss.result["fetched"]
    assert "/rooms/1" in miss.context_updates["world_feedback"]
    other = await action_fetch_world_entry(_si("notes.json:/a"))
    assert "not an entry of the world" in other.context_updates["world_feedback"]


def test_interact_routes_through_the_world_sizing():
    with open("flows/compiled.json") as fh:
        steps = json.load(fh)["interact"]["steps"]
    assert steps["gather_context"]["resolver"]["rules"][0]["transition"] == "size_world"
    rules = {
        r["condition"]: r["transition"]
        for r in steps["size_world"]["resolver"]["rules"]
    }
    assert rules["result.world_indexed == true"] == "offer_world_menu"
    assert rules["true"] == "choose_charter"
    menu = steps["offer_world_menu"]["turn"]["transitions"]
    assert menu["options"] == {
        "pull_entry": "fetch_world_entry",
        "proceed": "choose_charter",
    }
    for step in ("plan_interaction", "plan_interaction_explore"):
        pc = steps[step]["pre_compute"][0]
        assert pc["params"]["source"] == {"$ref": "context.interaction_context_view"}
