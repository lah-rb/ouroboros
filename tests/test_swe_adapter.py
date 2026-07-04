"""swe_adapter — the official SWE-bench harness adapter.

Pins the parts that don't need Docker/LLM/dataset: the git-diff artifact
contract (extraction + predictions schema), the forced code_core+repair
mission build, and the gold-oracle predictions builder. Container/dataset
paths are import-guarded so the suite stays green without the heavy deps.
"""

from __future__ import annotations

import json
import tempfile

from swe_adapter.instance import SweInstance
from swe_adapter.patch import extract_model_patch, prediction_row


class _FakeContainer:
    """Records the exec command and returns a canned demux (stdout, stderr)."""

    def __init__(self, stdout=b"", stderr=b"", raise_exc=None):
        self._stdout, self._stderr, self._raise = stdout, stderr, raise_exc
        self.last_cmd = None

    def exec_run(self, cmd=None, demux=False, **kw):
        self.last_cmd = cmd
        if self._raise:
            raise self._raise

        class _R:
            output = (self._stdout, self._stderr)

        return _R()


def _inst(**kw) -> SweInstance:
    base = dict(
        instance_id="marshmallow-code__marshmallow-1359",
        repo="marshmallow-code/marshmallow",
        base_commit="abc123",
        problem_statement="Nested fields with dot-delimited attribute fail to serialize.",
        patch="GOLD DIFF",
        test_patch="TEST DIFF",
    )
    base.update(kw)
    return SweInstance(**base)


# ── artifact contract ─────────────────────────────────────────────────


def test_extract_model_patch_returns_diff():
    diff = "diff --git a/x.py b/x.py\n+fix\n"
    c = _FakeContainer(stdout=diff.encode())
    out = extract_model_patch(c, "/testbed")
    assert out == diff
    # git add -A precedes the diff (untracked files must be captured)
    joined = " ".join(c.last_cmd)
    assert "git add -A" in joined and "git diff --cached" in joined
    assert "/testbed" in joined


def test_extract_model_patch_empty_is_empty_string_not_none():
    out = extract_model_patch(_FakeContainer(stdout=b"", stderr=b"nothing to commit"))
    assert out == ""  # a valid (unsolved) prediction, never None


def test_extract_model_patch_survives_exec_failure():
    out = extract_model_patch(_FakeContainer(raise_exc=RuntimeError("container gone")))
    assert out == ""


def test_prediction_row_schema_exact():
    row = prediction_row("inst-1", "ouroboros", "PATCH")
    assert set(row) == {"instance_id", "model_name_or_path", "model_patch"}
    assert row == {
        "instance_id": "inst-1",
        "model_name_or_path": "ouroboros",
        "model_patch": "PATCH",
    }
    # None patch coerces to "" (schema is str)
    assert prediction_row("i", "m", None)["model_patch"] == ""


def test_predictions_jsonl_one_object_per_line():
    from swe_adapter.evaluate import write_predictions

    rows = [prediction_row("a", "m", "PA"), prediction_row("b", "m", "PB")]
    with tempfile.TemporaryDirectory() as d:
        path = write_predictions(rows, f"{d}/preds.jsonl")
        lines = open(path).read().splitlines()
    assert len(lines) == 2
    assert [json.loads(x)["instance_id"] for x in lines] == ["a", "b"]


# ── image key normalization ───────────────────────────────────────────


def test_image_key_normalizes_instance_id():
    key = _inst().image_key
    assert key.startswith("swebench/sweb.eval.x86_64.")
    assert "__" not in key.split(".")[-2]  # double underscore is escaped


# ── forced code_core + repair mission build (no container / LLM) ──────


def test_build_mission_forces_code_core_repair_brownfield():
    from swe_adapter.runner import REPO_DIR, build_mission

    inst = _inst()
    with tempfile.TemporaryDirectory() as host_tmp:
        mission, entry_flow = build_mission(inst, host_tmp)
    assert mission.config.flow_set == "code_core"
    assert mission.config.task_profile == "repair"
    assert mission.config.working_directory == REPO_DIR == "/testbed"
    assert mission.config.web_research is False
    assert mission.objective == inst.problem_statement
    assert mission.pending_directive == inst.problem_statement  # → replan → repair
    assert entry_flow == "ingest_workspace"


# ── gold-patch oracle predictions ─────────────────────────────────────


def test_gold_predictions_use_gold_patch():
    from swe_adapter.evaluate import GOLD_MODEL, write_gold_predictions

    insts = [_inst(), _inst(instance_id="django__django-11099", patch="GOLD2")]
    with tempfile.TemporaryDirectory() as d:
        path = write_gold_predictions(insts, f"{d}/gold.jsonl")
        rows = [json.loads(x) for x in open(path).read().splitlines()]
    assert [r["model_patch"] for r in rows] == ["GOLD DIFF", "GOLD2"]
    assert all(r["model_name_or_path"] == GOLD_MODEL for r in rows)


# ── pilot subset sanity ───────────────────────────────────────────────


def test_pilot_has_small_and_large_scouts():
    from swe_adapter.instance import (
        PILOT_INSTANCES,
        PILOT_LARGE_SCOUTS,
        PILOT_SMALL,
    )

    assert len(PILOT_SMALL) >= 5
    assert any("django" in i or "sympy" in i or "matplotlib" in i for i in PILOT_LARGE_SCOUTS)
    assert set(PILOT_INSTANCES) == set(PILOT_SMALL) | set(PILOT_LARGE_SCOUTS)
