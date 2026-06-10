"""Tests for the ``step_context`` plumbing and session-inference trace events.

Background
----------
Before this instrumentation, ``session_inference`` calls fired directly
from inside actions — every turn in ``diagnosis_session_actions`` and
the AST-edit actions — were invisible to the trace. The runtime's
``_execute_inference_action`` emitted ``InferenceCall`` events only for
steps declared as ``action: inference``; 19 in-action session call sites
were a black box. See the a12 trace post-mortem for impact.

The §4.7 contextvars approach binds mission_id/cycle/flow/step around
each action dispatch; effects read it opportunistically when emitting
trace events. These tests assert:

1. The context var is bound inside a ``step_context`` block and cleared
   afterward, preserving any prior value.
2. Effects' ``session_inference`` emits an ``InferenceCall`` trace event
   with the bound context when called inside a step.
3. Outside a step, ``session_inference`` does not emit a trace event
   (callers from tests/tooling don't pollute the trace).
"""

from __future__ import annotations

import pytest

from agent.effects.mock import MockEffects
from agent.trace import (
    InferenceCall,
    get_step_context,
    step_context,
)

# ── context var plumbing ─────────────────────────────────────────────


def test_step_context_is_none_outside_block():
    """No step context bound at module-import-time or in test setup."""
    assert get_step_context() is None


def test_step_context_binds_fields_inside_block():
    """Inside the block, get_step_context returns the bound dict."""
    with step_context(
        mission_id="m1", cycle=7, flow="diagnose_issue", step="start_session"
    ):
        ctx = get_step_context()
        assert ctx is not None
        assert ctx["mission_id"] == "m1"
        assert ctx["cycle"] == 7
        assert ctx["flow"] == "diagnose_issue"
        assert ctx["step"] == "start_session"


def test_step_context_restores_prior_value_after_exit():
    """Token-based reset preserves nesting semantics."""
    with step_context(mission_id="outer", cycle=1, flow="f_outer", step="s_outer"):
        assert get_step_context()["flow"] == "f_outer"
        with step_context(mission_id="inner", cycle=2, flow="f_inner", step="s_inner"):
            assert get_step_context()["flow"] == "f_inner"
        # After inner exits, outer is restored.
        assert get_step_context()["flow"] == "f_outer"
    # After outer exits, baseline is None.
    assert get_step_context() is None


# ── Effects-side emission ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_session_inference_emits_trace_event_inside_step_context():
    """When called inside step_context, MockEffects emits InferenceCall."""
    effects = MockEffects(inference_responses=["acknowledged"])
    session_id = await effects.start_inference_session()

    with step_context(
        mission_id="test-mission",
        cycle=3,
        flow="diagnose_issue",
        step="start_session",
    ):
        await effects.session_inference(session_id, "seed prompt")

    inference_events = [e for e in effects.trace_events if isinstance(e, InferenceCall)]
    assert len(inference_events) == 1
    event = inference_events[0]
    assert event.flow == "diagnose_issue"
    assert event.step == "start_session"
    assert event.cycle == 3
    assert event.mission_id == "test-mission"
    assert event.purpose == "session_inference"
    assert event.tokens_in > 0  # "seed prompt" splits into two tokens


@pytest.mark.asyncio
async def test_mock_session_inference_outside_step_context_no_trace_event():
    """Calls from outside a bound step context do not emit trace events."""
    effects = MockEffects(inference_responses=["response"])
    session_id = await effects.start_inference_session()

    # No step_context wrapper.
    await effects.session_inference(session_id, "prompt")

    inference_events = [e for e in effects.trace_events if isinstance(e, InferenceCall)]
    assert inference_events == []


