"""The batch fallback is a ladder, not a cliff.

A short generation used to fall straight through to serial creation, and
serial is where cross-file seam bugs are born — every file authored alone,
blind to its siblings. The 13-arm GUARDIAN batch (2026-08-05) made the cost
measurable: the only two arms whose batch turn collapsed produced the two
worst artifacts in the field, and hy3 ran the natural experiment for us by
generating 6/6 files as a contemplator (judged 47/47, played to victory)
and 1/6 as a grinder from the SAME config, prompt and temperature, neither
truncated.

    rung 1  substantially complete -> keep it, serial the remainder
    rung 2  cheap and short        -> resample the batch (<= 2 retries)
    rung 3  neither                -> serial, and now it MEANS something

The load-bearing property is that rung 2 discards its attempt BEFORE
writing. Two independent generations spliced into one tree is the very
incoherence the batch path exists to prevent, so a resample that left its
files behind would be reintroducing the bug it is meant to fix.

SCOPE: everything here calls the verdict and the action DIRECTLY, passing
`attempt` in by hand. That is a unit test of the decision, and it is not
enough on its own — it says nothing about whether the runtime ever supplies
a real attempt number. It did not, for the ladder's whole first day in
production. The wiring is covered by
`tests/test_step_attempt_reaches_actions.py`, which drives `execute_flow`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.actions.batch_structural_actions import (
    BATCH_MAX_ATTEMPTS,
    SERIAL_CREATE_TOKENS,
    _batch_retry_verdict,
    action_slice_batch_files,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    MissionConfig,
    MissionState,
    ModuleSpec,
)

ROOT = Path(__file__).resolve().parents[1]


def _mission(tmp_path, files):
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), structural_mode="batch"),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=list(files),
            modules=[ModuleSpec(file=f, responsibility="x") for f in files],
        ),
        goals=[],
        notes=[],
    )


def _response(files):
    """A well-formed batch response covering exactly `files`.

    Content is per-extension on purpose: the scaffold parse floor rejects a
    data file that does not parse, and a rejected write lands in `missing`
    — which would look like a coverage failure the ladder had mishandled.
    """
    out = []
    for i, f in enumerate(files):
        if f.endswith(".json"):
            out.append(f'```json\n# === FILE: {f} ===\n{{"n": {i}}}\n```\n')
        else:
            out.append(f"```python\n# === FILE: {f} ===\nX = {i}\n```\n")
    return "\n".join(out)


def _slice(tmp_path, declared, covered, *, tokens, attempt=1, truncated=False):
    mission = _mission(tmp_path, declared)
    fx = MockEffects(mission=mission)
    ctx = {
        "mission": mission,
        "inference_response": _response(covered),
        "inference_truncated": truncated,
    }
    if tokens is not None:
        ctx["inference_tokens_generated"] = tokens
    out = asyncio.run(
        action_slice_batch_files(
            StepInput(
                context=ctx,
                params={},
                meta=FlowMeta(
                    flow_name="build_structure",
                    step_id="slice_batch_files",
                    attempt=attempt,
                ),
                effects=fx,
            )
        )
    )
    return out, fx


# The real shapes, from ~/ouroboros-runs on 2026-08-05.
SIX = ["models.py", "loader.py", "parser.py", "engine.py", "main.py", "world.json"]
EIGHT = [f"m{i}.py" for i in range(8)]
SEVEN = [f"n{i}.py" for i in range(7)]


class TestTheRungs:
    """Table over the verdict function itself — every branch, named."""

    @pytest.mark.parametrize(
        "case,kw,expect",
        [
            # RUNG 1 — qwen3-next salvaged 6/7 and placed mid-field. Keep it.
            (
                "substantially complete",
                dict(resolved=6, declared=7, missing=1, tokens=500),
                False,
            ),
            (
                "exactly at the floor",
                dict(resolved=2, declared=3, missing=1, tokens=10),
                False,
            ),
            ("complete", dict(resolved=6, declared=6, missing=0, tokens=7843), False),
            # RUNG 2 — the two real collapses.
            (
                "hy3[g]: 1/6 at 700 tok",
                dict(resolved=1, declared=6, missing=5, tokens=700),
                True,
            ),
            (
                "glm-4.7: 3/8 at 3521 tok",
                dict(resolved=3, declared=8, missing=5, tokens=3521),
                True,
            ),
            (
                "an empty generation",
                dict(resolved=0, declared=6, missing=6, tokens=0),
                True,
            ),
            # RUNG 3 — short, but resampling is the wrong move.
            (
                "truncated: the ceiling binds again",
                dict(resolved=1, declared=6, missing=5, tokens=700, truncated=True),
                False,
            ),
            (
                "salvaged: already the recovery rung",
                dict(resolved=1, declared=6, missing=5, tokens=700, salvaged=True),
                False,
            ),
            (
                "expensive: serial is the cheaper path",
                dict(resolved=1, declared=6, missing=5, tokens=40_000),
                False,
            ),
            (
                "retries exhausted",
                dict(
                    resolved=1,
                    declared=6,
                    missing=5,
                    tokens=700,
                    attempt=BATCH_MAX_ATTEMPTS,
                ),
                False,
            ),
            (
                "cost unknown — never resample blind",
                dict(resolved=1, declared=6, missing=5, tokens=None),
                False,
            ),
        ],
    )
    def test_verdict(self, case, kw, expect):
        kw.setdefault("attempt", 1)
        kw.setdefault("truncated", False)
        kw.setdefault("salvaged", False)
        should, why = _batch_retry_verdict(**kw)
        assert should is expect, f"{case}: {why}"
        assert (
            why
        ), "every rung must say why — serial is only a signal if it is explained"

    def test_the_budget_test_is_the_serial_cost_it_avoids(self):
        """Not an absolute ceiling — the comparison scales with the work at
        stake, so it needs no per-model tuning."""
        base = dict(
            resolved=1,
            declared=6,
            missing=5,
            attempt=1,
            truncated=False,
            salvaged=False,
        )
        budget = 5 * SERIAL_CREATE_TOKENS
        assert _batch_retry_verdict(**base, tokens=budget)[0] is True
        assert _batch_retry_verdict(**base, tokens=budget + 1)[0] is False

    def test_more_missing_files_buys_a_bigger_retry_budget(self):
        base = dict(attempt=1, truncated=False, salvaged=False, tokens=6000)
        # 5 missing -> 10k budget -> retry; 1 missing -> 2k budget -> decline
        assert (
            _batch_retry_verdict(resolved=1, declared=6, missing=5, **base)[0] is True
        )
        assert (
            _batch_retry_verdict(resolved=1, declared=2, missing=1, **base)[0] is False
        )


class TestAResampledAttemptLeavesNoTrace:
    """THE load-bearing property. If a discarded attempt wrote its files,
    the resample would splice two independent generations into one tree —
    reintroducing exactly the incoherence the batch path prevents."""

    def test_it_writes_nothing(self, tmp_path):
        """Asserted against the effects layer, NOT the real filesystem —
        MockEffects keeps its own file map and never touches tmp_path, so a
        `tmp_path.exists()` assertion here would pass no matter what the
        code did."""
        out, fx = _slice(tmp_path, SIX, SIX[:1], tokens=700)
        assert out.result["retry_batch"] is True
        assert fx.written_files == {}, "a discarded attempt must not land on disk"
        assert out.result["files_written"] == 0

    def test_it_publishes_nothing(self, tmp_path):
        """No manifest, so the resample's own slice sets the record for real
        rather than merging with an attempt that never happened."""
        out, _ = _slice(tmp_path, SIX, SIX[:1], tokens=700)
        assert out.context_updates == {}

    def test_the_kept_attempt_does_write(self, tmp_path):
        """The control for the test above: same harness, same assertion
        surface, opposite verdict — so `written_files == {}` is proven to be
        a real observation rather than a mock that never records."""
        out, fx = _slice(tmp_path, SIX, SIX, tokens=7843)
        assert out.result["retry_batch"] is False
        assert sorted(fx.written_files) == sorted(SIX)
        assert out.context_updates["batch_manifest"]["written"] == SIX


class TestExhaustedRetriesKeepTheirWork:
    """Giving up must not also throw away the files the last attempt did
    produce — that would make the ladder strictly worse than the cliff."""

    def test_the_final_attempt_writes_what_it_got(self, tmp_path):
        out, _ = _slice(tmp_path, SIX, SIX[:1], tokens=700, attempt=BATCH_MAX_ATTEMPTS)
        assert out.result["retry_batch"] is False
        assert out.context_updates["batch_manifest"]["written"] == ["models.py"]
        assert len(out.context_updates["batch_manifest"]["missing"]) == 5


class TestTheRealIncidents:
    def test_hy3_grinder_would_have_resampled(self, tmp_path):
        """tier_20260803-100554 arm01: 1/6 files, 700 tokens, not truncated.
        It fell to serial, authored five files alone over 1h49m, and shipped
        an artifact with no win path anywhere (38/47, lost)."""
        out, _ = _slice(tmp_path, SIX, SIX[:1], tokens=700)
        assert out.result["retry_batch"] is True

    def test_glm_47_flash_would_have_resampled(self, tmp_path):
        """tier_20260803-200050 arm03: 3/8 files, 3,521 tokens. 30/47, the
        floor of the batch, both conformance triggers."""
        out, _ = _slice(tmp_path, EIGHT, EIGHT[:3], tokens=3521)
        assert out.result["retry_batch"] is True

    def test_qwen3_next_would_have_been_kept(self, tmp_path):
        """tier_20260805-092309 arm01: 6/7 written and salvaged from an
        aborted generation. 86% is over the floor — rung 1 keeps it, and
        the one missing file goes serial as before."""
        out, _ = _slice(tmp_path, SEVEN, SEVEN[:6], tokens=500)
        assert out.result["retry_batch"] is False
        assert out.context_updates["batch_manifest"]["missing"] == [SEVEN[6]]

    def test_a_healthy_batch_is_untouched(self, tmp_path):
        """7 of 13 arms wrote every declared file. The ladder must be
        invisible to them."""
        out, _ = _slice(tmp_path, SIX, SIX, tokens=60_631)
        assert out.result["retry_batch"] is False
        assert out.context_updates["batch_manifest"]["missing"] == []


class TestTheRecordSaysWhichRung:
    """Serial fallback is only interpretable as a model signal if the record
    shows the cheap retries were tried and declined."""

    def test_the_manifest_carries_the_rung_and_the_attempt_count(self, tmp_path):
        out, _ = _slice(tmp_path, SIX, SIX[:1], tokens=700, attempt=BATCH_MAX_ATTEMPTS)
        manifest = out.context_updates["batch_manifest"]
        assert manifest["attempts"] == BATCH_MAX_ATTEMPTS
        assert "exhausted" in manifest["fallback_rung"]

    def test_a_complete_batch_records_the_keeping_rung(self, tmp_path):
        out, _ = _slice(tmp_path, SIX, SIX, tokens=7843)
        assert out.context_updates["batch_manifest"]["attempts"] == 1


class TestTheFlowIsWiredForIt:
    """Two wires that fail SILENTLY if they regress: an undeclared token key
    reads as None and permanently disables rung 2, and a missing resolver
    rule makes `retry_batch` a value nothing acts on."""

    @staticmethod
    def _step():
        compiled = json.loads((ROOT / "flows" / "compiled.json").read_text())
        flows = compiled.get("code_core", compiled)
        return flows["build_structure"]["steps"]["slice_and_write"]

    def test_the_token_cost_reaches_the_step(self):
        """_build_step_input filters context to what a step declares, so an
        undeclared key is not merely absent — it silently reads as unknown
        cost, which the verdict correctly refuses to resample on. The ladder
        would go quiet without a single test failing."""
        ctx = self._step()["context"]
        assert "inference_tokens_generated" in ctx.get("optional", [])

    def test_a_resample_transitions_back_to_the_generator(self):
        rules = self._step()["resolver"]["rules"]
        retry = [r for r in rules if "retry_batch" in r["condition"]]
        assert retry, "nothing routes result.retry_batch anywhere"
        assert retry[0]["transition"] == "generate_all_files"

    def test_the_resolver_carries_its_own_hard_loop_stop(self):
        """Independent of the action's budget check: the cycle must terminate
        even if that check ever regresses."""
        rules = self._step()["resolver"]["rules"]
        retry = next(r for r in rules if "retry_batch" in r["condition"])
        assert "meta.attempt" in retry["condition"]

    def test_the_retry_rule_is_evaluated_before_the_write_paths(self):
        rules = self._step()["resolver"]["rules"]
        idx = [i for i, r in enumerate(rules) if "retry_batch" in r["condition"]][0]
        assert (
            idx == 0
        ), "a later rule would claim the attempt before the resample could"
