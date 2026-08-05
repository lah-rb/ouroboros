"""A truncated batch response must not write its severed last file.

`extract_fenced_blocks` delegates to markdown-it, and CommonMark closes an
unterminated fenced block implicitly at end of input. So when a generation is
cut mid-file, the half-written body comes back as a perfectly ordinary block and
gets written over a previously-good file — the "stale earlier pass" failure.

The drop is deliberately conditional on the fence actually being unterminated: a
truncation that lands just after a closing fence has a complete final file, and
discarding it would cost a regeneration for nothing.

Dropped files are not lost. `missing = [f for f in declared if f not in written]`
already reconciles against the architecture manifest and routes the remainder to
the serial create path.
"""

from __future__ import annotations

import asyncio

from agent.actions.batch_structural_actions import action_slice_batch_files
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    MissionConfig,
    MissionState,
    ModuleSpec,
)

_COMPLETE = """```python
# === FILE: models.py ===
class Player:
    hp = 30
```

```python
# === FILE: engine.py ===
class Engine:
    def run(self):
        return 1
```
"""

# Same response cut mid-body: the final fence never closes.
_SEVERED = """```python
# === FILE: models.py ===
class Player:
    hp = 30
```

```python
# === FILE: engine.py ===
class Engine:
    def run(self):
        retu"""


def _mission(tmp_path):
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), structural_mode="batch"),
        architecture=ArchitectureState(
            run_command="python engine.py",
            creation_order=["models.py", "engine.py"],
            modules=[
                ModuleSpec(file="models.py", responsibility="data"),
                ModuleSpec(file="engine.py", responsibility="engine"),
            ],
        ),
        goals=[],
        notes=[],
    )


def _run(tmp_path, raw, truncated):
    mission = _mission(tmp_path)
    fx = MockEffects(mission=mission)
    out = asyncio.run(
        action_slice_batch_files(
            StepInput(
                context={
                    "mission": mission,
                    "inference_response": raw,
                    "inference_truncated": truncated,
                },
                params={},
                meta=FlowMeta(flow_name="build_structure", step_id="slice_batch_files"),
                effects=fx,
            )
        )
    )
    return out.context_updates["batch_manifest"], fx


class TestSeveredTrailingBlock:
    def test_the_severed_file_is_not_written(self, tmp_path):
        manifest, _ = _run(tmp_path, _SEVERED, truncated=True)
        assert manifest["written"] == ["models.py"]
        assert "engine.py" not in manifest["written"]

    def test_it_stays_missing_so_the_create_path_regenerates_it(self, tmp_path):
        manifest, _ = _run(tmp_path, _SEVERED, truncated=True)
        assert (
            "engine.py" in manifest["missing"]
        ), "dropping must hand the file to the serial create path, not lose it"

    def test_the_truncation_is_recorded(self, tmp_path):
        manifest, _ = _run(tmp_path, _SEVERED, truncated=True)
        assert manifest["truncated"] is True

    def test_the_good_earlier_files_still_land(self, tmp_path):
        """The whole point of force-windowing: keep the completed work."""
        manifest, fx = _run(tmp_path, _SEVERED, truncated=True)
        assert "models.py" in manifest["written"]


class TestItOnlyDropsWhenTheFenceIsActuallyOpen:
    def test_a_truncation_on_a_clean_fence_keeps_every_file(self, tmp_path):
        """Cut after a closing fence — the last file is complete, so dropping
        it would cost a regeneration for nothing."""
        manifest, _ = _run(tmp_path, _COMPLETE, truncated=True)
        assert manifest["written"] == ["models.py", "engine.py"]
        assert manifest["missing"] == []

    def test_an_untruncated_response_is_never_touched(self, tmp_path):
        manifest, _ = _run(tmp_path, _COMPLETE, truncated=False)
        assert manifest["written"] == ["models.py", "engine.py"]

    def test_a_severed_response_not_flagged_truncated_is_left_alone(self, tmp_path):
        """Scoping guard: this path keys off the engine's truncation signal, not
        its own fence heuristic. An unflagged malformed response is a different
        bug and the syntax gate is what catches it."""
        manifest, _ = _run(tmp_path, _SEVERED, truncated=False)
        assert "engine.py" in manifest["written"]


class TestDegenerateCases:
    def test_a_single_severed_block_writes_nothing(self, tmp_path):
        raw = "```python\n# === FILE: models.py ===\nclass Player:\n    hp = 3"
        manifest, _ = _run(tmp_path, raw, truncated=True)
        assert manifest["written"] == []
        assert set(manifest["missing"]) == {"models.py", "engine.py"}

    def test_an_empty_response_is_unchanged(self, tmp_path):
        manifest, _ = _run(tmp_path, "", truncated=True)
        assert manifest["written"] == []