@pytest.mark.asyncio
async def test_mock_session_inference_emits_one_event_per_turn():
    """Each call within the block emits exactly one event."""
    effects = MockEffects(inference_responses=["r1", "r2", "r3"])
    session_id = await effects.start_inference_session()

    with step_context(
        mission_id="m", cycle=1, flow="diagnose_issue", step="trace_symbols"
    ):
        await effects.session_inference(session_id, "turn 1")
        await effects.session_inference(session_id, "turn 2")
        await effects.session_inference(session_id, "turn 3")

    inference_events = [e for e in effects.trace_events if isinstance(e, InferenceCall)]
    assert len(inference_events) == 3
    # All three carry the same step attribution because they fire in
    # the same step_context block.
    assert all(e.step == "trace_symbols" for e in inference_events)
    assert all(e.purpose == "session_inference" for e in inference_events)


# ── Session lifecycle events ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_start_emits_event_in_step_context():
    """start_inference_session emits SessionStart with config snapshot."""
    from agent.trace import SessionStart

    effects = MockEffects()
    with step_context(
        mission_id="m", cycle=1, flow="diagnose_issue", step="start_session"
    ):
        session_id = await effects.start_inference_session({"ttl_seconds": 600})

    events = [e for e in effects.trace_events if isinstance(e, SessionStart)]
    assert len(events) == 1
    assert events[0].session_id == session_id
    assert events[0].config == {"ttl_seconds": 600}
    assert events[0].flow == "diagnose_issue"
    assert events[0].step == "start_session"


@pytest.mark.asyncio
async def test_session_end_emits_event_in_step_context():
    """end_inference_session emits SessionEnd and marks success."""
    from agent.trace import SessionEnd, SessionStart

    effects = MockEffects()
    with step_context(
        mission_id="m", cycle=1, flow="diagnose_issue", step="start_session"
    ):
        session_id = await effects.start_inference_session()
    with step_context(
        mission_id="m", cycle=2, flow="diagnose_issue", step="end_session"
    ):
        ok = await effects.end_inference_session(session_id)

    assert ok is True
    start_events = [e for e in effects.trace_events if isinstance(e, SessionStart)]
    end_events = [e for e in effects.trace_events if isinstance(e, SessionEnd)]
    assert len(start_events) == 1
    assert len(end_events) == 1
    assert end_events[0].session_id == session_id
    assert end_events[0].success is True
    # Different step between start and end — both attribute correctly
    assert start_events[0].step == "start_session"
    assert end_events[0].step == "end_session"


@pytest.mark.asyncio
async def test_session_lifecycle_outside_step_context_no_events():
    """Outside step context, session lifecycle emits no trace events."""
    from agent.trace import SessionEnd, SessionStart

    effects = MockEffects()
    session_id = await effects.start_inference_session()
    await effects.end_inference_session(session_id)

    assert [e for e in effects.trace_events if isinstance(e, SessionStart)] == []
    assert [e for e in effects.trace_events if isinstance(e, SessionEnd)] == []


# ── Command events ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_command_run_emits_event_with_output_previews():
    """run_command emits CommandRun with stdout/stderr previews."""
    from agent.effects.protocol import CommandResult
    from agent.trace import CommandRun

    effects = MockEffects(
        commands={
            "echo hi": CommandResult(
                return_code=0,
                stdout="hi\n",
                stderr="",
                command="echo hi",
            )
        }
    )
    with step_context(
        mission_id="m", cycle=1, flow="interact", step="execute_commands"
    ):
        await effects.run_command(["echo", "hi"])

    events = [e for e in effects.trace_events if isinstance(e, CommandRun)]
    assert len(events) == 1
    assert events[0].command == "echo hi"
    assert events[0].return_code == 0
    assert events[0].timed_out is False
    assert events[0].stdout_preview == "hi\n"
    assert events[0].flow == "interact"
    assert events[0].step == "execute_commands"


