"""Ingestion: every inference call becomes a turn row, through the effects.

The runtime no longer emits InferenceCall rows; the effect that made the call
does, through one builder, inside a bound step context. These tests pin the
wiring with a LocalEffects on a temp workspace and no server: the store opens
on the first event, turns land with full content, the run closes with the
ledger summary, and the two ways of naming a parallel branch agree.
"""

from __future__ import annotations

import glob
import os
import warnings

import pytest

from agent.effects.child import ChildEffects
from agent.effects.local import LocalEffects
from agent.effects.mock import MockEffects
from agent.effects.protocol import InferenceResult
from agent.history import reader
from agent.history.store import HistoryLocked
from agent.trace import (
    CycleEnd,
    CycleStart,
    InferenceCall,
    StepEnd,
    StepStart,
    annotate_turn,
    get_step_context,
    step_context,
)


class _FakeInference:
    """Stands in for InferenceEffect: returns a rich InferenceResult."""

    def __init__(self, text="the answer", thinking="because"):
        self.text = text
        self.thinking = thinking
        self.calls: list[dict] = []

    async def run_inference(
        self, prompt, config_overrides=None, static_prefix=None, flow_key=None
    ):
        self.calls.append(
            {"prompt": prompt, "static_prefix": static_prefix, "flow_key": flow_key}
        )
        return InferenceResult(
            text=self.text,
            tokens_generated=3,
            finished=True,
            request_id="ouro-abc123",
            cached_prefix_tokens=100,
            fresh_prefill_tokens=20,
            generated_tokens=3,
            cache_hit=True,
            prefill_ms=10.0,
            decode_ms=30.0,
            end_reason="stop",
        )

    async def session_turn(
        self, session_id, prompt, config_overrides=None, *, session_used=0
    ):
        self.calls.append({"session": session_id, "prompt": prompt})
        return InferenceResult(
            text=self.text,
            tokens_generated=3,
            finished=True,
            request_id=session_id,
            generated_tokens=3,
            session_turn_id=4,
            turn_committed=True,
            end_reason="stop",
        )

    async def fetch_thinking(self, request_id=""):
        return self.thinking

    async def health(self):
        return {}


def _effects(tmp_path, **kw) -> LocalEffects:
    (tmp_path / ".agent").mkdir(exist_ok=True)
    eff = LocalEffects(str(tmp_path), **kw)
    fake = _FakeInference()
    eff._get_inference = lambda domain="": fake  # type: ignore[method-assign]
    eff._fake = fake  # type: ignore[attr-defined]

    async def _no_health():
        return None

    eff._maybe_sample_server_health = _no_health  # type: ignore[method-assign]
    return eff


def _agent(tmp_path) -> str:
    return str(tmp_path / ".agent")


@pytest.mark.asyncio
async def test_a_run_inference_inside_a_step_is_a_turn_row_with_full_content(tmp_path):
    eff = _effects(tmp_path)
    with step_context("m1", 3, "design_and_plan", "draft", attempt=2, goal_id="g9"):
        with annotate_turn(
            purpose="step_inference",
            prompt_full="STATIC+DYNAMIC",
            prompt_static="STATIC+",
            prompt_dynamic="DYNAMIC",
            prompt_render_ms=4.0,
        ):
            await eff.run_inference(
                "DYNAMIC",
                {"temperature": 0.3, "max_tokens": 99},
                static_prefix="STATIC+",
                flow_key="design_and_plan:draft:ab",
            )
    await eff.history_close("completed")
    (turn,) = reader.load_turns(_agent(tmp_path))
    assert turn["prompt_content"] == "STATIC+DYNAMIC"
    assert turn["prompt_static"] == "STATIC+" and turn["prompt_dynamic"] == "DYNAMIC"
    assert turn["response_content"] == "the answer"
    assert turn["thinking_content"] == "because"
    assert turn["flow"] == "design_and_plan" and turn["step"] == "draft"
    assert turn["step_attempt"] == 2 and turn["goal_id"] == "g9"
    assert turn["request_id"] == "ouro-abc123" and turn["end_reason"] == "stop"
    assert turn["tokens_in"] == 120 and turn["tokens_out"] == 3  # real counts win
    assert turn["cache_hit"] is True and turn["prompt_render_ms"] == 4.0
    assert turn["temperature"] == 0.3 and turn["max_tokens"] == 99
    assert turn["purpose"] == "step_inference"
    # No JSONL trace directory is written any more.
    assert not os.path.isdir(os.path.join(_agent(tmp_path), "traces"))


@pytest.mark.asyncio
async def test_direct_run_inference_callers_get_a_row_too(tmp_path):
    """The 13 action modules that call run_inference themselves used to have
    no trace row at all."""
    eff = _effects(tmp_path)
    with step_context("m1", 0, "curate", "grade"):
        await eff.run_inference("grade this")
    await eff.history_close()
    (turn,) = reader.load_turns(_agent(tmp_path))
    assert turn["purpose"] == "action_inference" and turn["flow"] == "curate"


@pytest.mark.asyncio
async def test_outside_a_step_nothing_is_recorded(tmp_path):
    eff = _effects(tmp_path)
    assert get_step_context() is None
    await eff.run_inference("tooling call")
    await eff.history_close()
    assert reader.load_turns(_agent(tmp_path)) == []


@pytest.mark.asyncio
async def test_session_turns_carry_the_session_identity(tmp_path):
    eff = _effects(tmp_path)
    with step_context("m1", 1, "diagnose_issue", "probe"):
        await eff.session_inference("sess42", "why?", {"temperature": 0.7})
    await eff.history_close()
    (turn,) = reader.load_turns(_agent(tmp_path))
    assert turn["purpose"] == "session_inference"
    assert turn["session_id"] == "sess42" and turn["session_turn_id"] == 4
    assert turn["turn_committed"] is True and turn["request_id"] == "sess42"


