"""Cross-file multi-symbol patch: partition → per-file write → advance → finalize.

Diagnose emits file-qualified related_symbols ("path.py:Class.method").
prepare_next_rewrite partitions them: same-file entries seed rewrite_queue,
cross-file entries land in cross_file_queue. write_patched_file persists each
file as its queue drains; load_next_file advances the batch (re-read +
re-extract) inside the same edit session; finalize closes the session and
assembles the batch summary — unresolved symbols surface there, never
silently dropped.
"""

from __future__ import annotations

import pytest

from agent.actions.ast_actions import (
    action_finalize_edit_session,
    action_load_next_file,
    action_prepare_next_rewrite,
    action_rewrite_symbol_turn,
    action_write_patched_file,
    _build_symbol_table,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.session_injections import peek

_ENGINE = (
    "class GameEngine:\n"
    "    def handle_take(self, item):\n"
    "        return item\n"
    "\n"
    "    def handle_drop(self, item):\n"
    "        return item\n"
)

_PARSER = (
    "def parse_command(raw):\n"
    "    return raw\n"
    "\n"
    "\n"
    "def tokenize(raw):\n"
    "    return raw.split()\n"
)


def _si(effects=None, context=None, params=None) -> StepInput:
    return StepInput(
        context=dict(context or {}),
        params=dict(params or {}),
        meta=FlowMeta(flow_name="patch", step_id="x"),
        effects=effects,
    )


# ── prepare_next_rewrite: partitioning ────────────────────────────────


@pytest.mark.asyncio
async def test_partition_same_file_cross_file_and_bare():
    table = _build_symbol_table("engine.py", _ENGINE)
    si = _si(
        context={"symbol_table": table},
        params={
            "target_symbol": "GameEngine.handle_take",
            "file_path": "engine.py",
            "related_symbols": [
                "engine.py:GameEngine.handle_drop",  # file-qualified, same file
                "parser.py:parse_command",  # cross-file
                "parser.py:tokenize",  # cross-file, same file as above
            ],
        },
    )
    out = await action_prepare_next_rewrite(si)
    assert out.result["has_next"] is True
    cu = out.context_updates
    assert cu["current_symbol"]["name"] == "GameEngine.handle_take"
    assert [s["name"] for s in cu["rewrite_queue"]] == ["GameEngine.handle_drop"]
    assert cu["cross_file_queue"] == [
        {"file": "parser.py", "symbols": ["parse_command", "tokenize"]}
    ]
    assert cu["unresolved_symbols"] == []
    assert cu["files_changed"] == []


@pytest.mark.asyncio
async def test_partition_unresolved_local_recorded_not_dropped():
    table = _build_symbol_table("engine.py", _ENGINE)
    si = _si(
        context={"symbol_table": table},
        params={
            "target_symbol": "GameEngine.handle_take",
            "file_path": "engine.py",
            "related_symbols": ["GameEngine.no_such_method"],
        },
    )
    out = await action_prepare_next_rewrite(si)
    assert out.result["has_next"] is True
    assert out.context_updates["unresolved_symbols"] == [
        "engine.py:GameEngine.no_such_method"
    ]


# ── load_next_file ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_next_file_seeds_queue_and_injects_notice():
    effects = MockEffects(files={"parser.py": _PARSER})
    si = _si(
        effects=effects,
        context={
            "cross_file_queue": [
                {"file": "parser.py", "symbols": ["parse_command", "tokenize"]}
            ],
            "unresolved_symbols": [],
        },
    )
    out = await action_load_next_file(si)
    assert out.result["has_next"] is True
    cu = out.context_updates
    assert cu["file_path"] == "parser.py"
    assert cu["file_content"] == _PARSER
    assert cu["file_content_updated"] == _PARSER
    assert cu["current_symbol"]["name"] == "parse_command"
    assert [s["name"] for s in cu["rewrite_queue"]] == ["tokenize"]
    assert cu["cross_file_queue"] == []
    # File-switch notice queued for the next rewrite turn's inference.
    notices = peek(cu)
    assert len(notices) == 1 and "Now editing parser.py" in notices[0]


@pytest.mark.asyncio
async def test_load_next_file_unreadable_file_recorded_unresolved():
    effects = MockEffects(files={})  # parser.py missing
    si = _si(
        effects=effects,
        context={
            "cross_file_queue": [{"file": "parser.py", "symbols": ["parse_command"]}],
            "unresolved_symbols": [],
        },
    )
    out = await action_load_next_file(si)
    assert out.result["has_next"] is False
    assert out.context_updates["unresolved_symbols"] == ["parser.py:parse_command"]


@pytest.mark.asyncio
async def test_load_next_file_missing_symbol_recorded_skips_to_next():
    effects = MockEffects(files={"parser.py": _PARSER, "models.py": _PARSER})
    si = _si(
        effects=effects,
        context={
            "cross_file_queue": [
                {"file": "parser.py", "symbols": ["ghost_function"]},
                {"file": "models.py", "symbols": ["tokenize"]},
            ],
            "unresolved_symbols": [],
        },
    )
    out = await action_load_next_file(si)
    assert out.result["has_next"] is True
    cu = out.context_updates
    assert cu["file_path"] == "models.py"
    assert cu["current_symbol"]["name"] == "tokenize"
    assert cu["unresolved_symbols"] == ["parser.py:ghost_function"]


@pytest.mark.asyncio
async def test_load_next_file_empty_queue_is_exhausted():
    effects = MockEffects(files={})
    si = _si(effects=effects, context={"cross_file_queue": []})
    out = await action_load_next_file(si)
    assert out.result["has_next"] is False


# ── write_patched_file ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_patched_file_aggregates_across_batch():
    effects = MockEffects(files={})
    out1 = await action_write_patched_file(
        _si(
            effects=effects,
            context={
                "file_path": "engine.py",
                "file_content_updated": _ENGINE,
                "already_rewritten": {"engine.py:GameEngine.handle_take": "x"},
            },
        )
    )
    assert out1.result["write_success"] is True
    assert out1.context_updates["files_changed"] == ["engine.py"]

    out2 = await action_write_patched_file(
        _si(
            effects=effects,
            context={
                "file_path": "parser.py",
                "file_content_updated": _PARSER,
                "files_changed": out1.context_updates["files_changed"],
                "edit_summary_parts": out1.context_updates["edit_summary_parts"],
                "already_rewritten": {
                    "engine.py:GameEngine.handle_take": "x",
                    "parser.py:parse_command": "y",
                },
            },
        )
    )
    assert out2.context_updates["files_changed"] == ["engine.py", "parser.py"]
    assert effects._files["engine.py"] == _ENGINE
    assert effects._files["parser.py"] == _PARSER
    parts = out2.context_updates["edit_summary_parts"]
    assert any("GameEngine.handle_take" in p for p in parts)
    assert any("parse_command" in p for p in parts)


@pytest.mark.asyncio
async def test_write_patched_file_skips_when_no_rewrites_landed():
    effects = MockEffects(files={})
    out = await action_write_patched_file(
        _si(
            effects=effects,
            context={
                "file_path": "engine.py",
                "file_content_updated": _ENGINE,
                "already_rewritten": {},  # every rewrite was rejected
            },
        )
    )
    # Batch continues (next file's symbols are independent) but the
    # unchanged file is neither written nor reported as changed.
    assert out.result["write_success"] is True
    assert out.context_updates["files_changed"] == []
    assert "engine.py" not in effects._files
    assert any(
        "no rewrites landed" in p for p in out.context_updates["edit_summary_parts"]
    )


# ── finalize_edit_session ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_closes_session_without_writing():
    effects = MockEffects(files={})
    session_id = await effects.start_inference_session()
    out = await action_finalize_edit_session(
        _si(
            effects=effects,
            context={
                "edit_session_id": session_id,
                "files_changed": ["engine.py", "parser.py"],
                "edit_summary_parts": [
                    "engine.py: rewrote GameEngine.handle_take",
                    "parser.py: rewrote parse_command",
                ],
                "unresolved_symbols": ["models.py:Ghost.method"],
            },
        )
    )
    assert out.result["status"] == "success"
    assert effects.call_count("write_file") == 0
    assert effects.call_count("end_inference_session") == 1
    summary = out.context_updates["edit_summary"]
    assert "engine.py: rewrote GameEngine.handle_take" in summary
    assert "parser.py: rewrote parse_command" in summary
    assert "unresolved: models.py:Ghost.method" in summary


@pytest.mark.asyncio
async def test_finalize_failed_when_nothing_changed():
    effects = MockEffects(files={})
    session_id = await effects.start_inference_session()
    out = await action_finalize_edit_session(
        _si(effects=effects, context={"edit_session_id": session_id})
    )
    assert out.result["status"] == "failed"
    assert out.context_updates["edit_summary"] == "No changes applied"


# ── rewrite_symbol_turn: file-qualified already_rewritten keys ────────


# ── validation runs per changed file ──────────────────────────────────


@pytest.mark.asyncio
async def test_validation_runs_tiers_for_every_changed_file():
    from agent.actions.pipeline_actions import action_run_validation_checks_from_env
    from agent.effects.protocol import CommandResult

    ok = CommandResult(return_code=0, stdout="", stderr="", command="python")
    effects = MockEffects(commands={"python": ok})
    si = _si(
        effects=effects,
        context={
            "validation_commands": {
                "syntax": ["python", "-c", "compile('{file}')"],
            }
        },
        params={"target": "engine.py", "files": ["engine.py", "parser.py"]},
    )
    out = await action_run_validation_checks_from_env(si)
    assert out.result["all_passing"] is True
    names = [c["name"] for c in out.context_updates["validation_results"]]
    assert names == ["syntax: engine.py", "syntax: parser.py"]


@pytest.mark.asyncio
async def test_validation_falls_back_to_target_when_no_files():
    from agent.actions.pipeline_actions import action_run_validation_checks_from_env
    from agent.effects.protocol import CommandResult

    ok = CommandResult(return_code=0, stdout="", stderr="", command="python")
    effects = MockEffects(commands={"python": ok})
    si = _si(
        effects=effects,
        context={"validation_commands": {"syntax": ["python", "-c", "x"]}},
        params={"target": "engine.py", "files": []},
    )
    out = await action_run_validation_checks_from_env(si)
    names = [c["name"] for c in out.context_updates["validation_results"]]
    assert names == ["syntax: engine.py"]


@pytest.mark.asyncio
async def test_rewrite_turn_records_file_qualified_key():
    effects = MockEffects(
        inference_responses=[
            "```python\ndef parse_command(raw):\n    return raw.lower()\n```"
        ]
    )
    session_id = await effects.start_inference_session()
    table = _build_symbol_table("parser.py", _PARSER)
    current = next(s for s in table if s["name"] == "parse_command")
    si = _si(
        effects=effects,
        context={
            "edit_session_id": session_id,
            "current_symbol": current,
            "file_content": _PARSER,
        },
        params={"file_path": "parser.py"},
    )
    out = await action_rewrite_symbol_turn(si)
    assert out.result["rewrite_success"] is True
    assert list(out.context_updates["already_rewritten"]) == ["parser.py:parse_command"]