@pytest.mark.asyncio
async def test_command_run_truncates_large_stdout():
    """Long stdout is truncated in the trace event."""
    from agent.effects.protocol import CommandResult
    from agent.trace import CommandRun, _OUTPUT_PREVIEW_CHARS

    huge = "x" * (_OUTPUT_PREVIEW_CHARS + 500)
    effects = MockEffects(
        commands={
            "spew": CommandResult(return_code=0, stdout=huge, stderr="", command="spew")
        }
    )
    with step_context(mission_id="m", cycle=1, flow="interact", step="s"):
        await effects.run_command(["spew"])

    events = [e for e in effects.trace_events if isinstance(e, CommandRun)]
    assert len(events) == 1
    # Preview is shorter than input, contains the truncation marker.
    assert len(events[0].stdout_preview) < len(huge)
    assert "truncated" in events[0].stdout_preview


@pytest.mark.asyncio
async def test_command_run_outside_step_context_no_event():
    from agent.trace import CommandRun

    effects = MockEffects()
    await effects.run_command(["ls"])

    assert [e for e in effects.trace_events if isinstance(e, CommandRun)] == []


# ── MCP tool call events ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_call_tool_emits_event_with_server_name_resolved():
    """McpToolCall uses server_name from mcp_connections, not raw conn_id."""
    from agent.trace import McpToolCall

    effects = MockEffects()
    with step_context(mission_id="m", cycle=1, flow="research", step="search"):
        conn_id = await effects.mcp_connect("exa")
        await effects.mcp_call_tool(
            conn_id, "web_search_exa", {"query": "test", "numResults": 5}
        )

    events = [e for e in effects.trace_events if isinstance(e, McpToolCall)]
    assert len(events) == 1
    assert events[0].server == "exa"  # resolved, not conn_id
    assert events[0].tool == "web_search_exa"
    assert set(events[0].arg_keys) == {"query", "numResults"}
    assert events[0].error == ""


@pytest.mark.asyncio
async def test_mcp_call_tool_outside_step_context_no_event():
    from agent.trace import McpToolCall

    effects = MockEffects()
    conn_id = await effects.mcp_connect("exa")
    await effects.mcp_call_tool(conn_id, "web_search_exa", {"query": "test"})

    assert [e for e in effects.trace_events if isinstance(e, McpToolCall)] == []


# ── Note events ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_note_emits_event_in_step_context():
    """push_note emits NotePushed with category, tags, and content preview."""
    from agent.trace import NotePushed

    effects = MockEffects()
    with step_context(
        mission_id="m", cycle=1, flow="diagnose_issue", step="compile_diagnosis"
    ):
        await effects.push_note(
            "investigation found root cause in loader.py",
            category="failure_analysis",
            tags=["loader", "staticmethod"],
            source_flow="diagnose_issue",
        )

    events = [e for e in effects.trace_events if isinstance(e, NotePushed)]
    assert len(events) == 1
    assert events[0].category == "failure_analysis"
    assert events[0].tags == ["loader", "staticmethod"]
    assert events[0].source_flow == "diagnose_issue"
    assert "loader.py" in events[0].content_preview
    assert events[0].success is True


@pytest.mark.asyncio
async def test_push_note_truncates_long_content():
    """Very long note content gets truncated to _NOTE_PREVIEW_CHARS."""
    from agent.trace import NotePushed, _NOTE_PREVIEW_CHARS

    big = "y" * (_NOTE_PREVIEW_CHARS + 200)
    effects = MockEffects()
    with step_context(mission_id="m", cycle=1, flow="f", step="s"):
        await effects.push_note(big, category="general")

    events = [e for e in effects.trace_events if isinstance(e, NotePushed)]
    assert len(events) == 1
    assert len(events[0].content_preview) < len(big)
    assert "truncated" in events[0].content_preview


@pytest.mark.asyncio
async def test_push_note_outside_step_context_no_event():
    from agent.trace import NotePushed

    effects = MockEffects()
    await effects.push_note("a note", category="general")

    assert [e for e in effects.trace_events if isinstance(e, NotePushed)] == []
