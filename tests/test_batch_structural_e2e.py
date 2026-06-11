"""Parallel structural mode end-to-end (mock): batch → gates → bookkeeping.

Drives mission_control through the runtime with a virgin structural
phase in parallel mode: the sweep dispatches build_structure, the canned
generation contains three declared files (one with a syntax error
flagged by the canned py_compile result) plus one hallucinated extra and
one omission. Asserts the full deterministic chain: slicing discipline,
per-file gates, per-goal reports, completions, the batch-economics note,
and the diagnose-first dispatch shape the next sweep pass produces.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.loop import run_agent
from agent.persistence.models import (
    ArchitectureState,
    DataShapeContract,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
)

_OK = CommandResult(return_code=0, stdout="", stderr="", command="x")
_SYNTAX_FAIL = CommandResult(
    return_code=1, stdout="", stderr="SyntaxError: invalid syntax", command="x"
)

_BATCH_RESPONSE = """\
Here is the complete project:

```python
# === FILE: models.py ===
class Deck:
    def __init__(self, name):
        self.name = name
```

```python
# === FILE: engine.py ===
def run(:
    pass
```

```yaml
# === FILE: decks.yaml ===
decks:
  - name: starter
    cards: []
```

```markdown
# === FILE: README.md ===
# Hallucinated extra file
```
"""
# main.py is deliberately omitted — the sweep's serial fallback owns it.


def _mission() -> MissionState:
    return MissionState(
        objective="Build a flashcard study CLI.",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),  # parallel default
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=["models.py", "engine.py", "main.py", "decks.yaml"],
            modules=[
                ModuleSpec(file="models.py", responsibility="data classes"),
                ModuleSpec(file="engine.py", responsibility="study loop"),
                ModuleSpec(file="main.py", responsibility="entry point"),
            ],
            data_shapes=[
                DataShapeContract(
                    file="decks.yaml",
                    consumed_by="engine.py",
                    structure="decks: list of {name, cards}",
                    example='{"decks": [{"name": "starter", "cards": []}]}',
                )
            ],
        ),
        goals=[
            GoalRecord(
                description="models", type="structural", associated_files=["models.py"]
            ),
            GoalRecord(
                description="engine", type="structural", associated_files=["engine.py"]
            ),
            GoalRecord(
                description="entry", type="structural", associated_files=["main.py"]
            ),
            GoalRecord(
                description="decks", type="structural", associated_files=["decks.yaml"]
            ),
        ],
    )


def test_parallel_batch_end_to_end():
    mission = _mission()
    fx = MockEffects(
        mission=mission,
        files={
            ".agent/env.json": json.dumps(
                {"py": {"syntax": ["python", "-m", "py_compile", "{file}"]}}
            )
        },
        commands={
            "python -m py_compile models.py": _OK,
            "python -m py_compile engine.py": _SYNTAX_FAIL,
        },
        inference_responses=[_BATCH_RESPONSE],
    )

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # max_cycles=1: mission_control → build_structure → parked (raises) on
    # the next dispatch. The batch work is complete and saved by then.
    with pytest.raises(RuntimeError, match="parked"):
        asyncio.run(
            run_agent(
                mission_id=mission.id,
                effects=fx,
                flows_dir=os.path.join(root, "flows"),
                prompts_dir=os.path.join(root, "prompts"),
                entry_flow="mission_control",
                max_cycles=1,
            )
        )

    saved = fx._state["mission"]
    by_file = {g.associated_files[0]: g for g in saved.goals}

    # Slicing discipline: declared files written, extra skipped, omission missing.
    assert fx._files["models.py"].startswith("class Deck")
    assert "engine.py" in fx._files
    assert fx._files["decks.yaml"].startswith("decks:")
    assert "README.md" not in fx._files
    assert "main.py" not in fx._files

    # Gates → goal bookkeeping: pass completes, syntax failure stays open.
    assert by_file["models.py"].status == "complete"
    assert by_file["decks.yaml"].status == "complete"
    assert by_file["engine.py"].status == "incomplete"
    assert by_file["main.py"].status == "incomplete"
    assert by_file["main.py"].reports == []  # missing file: serial fallback owns it

    engine_report = by_file["engine.py"].reports[-1]
    assert engine_report.flow == "build_structure"
    assert engine_report.status == "failed"
    assert engine_report.checks_failed == ["syntax: engine.py"]

    # Batch-economics note (also the no-rebatch attempted-flag).
    note = next(n for n in saved.notes if "batch_structural" in n.tags)
    assert "3 files written" in note.content
    assert "2 goals completed" in note.content

    # Next sweep pass: engine.py (exists in the workspace from the batch)
    # routes diagnose-first. The file-existence check is real-filesystem,
    # so assert the dispatch shape directly at the action level.
    from agent.actions.mission_actions import action_structural_sweep_next
    from agent.models import FlowMeta, StepInput

    saved.config.working_directory = "/tmp"  # any dir; per-file check below
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        saved.config.working_directory = td
        for name, content in fx._files.items():
            if name.endswith((".py", ".yaml")):
                with open(os.path.join(td, name), "w") as f:
                    f.write(content)
        out = asyncio.run(
            action_structural_sweep_next(
                StepInput(
                    context={"mission": saved},
                    params={},
                    meta=FlowMeta(
                        flow_name="mission_control", step_id="structural_sweep_next"
                    ),
                    effects=fx,
                )
            )
        )
    assert out.result.get("needs_fix") is True
    cfg = out.context_updates["dispatch_config"]
    assert cfg["flow"] == "diagnose_issue"
    assert cfg["target_file_path"] == "engine.py"