@pytest.mark.asyncio
async def test_a_child_branch_is_named_on_the_parents_row(tmp_path):
    """ChildEffects.emit_trace stamps runtime events; an effect-emitted
    turn row needs the branch contextvar to carry the lane name."""
    eff = _effects(tmp_path)
    child = ChildEffects(eff, "lane:curate-2", inference_domain="")
    with step_context("m1", 0, "curate_drain", "grade"):
        await child.run_inference("grade")
        await child.session_inference("s1", "more")
    await eff.history_close()
    turns = reader.load_turns(_agent(tmp_path))
    assert [t["branch"] for t in turns] == ["lane:curate-2", "lane:curate-2"]


@pytest.mark.asyncio
async def test_flush_traces_writes_the_summary_head_and_compacts(tmp_path):
    eff = _effects(tmp_path)
    await eff.emit_trace(CycleStart(mission_id="m1", cycle=0, flow="mission_control"))
    with step_context("m1", 0, "design_and_plan", "draft"):
        await eff.emit_trace(StepStart(mission_id="m1", step="draft"))
        await eff.run_inference("p1")
        await eff.emit_trace(StepEnd(mission_id="m1", step="draft"))
    await eff.emit_trace(
        CycleEnd(
            mission_id="m1", cycle=0, flow="design_and_plan", cycle_duration_ms=50.0
        )
    )
    await eff.flush_traces()
    run_id = eff.history.run_id
    head = os.path.join(_agent(tmp_path), "history", "runs", f"{run_id}.summary.json")
    assert os.path.isfile(head)
    recorded = reader.load_summary(_agent(tmp_path), run_id)["summary"]
    assert recorded["counts"]["inferences"] == 1 and recorded["counts"]["cycles"] == 1
    # cycle compaction folded this cycle's event parts into one file
    files = glob.glob(
        os.path.join(
            _agent(tmp_path), "history", "events", f"run={run_id}", "*.parquet"
        )
    )
    assert len(files) == 1
    await eff.history_close("paused")
    (run,) = reader.list_runs(_agent(tmp_path))
    assert run["final_status"] == "paused" and run["turns"] == 1 and run["cycles"] == 1


@pytest.mark.asyncio
async def test_events_after_close_do_not_reopen_a_store(tmp_path):
    eff = _effects(tmp_path)
    await eff.emit_trace(CycleStart(mission_id="m1"))
    await eff.history_close()
    await eff.emit_trace(CycleStart(mission_id="m1"))
    assert eff.history is None
    assert len(reader.list_runs(_agent(tmp_path))) == 1


@pytest.mark.asyncio
async def test_metrics_mode_records_every_number_but_no_text(tmp_path):
    eff = _effects(tmp_path, history_mode="metrics")
    assert eff.capture_thinking is False
    with step_context("m1", 0, "f", "s"):
        await eff.run_inference("secret prompt")
    await eff.history_close()
    (turn,) = reader.load_turns(_agent(tmp_path))
    assert "prompt_content" not in turn and "response_content" not in turn
    assert turn["content_dropped"] is True and turn["tokens_out"] == 3


@pytest.mark.asyncio
async def test_off_mode_keeps_only_the_ledger(tmp_path):
    eff = _effects(tmp_path, history_mode="off")
    with step_context("m1", 0, "f", "s"):
        await eff.run_inference("p")
        await eff.emit_trace(InferenceCall(mission_id="m1", wall_ms=5.0))
    await eff.flush_traces()
    await eff.history_close()
    assert eff._ledger["counts"]["inferences"] == 2
    assert not reader.has_history(_agent(tmp_path))


def test_the_retired_trace_flags_warn_and_change_nothing(tmp_path):
    (tmp_path / ".agent").mkdir()
    with pytest.warns(DeprecationWarning):
        eff = LocalEffects(str(tmp_path), trace_prompts=True, trace_thinking=False)
    assert eff.history_mode == "full" and eff.capture_thinking is True
    with pytest.raises(ValueError):
        LocalEffects(str(tmp_path), history_mode="verbose")


@pytest.mark.asyncio
async def test_two_writers_on_one_workspace_is_loud(tmp_path):
    a = _effects(tmp_path)
    await a.emit_trace(CycleStart(mission_id="m1"))
    b = _effects(tmp_path)
    with pytest.raises(HistoryLocked):
        await b.emit_trace(CycleStart(mission_id="m1"))
    await a.history_close()


@pytest.mark.asyncio
async def test_mock_run_inference_emits_through_the_same_builder():
    eff = MockEffects(inference_responses=["consumed outside", "ok"])
    await eff.run_inference("outside")  # no step context: nothing recorded
    with step_context("m1", 2, "data_patch", "translate_ops"):
        with annotate_turn(purpose="llm_menu_resolve", call_attempt=2):
            await eff.run_inference("inside", {"temperature": 0.1})
    calls = [e for e in eff.trace_events if e.event_type == "inference_call"]
    assert len(calls) == 1
    ev = calls[0]
    assert ev.flow == "data_patch" and ev.prompt_content == "inside"
    assert ev.response_content == "ok" and ev.purpose == "llm_menu_resolve"
    assert ev.call_attempt == 2 and ev.temperature == 0.1


def test_no_warning_without_the_retired_flags(tmp_path):
    (tmp_path / ".agent").mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        LocalEffects(str(tmp_path))
