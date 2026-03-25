"""Tests for the runtime tracing system (Phase 2).

Covers TraceEvent serialization, count_tokens, instrumentation via
MockEffects, and the trace CLI summary renderer.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import pytest

from agent.trace import (
    TraceEvent,
    CycleStart,
    CycleEnd,
    StepStart,
    StepEnd,
    InferenceCall,
    FlowInvoke,
    FlowReturn,
    count_tokens,
)
from agent.effects.mock import MockEffects
from agent.effects.local import LocalEffects

# ── count_tokens ──────────────────────────────────────────────────────


class TestCountTokens:
    def test_simple_string(self):
        assert count_tokens("hello world") == 2

    def test_empty_string(self):
        assert count_tokens("") == 0

    def test_whitespace_only(self):
        assert count_tokens("   ") == 0

    def test_multiline(self):
        text = "line one\nline two\nline three"
        assert count_tokens(text) == 6

    def test_code_snippet(self):
        text = "def foo(x): return x + 1"
        assert count_tokens(text) == 6


# ── TraceEvent serialization ──────────────────────────────────────────


class TestTraceEventSerialization:
    def test_base_event_to_dict(self):
        event = TraceEvent(
            event_type="test",
            mission_id="m1",
            cycle=1,
            flow="test_flow",
        )
        d = event.to_dict()
        assert d["event_type"] == "test"
        assert d["mission_id"] == "m1"
        assert d["cycle"] == 1
        assert d["flow"] == "test_flow"
        assert "timestamp" in d
        assert "wall_time" in d

    def test_cycle_start_to_dict(self):
        event = CycleStart(
            mission_id="m1",
            cycle=2,
            flow="mission_control",
            entry_inputs=["mission_id", "task_id"],
        )
        d = event.to_dict()
        assert d["event_type"] == "cycle_start"
        assert d["entry_inputs"] == ["mission_id", "task_id"]

    def test_cycle_end_to_dict(self):
        event = CycleEnd(
            mission_id="m1",
            cycle=2,
            flow="mission_control",
            outcome="tail_call",
            target_flow="modify_file",
            cycle_duration_ms=1234.5,
        )
        d = event.to_dict()
        assert d["event_type"] == "cycle_end"
        assert d["outcome"] == "tail_call"
        assert d["target_flow"] == "modify_file"
        assert d["cycle_duration_ms"] == 1234.5

    def test_step_start_to_dict(self):
        event = StepStart(
            step="load_state",
            action_type="action",
            action="load_mission_state",
            context_consumed=["mission_id"],
            context_required=["mission_id"],
        )
        d = event.to_dict()
        assert d["event_type"] == "step_start"
        assert d["step"] == "load_state"
        assert d["action"] == "load_mission_state"

    def test_step_end_to_dict(self):
        event = StepEnd(
            step="load_state",
            published=["mission", "events"],
            resolver_type="rule",
            resolver_decision="assess",
            options_available=["assess", "abort"],
            step_duration_ms=50.0,
        )
        d = event.to_dict()
        assert d["event_type"] == "step_end"
        assert d["published"] == ["mission", "events"]
        assert d["resolver_decision"] == "assess"

    def test_inference_call_to_dict(self):
        event = InferenceCall(
            step="plan_change",
            tokens_in=1200,
            tokens_out=400,
            wall_ms=3000.0,
            temperature=0.7,
            max_tokens=4096,
            purpose="step_inference",
        )
        d = event.to_dict()
        assert d["event_type"] == "inference_call"
        assert d["tokens_in"] == 1200
        assert d["tokens_out"] == 400
        assert d["purpose"] == "step_inference"

    def test_flow_invoke_to_dict(self):
        event = FlowInvoke(
            step="prepare",
            child_flow="prepare_context",
            child_inputs=["target_file", "mission_id"],
        )
        d = event.to_dict()
        assert d["event_type"] == "flow_invoke"
        assert d["child_flow"] == "prepare_context"

    def test_flow_return_to_dict(self):
        event = FlowReturn(
            child_flow="prepare_context",
            return_status="success",
            child_duration_ms=500.0,
        )
        d = event.to_dict()
        assert d["event_type"] == "flow_return"
        assert d["return_status"] == "success"

    def test_json_roundtrip(self):
        event = InferenceCall(
            mission_id="abc",
            cycle=3,
            flow="modify_file",
            step="plan",
            tokens_in=100,
            tokens_out=50,
            wall_ms=200.0,
            temperature=0.7,
            max_tokens=2048,
            purpose="step_inference",
        )
        json_str = json.dumps(event.to_dict())
        parsed = json.loads(json_str)
        assert parsed["event_type"] == "inference_call"
        assert parsed["tokens_in"] == 100


# ── MockEffects tracing ──────────────────────────────────────────────


class TestMockEffectsTracing:
    @pytest.fixture
    def effects(self):
        return MockEffects()

    def test_emit_trace_appends(self, effects):
        event = StepStart(step="test_step", action="noop")
        asyncio.get_event_loop().run_until_complete(effects.emit_trace(event))
        assert len(effects.trace_events) == 1
        assert effects.trace_events[0].step == "test_step"

    def test_flush_traces_is_noop(self, effects):
        event = StepStart(step="test_step", action="noop")
        asyncio.get_event_loop().run_until_complete(effects.emit_trace(event))
        asyncio.get_event_loop().run_until_complete(effects.flush_traces())
        # Events still accessible after flush
        assert len(effects.trace_events) == 1

    def test_multiple_events_ordered(self, effects):
        events = [
            CycleStart(cycle=1, flow="test"),
            StepStart(step="s1", action="noop"),
            StepEnd(step="s1", resolver_type="rule", resolver_decision="s2"),
            CycleEnd(cycle=1, outcome="termination"),
        ]
        for e in events:
            asyncio.get_event_loop().run_until_complete(effects.emit_trace(e))
        assert len(effects.trace_events) == 4
        types = [e.event_type for e in effects.trace_events]
        assert types == ["cycle_start", "step_start", "step_end", "cycle_end"]


# ── LocalEffects tracing (flush to disk) ─────────────────────────────


class TestLocalEffectsTracing:
    def test_flush_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            effects = LocalEffects(working_directory=tmpdir)
            events = [
                CycleStart(mission_id="test123", cycle=1, flow="test"),
                StepStart(mission_id="test123", step="s1", action="noop"),
                CycleEnd(mission_id="test123", cycle=1, outcome="termination"),
            ]
            for e in events:
                asyncio.get_event_loop().run_until_complete(effects.emit_trace(e))

            asyncio.get_event_loop().run_until_complete(effects.flush_traces())

            # Find the trace file
            traces_dir = os.path.join(tmpdir, ".agent", "traces")
            assert os.path.isdir(traces_dir)
            files = os.listdir(traces_dir)
            assert len(files) == 1
            assert files[0].startswith("test123_")
            assert files[0].endswith(".jsonl")

            # Verify contents
            trace_path = os.path.join(traces_dir, files[0])
            with open(trace_path) as f:
                lines = f.readlines()
            assert len(lines) == 3

            parsed = [json.loads(line) for line in lines]
            assert parsed[0]["event_type"] == "cycle_start"
            assert parsed[1]["event_type"] == "step_start"
            assert parsed[2]["event_type"] == "cycle_end"

    def test_flush_empty_buffer_is_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            effects = LocalEffects(working_directory=tmpdir)
            asyncio.get_event_loop().run_until_complete(effects.flush_traces())
            traces_dir = os.path.join(tmpdir, ".agent", "traces")
            assert not os.path.exists(traces_dir)

    def test_multiple_flushes_append(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            effects = LocalEffects(working_directory=tmpdir)

            # First flush
            asyncio.get_event_loop().run_until_complete(
                effects.emit_trace(CycleStart(mission_id="m1", cycle=1, flow="f1"))
            )
            asyncio.get_event_loop().run_until_complete(effects.flush_traces())

            # Second flush
            asyncio.get_event_loop().run_until_complete(
                effects.emit_trace(
                    CycleEnd(mission_id="m1", cycle=1, outcome="termination")
                )
            )
            asyncio.get_event_loop().run_until_complete(effects.flush_traces())

            # Same file, appended
            traces_dir = os.path.join(tmpdir, ".agent", "traces")
            files = os.listdir(traces_dir)
            assert len(files) == 1

            trace_path = os.path.join(traces_dir, files[0])
            with open(trace_path) as f:
                lines = f.readlines()
            assert len(lines) == 2


# ── Runtime instrumentation ──────────────────────────────────────────


class TestRuntimeInstrumentation:
    """Verify trace events are emitted during flow execution."""

    def test_simple_flow_emits_step_events(self):
        """Execute test_simple flow and verify StepStart/StepEnd are emitted."""
        from agent.loader import load_all_flows
        from agent.actions.registry import build_action_registry
        from agent.runtime import execute_flow

        registry = load_all_flows("flows")
        actions = build_action_registry()
        effects = MockEffects(inference_responses=["Mock plan response", "Mock result"])

        flow_def = registry["test_simple"]
        result = asyncio.get_event_loop().run_until_complete(
            execute_flow(
                flow_def=flow_def,
                inputs={"mission_id": "test", "target_file_path": "test.py"},
                action_registry=actions,
                effects=effects,
                flow_registry=registry,
            )
        )

        # Should have trace events
        assert len(effects.trace_events) > 0

        # Check event types present
        event_types = [e.event_type for e in effects.trace_events]
        assert "step_start" in event_types
        assert "step_end" in event_types

    def test_inference_flow_emits_inference_call(self):
        """Execute test_inference flow and verify InferenceCall is emitted."""
        from agent.loader import load_all_flows
        from agent.actions.registry import build_action_registry
        from agent.runtime import execute_flow

        registry = load_all_flows("flows")
        actions = build_action_registry()
        effects = MockEffects(
            files={"test.py": "def hello():\n    return 'world'\n"},
            inference_responses=["The analysis shows all is well.", "Deep analysis."],
        )

        flow_def = registry["test_inference"]
        result = asyncio.get_event_loop().run_until_complete(
            execute_flow(
                flow_def=flow_def,
                inputs={
                    "mission_id": "test",
                    "target_file_path": "test.py",
                    "topic": "testing",
                },
                action_registry=actions,
                effects=effects,
                flow_registry=registry,
            )
        )

        event_types = [e.event_type for e in effects.trace_events]
        assert "inference_call" in event_types

        # Verify the inference call has token counts
        infer_events = [
            e for e in effects.trace_events if e.event_type == "inference_call"
        ]
        assert len(infer_events) >= 1
        assert infer_events[0].tokens_in > 0
        assert infer_events[0].tokens_out > 0
        assert infer_events[0].purpose == "step_inference"
