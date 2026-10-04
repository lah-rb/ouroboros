"""Agent side of llmvp's rewindSessionTurn.

Every failed session turn used to stay in the session's context — an empty
retry, a refused write, a truncated turn — and the next turn stacked on top of
it. The server can now take the LAST turn back by id (one level of undo); the
agent uses it (a) before a turn retry when the step opts in
(#Turn.rewind_on_retry) and (b) in the structural session walk before moving
past a file turn that left nothing usable. Both are best-effort: an older
server, or a turn the server already rolled out at commit, leaves the flow
exactly as it was.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.effects.protocol import InferenceResult
from agent.models import FlowDefinition, FlowMeta, StepInput, TurnDefinition
from agent.runtime import _execute_turn_inference


def _turn(rewind: bool) -> TurnDefinition:
    return TurnDefinition.model_validate(
        {
            "response_shape": "prose",
            "sections": [{"type": "instruction", "literal": "write it"}],
            "transitions": {"default": "done", "no_answer": "failed"},
            "response": {},
            "retries": 2,
            "rewind_on_retry": rewind,
        }
    )


class SessionEffects:
    def __init__(self, results: list[InferenceResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple] = []

    async def session_inference(self, session_id, prompt, config_overrides=None):
        self.calls.append(("turn", session_id))
        return self.results.pop(0)

    async def rewind_inference_session_turn(self, session_id, turn_id):
        self.calls.append(("rewind", session_id, turn_id))
        return {"ok": True, "reason": "rolled_back"}

    async def emit_trace(self, ev):
        pass


async def _run(turn: TurnDefinition, effects: SessionEffects):
    step = {"action": "inference", "description": "t", "turn": turn.model_dump()}
    flow = FlowDefinition.model_validate(
        {
            "flow": "t",
            "steps": {
                "s": step,
                "done": {
                    "action": "noop",
                    "description": "",
                    "terminal": True,
                    "status": "success",
                },
                "failed": {
                    "action": "noop",
                    "description": "",
                    "terminal": True,
                    "status": "failed",
                },
            },
            "entry": "s",
        }
    )
    step_input = StepInput(
        task="t",
        context={"inference_session_id": "sess-1"},
        config={},
        params={},
        meta=FlowMeta(flow_name="t", step_id="s"),
        effects=None,
        turn=turn,
    )
    return await _execute_turn_inference(
        step_def=flow.steps["s"],
        step_input=step_input,
        flow_def=flow,
        inputs={},
        effects=effects,
    )


def _r(text: str, turn_id: int | None, committed: bool = True) -> InferenceResult:
    return InferenceResult(
        text=text,
        tokens_generated=len(text),
        session_turn_id=turn_id,
        turn_committed=committed,
    )


@pytest.mark.asyncio
async def test_a_retry_rewinds_the_failed_attempt_first():
    fx = SessionEffects([_r("", 5), _r("the file", 6)])
    out = await _run(_turn(rewind=True), fx)
    assert fx.calls == [("turn", "sess-1"), ("rewind", "sess-1", 5), ("turn", "sess-1")]
    assert out.context_updates["inference_session_turn_id"] == 6
    assert out.context_updates["inference_turn_committed"] is True


@pytest.mark.asyncio
async def test_an_attempt_the_server_already_rolled_out_is_not_rewound():
    # An answerless turn is dropped server-side at commit — rewinding "the
    # last turn" then would take back the GOOD turn before it.
    fx = SessionEffects([_r("", 5, committed=False), _r("the file", 6)])
    await _run(_turn(rewind=True), fx)
    assert [c[0] for c in fx.calls] == ["turn", "turn"]


@pytest.mark.asyncio
async def test_rewind_is_opt_in():
    fx = SessionEffects([_r("", 5), _r("the file", 6)])
    await _run(_turn(rewind=False), fx)
    assert [c[0] for c in fx.calls] == ["turn", "turn"]


@pytest.mark.asyncio
async def test_an_older_server_without_turn_ids_just_retries():
    fx = SessionEffects([_r("", None), _r("the file", None)])
    await _run(_turn(rewind=True), fx)
    assert [c[0] for c in fx.calls] == ["turn", "turn"]


# ── the session walk ──────────────────────────────────────────────────


def _step_input(ctx: dict[str, Any], effects: Any) -> StepInput:
    return StepInput(
        task="t",
        context=ctx,
        config={},
        params={},
        meta=FlowMeta(flow_name="build_structure_session", step_id="x"),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_walk_rewinds_an_unusable_committed_turn():
    from agent.actions.session_structural_actions import action_rewind_session_turn

    fx = SessionEffects([])
    ctx = {
        "structural_session_id": "sess-9",
        "inference_session_turn_id": 12,
        "inference_turn_committed": True,
        "current_file": "engine.py",
    }
    out = await action_rewind_session_turn(_step_input(ctx, fx))
    assert fx.calls == [("rewind", "sess-9", 12)]
    assert out.result["rewound"] is True


@pytest.mark.asyncio
async def test_walk_leaves_a_turn_already_rolled_out_alone():
    from agent.actions.session_structural_actions import action_rewind_session_turn

    fx = SessionEffects([])
    ctx = {
        "structural_session_id": "sess-9",
        "inference_session_turn_id": 12,
        "inference_turn_committed": False,
    }
    out = await action_rewind_session_turn(_step_input(ctx, fx))
    assert fx.calls == []
    assert out.result["rewound"] is False


@pytest.mark.asyncio
async def test_a_truncated_turn_is_recorded_for_the_manifest():
    from agent.actions.session_structural_actions import action_write_session_file

    ctx = {
        "current_file": "engine.py",
        "inference_response": "no code here",
        "inference_truncated": True,
    }
    out = await action_write_session_file(_step_input(ctx, SimpleNamespace()))
    assert out.result["write_success"] is False
    assert out.context_updates["session_truncated_files"] == ["engine.py"]


@pytest.mark.asyncio
async def test_the_manifest_reports_truncation_instead_of_hardcoding_false():
    from agent.actions.session_structural_actions import action_session_next_file

    ctx = {
        "mission": SimpleNamespace(architecture=None),
        "pending_files": [],
        "session_files_written": ["models.py"],
        "session_truncated_files": ["engine.py"],
    }
    out = await action_session_next_file(_step_input(ctx, None))
    manifest = out.context_updates["batch_manifest"]
    assert manifest["truncated"] is True
    assert manifest["truncated_files"] == ["engine.py"]


# ── the client: an older server must not break session turns ──────────


def test_unknown_turn_field_errors_are_recognised():
    from agent.effects.inference import _unknown_turn_fields

    assert _unknown_turn_fields(
        "GraphQL errors: Cannot query field 'sessionTurnId' on type 'CompletionResponse'."
    )
    assert not _unknown_turn_fields("GraphQL errors: Session s1 not found")
    assert not _unknown_turn_fields(None)


@pytest.mark.asyncio
async def test_session_turn_falls_back_to_the_legacy_query_once():
    from agent.effects import inference as inf

    client = inf.InferenceEffect()
    sent: list[str] = []

    async def fake_request(_client, body, **kw):
        sent.append(body["query"])
        if "sessionTurnId" in body["query"]:
            return InferenceResult(
                text="",
                tokens_generated=0,
                error="GraphQL errors: Cannot query field 'sessionTurnId' on type",
            )
        return InferenceResult(text="ok", tokens_generated=1)

    async def fake_get_client():
        return object()

    client._request_with_health_watchdog = fake_request
    client._get_client = fake_get_client
    r = await client.session_turn("s1", "p")
    assert r.text == "ok"
    assert client._session_turn_fields_supported is False
    r = await client.session_turn("s1", "p")
    assert r.text == "ok"
    assert ["sessionTurnId" in q for q in sent] == [True, False, False]
