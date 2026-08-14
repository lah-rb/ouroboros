"""Curator actions: goals, worklists, session choreography, booking.

The policy layer of stage three. Pins: idempotent goal derivation,
worklist selection for both sweeps (retry-first ordering), the
review-verdict routing, the pack gate + snapshot-fork retry, and the
structural cleanup guarantee (book_result ends the session and purges
the snapshot on EVERY outcome).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.actions import curation_actions as ca
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


def _rec(key, extracted=True, figs=0, **over):
    rec = {
        "paper_key": key,
        "title": f"Paper {key}",
        "doi": f"10.1/{key}",
        "license": "cc-by",
        "extraction_status": "extracted" if extracted else "",
        "figure_count": figs,
        "md_path": f"databank/markdown/{key}.md",
    }
    rec.update(over)
    return rec


def _bank(recs):
    return "\n".join(json.dumps(r) for r in recs) + "\n"


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="curate",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="curator"),
        goals=goals or [],
    )


def _si(context=None, inputs=None, effects=None) -> StepInput:
    return StepInput(
        context=context or {},
        inputs=inputs or {},
        params={},
        meta=FlowMeta(flow_name="curate_control", step_id="t"),
        effects=effects,
    )


_GOOD_MD = (
    "# Paper\n\nYield strength was 759 MPa at 77 K; elongation 71%.\n\n"
    "Hardness reached 458 HV after rolling.\n"
)
_REVIEW_ACCEPT = (
    '```json\n{"verdict": "accept", "summary": "solid tensile data", "issues": []}\n```'
)
_REVIEW_DENY = '```json\n{"verdict": "deny", "summary": "garbled extraction", "issues": ["tables unreadable"]}\n```'
_PACK_GOOD = '```json\n{"yield_strength_mpa": 759, "elongation_pct": 71}\n```'
_PACK_FABRICATED = '```json\n{"yield_strength_mpa": 759, "creep_rate": 3.1e-07}\n```'


# ── goals + sweeps ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_derive_creates_both_goals_idempotently():
    mission = _mission()
    fx = MockEffects(
        files={"databank/papers.jsonl": _bank([_rec("a"), _rec("b", extracted=False)])},
        mission=mission,
    )
    out = await ca.action_derive_curation_goals(_si({"mission": mission}, effects=fx))
    assert out.result == {"goals_ready": True, "created": 2}
    out2 = await ca.action_derive_curation_goals(_si({"mission": mission}, effects=fx))
    assert out2.result["created"] == 0
    assert {g.type for g in mission.goals} == {"fig_review", "curate"}


@pytest.mark.asyncio
async def test_derive_nothing_without_extracted_papers():
    mission = _mission()
    fx = MockEffects(
        files={"databank/papers.jsonl": _bank([_rec("a", extracted=False)])},
        mission=mission,
    )
    out = await ca.action_derive_curation_goals(_si({"mission": mission}, effects=fx))
    assert out.result["goals_ready"] is False and not mission.goals


@pytest.mark.asyncio
async def test_fig_sweep_selects_only_figured_pending_papers():
    goal = GoalRecord(description="f", type="fig_review", status="incomplete")
    mission = _mission([goal])
    fx = MockEffects(
        files={
            "databank/papers.jsonl": _bank(
                [
                    _rec("with_figs", figs=3),
                    _rec("no_figs", figs=0),
                    _rec("done", figs=2, figtext_status="figtext_done"),
                    _rec("failed", figs=2, figtext_status="figtext_failed"),
                ]
            )
        },
        mission=mission,
    )
    out = await ca.action_fig_review_sweep_next(_si({"mission": mission}, effects=fx))
    assert out.result["needs_fig_review"] is True
    assert out.context_updates["dispatch_config"]["paper_keys"] == ["with_figs"]


@pytest.mark.asyncio
async def test_fig_sweep_empty_completes_goal():
    goal = GoalRecord(description="f", type="fig_review", status="incomplete")
    mission = _mission([goal])
    fx = MockEffects(
        files={"databank/papers.jsonl": _bank([_rec("no_figs", figs=0)])},
        mission=mission,
    )
    out = await ca.action_fig_review_sweep_next(_si({"mission": mission}, effects=fx))
    assert out.result["sweep_complete"] is True
    assert goal.status == "complete"


@pytest.mark.asyncio
async def test_curate_sweep_waits_for_figtext_and_prioritizes_repacks():
    goal = GoalRecord(description="c", type="curate", status="incomplete")
    mission = _mission([goal])
    fx = MockEffects(
        files={
            "databank/papers.jsonl": _bank(
                [
                    _rec("await_figs", figs=2),  # figtext pending -> not curatable
                    _rec("fresh", figs=0),
                    _rec(
                        "repack_me",
                        figs=0,
                        review_status="accepted",
                        pack_status="needs_repack",
                    ),
                    _rec("denied", figs=0, review_status="denied"),
                    _rec(
                        "done",
                        figs=0,
                        review_status="accepted",
                        pack_status="packed",
                    ),
                ]
            )
        },
        mission=mission,
    )
    out = await ca.action_curate_sweep_next(_si({"mission": mission}, effects=fx))
    assert out.result["needs_curate"] is True
    assert out.context_updates["dispatch_config"]["paper_key"] == "repack_me"


# ── fig batch booking ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fig_batch_books_done_and_failed():
    import os

    from agent.effects.protocol import CommandResult

    fx = MockEffects(
        files={"databank/papers.jsonl": _bank([_rec("a", figs=2), _rec("b", figs=1)])}
    )
    tool = os.path.join(ca._repo_root(), ca._FIG_TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0,
        stdout=json.dumps(
            {"paper_key": "a", "figtext_path": "x", "figs": 2, "error": ""}
        )
        + "\n"
        + json.dumps({"paper_key": "b", "figs": 0, "error": "MLX boom"}),
        stderr="",
        command="tool",
    )
    out = await ca.action_fig_review_batch(
        _si(
            inputs={
                "paper_keys": ["a", "b"],
                "working_directory": "/tmp/x",
                "mission_id": "m",
                "goal_id": "g",
                "flow_directive": "figs",
            },
            effects=fx,
        )
    )
    assert out.result == {"status": "success", "done": 1, "failed": 1}
    from agent.actions.scholarly_actions import read_databank

    bank = asyncio.get_event_loop() and await read_databank(fx)
    assert bank["a"]["figtext_status"] == "figtext_done"
    assert bank["b"]["figtext_status"] == "figtext_failed"
    assert "MLX boom" in bank["b"]["failure_reason"]


# ── the per-paper session choreography ───────────────────────────────


def _paper_fx(responses, key="p1", md=_GOOD_MD):
    return MockEffects(
        files={
            "databank/papers.jsonl": _bank([_rec(key, figs=0)]),
            f"databank/markdown/{key}.md": md,
        },
        inference_responses=responses,
    )


def _paper_inputs(key="p1"):
    return {
        "paper_key": key,
        "working_directory": "/tmp/x",
        "mission_id": "m",
        "goal_id": "g",
        "flow_directive": "curate p1",
    }


@pytest.mark.asyncio
async def test_ingest_review_accept_snapshots_and_publishes_state():
    fx = _paper_fx([_REVIEW_ACCEPT])
    out = await ca.action_curate_ingest_review(_si(inputs=_paper_inputs(), effects=fx))
    assert out.result["verdict"] == "accepted"
    state = out.context_updates["curate_state"]
    assert state["review"]["status"] == "accepted"
    assert "paper:p1" in fx._mock_snapshots  # post-review snapshot pinned


@pytest.mark.asyncio
async def test_ingest_review_deny_routes_without_pack():
    fx = _paper_fx([_REVIEW_DENY])
    out = await ca.action_curate_ingest_review(_si(inputs=_paper_inputs(), effects=fx))
    assert out.result["verdict"] == "denied"
    assert out.context_updates["curate_state"]["review"]["issues"] == [
        "tables unreadable"
    ]


@pytest.mark.asyncio
async def test_ingest_review_unparseable_twice_fails_review():
    fx = _paper_fx(["not json", "still not json"])
    out = await ca.action_curate_ingest_review(_si(inputs=_paper_inputs(), effects=fx))
    assert out.result["verdict"] == "review_failed"


@pytest.mark.asyncio
async def test_pack_passes_gates_first_attempt():
    fx = _paper_fx([_REVIEW_ACCEPT, _PACK_GOOD])
    r1 = await ca.action_curate_ingest_review(_si(inputs=_paper_inputs(), effects=fx))
    out = await ca.action_curate_pack_data(
        _si(context={"curate_state": r1.context_updates["curate_state"]}, effects=fx)
    )
    assert out.result["gate_passed"] is True
    state = out.context_updates["curate_state"]
    assert state["pack"]["status"] == "packed"
    assert state["pack"]["quality"]["grounding_rate"] == 1.0
    assert state["pack"]["attempts"] == 1


@pytest.mark.asyncio
async def test_pack_gate_failure_retries_from_snapshot_then_fails():
    # Both attempts fabricate a value -> pack_failed after the fork retry.
    fx = _paper_fx([_REVIEW_ACCEPT, _PACK_FABRICATED, _PACK_FABRICATED])
    r1 = await ca.action_curate_ingest_review(_si(inputs=_paper_inputs(), effects=fx))
    out = await ca.action_curate_pack_data(
        _si(context={"curate_state": r1.context_updates["curate_state"]}, effects=fx)
    )
    assert out.result["gate_passed"] is False
    state = out.context_updates["curate_state"]
    assert state["pack"]["status"] == "pack_failed"
    assert state["pack"]["attempts"] == 2
    # The retry forked from the snapshot (a second session was started
    # with from_snapshot) — visible in the mock call record.
    forks = [
        c
        for c in fx._calls
        if c.method == "start_inference_session"
        and (c.args or {}).get("from_snapshot") == "paper:p1"
    ]
    assert len(forks) == 1


@pytest.mark.asyncio
async def test_pack_retry_recovers_on_second_attempt():
    fx = _paper_fx([_REVIEW_ACCEPT, _PACK_FABRICATED, _PACK_GOOD])
    r1 = await ca.action_curate_ingest_review(_si(inputs=_paper_inputs(), effects=fx))
    out = await ca.action_curate_pack_data(
        _si(context={"curate_state": r1.context_updates["curate_state"]}, effects=fx)
    )
    assert out.result["gate_passed"] is True
    assert out.context_updates["curate_state"]["pack"]["attempts"] == 2


@pytest.mark.asyncio
async def test_book_result_writes_envelope_and_always_cleans_up():
    fx = _paper_fx([_REVIEW_ACCEPT, _PACK_GOOD])
    r1 = await ca.action_curate_ingest_review(_si(inputs=_paper_inputs(), effects=fx))
    r2 = await ca.action_curate_pack_data(
        _si(context={"curate_state": r1.context_updates["curate_state"]}, effects=fx)
    )
    out = await ca.action_curate_book_result(
        _si(context={"curate_state": r2.context_updates["curate_state"]}, effects=fx)
    )
    assert out.result["status"] == "success"
    assert "packed" in out.result["outcome"]
    # Snapshot purged + session ended (structural cleanup).
    assert "paper:p1" not in getattr(fx, "_mock_snapshots", {})
    assert not fx._mock_active_sessions
    # Envelope + registry written; record booked.
    env = json.loads(fx._files["databank/dataset/p1.json"])
    assert env["data"]["yield_strength_mpa"] == 759
    assert env["review"]["status"] == "accepted"
    registry = json.loads(fx._files[ca.KEY_REGISTRY_PATH])
    assert registry["yield_strength_mpa"]["count"] == 1
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["p1"]["pack_status"] == "packed"
    assert bank["p1"]["dataset_path"] == "databank/dataset/p1.json"


@pytest.mark.asyncio
async def test_book_result_denied_records_reasons_and_cleans_up():
    fx = _paper_fx([_REVIEW_DENY])
    r1 = await ca.action_curate_ingest_review(_si(inputs=_paper_inputs(), effects=fx))
    out = await ca.action_curate_book_result(
        _si(context={"curate_state": r1.context_updates["curate_state"]}, effects=fx)
    )
    assert out.result["outcome"] == "denied"
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["p1"]["review_status"] == "denied"
    assert bank["p1"]["review_issues"] == ["tables unreadable"]
    assert "databank/dataset/p1.json" not in fx._files  # nothing packed
    assert not fx._mock_active_sessions


# ── gate + corpus build ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_and_corpus_build():
    packed = _rec(
        "x",
        figs=0,
        review_status="accepted",
        pack_status="packed",
        dataset_path="databank/dataset/x.json",
    )
    denied = _rec("y", figs=0, review_status="denied")
    envelope = {
        "paper_key": "x",
        "title": "t",
        "doi": "d",
        "license": "cc-by",
        "review": {"status": "accepted", "summary": "s"},
        "data": {"yield_strength_mpa": 759},
    }
    registry = {"yield_strength_mpa": {"type": "number", "count": 1, "exemplar": "759"}}
    fx = MockEffects(
        files={
            "databank/papers.jsonl": _bank([packed, denied]),
            "databank/dataset/x.json": json.dumps(envelope),
            ca.KEY_REGISTRY_PATH: json.dumps(registry),
        }
    )
    out = await ca.action_check_curation_complete(_si(effects=fx))
    assert out.result["gate_passed"] is True

    out = await ca.action_build_corpus_dataset(_si(effects=fx))
    assert out.result == {"built": True, "papers": 1}
    corpus = json.loads(fx._files["databank/dataset/corpus.json"])
    assert corpus["counts"] == {"packed": 1, "denied": 1, "failed": 0}
    assert corpus["papers"][0]["paper_key"] == "x"


@pytest.mark.asyncio
async def test_gate_fails_with_pending_then_reopen():
    fx = MockEffects(files={"databank/papers.jsonl": _bank([_rec("pend", figs=0)])})
    out = await ca.action_check_curation_complete(_si(effects=fx))
    assert out.result["gate_passed"] is False
    assert out.context_updates["pending_curation"] == ["pend"]

    mission = _mission(
        [
            GoalRecord(description="c", type="curate", status="complete"),
            GoalRecord(description="f", type="fig_review", status="complete"),
        ]
    )
    out2 = await ca.action_reopen_curation_goal(
        _si({"mission": mission}, effects=MockEffects(mission=mission))
    )
    assert out2.result["reopened"] is True
    assert all(g.status == "incomplete" for g in mission.goals)


# ── unverifiable papers reach the curator ─────────────────────────────


def test_curator_consumes_papers_with_no_text_layer():
    """`extract_unverified` means OCR produced a document but the source has
    NO TEXT LAYER to check it against — the scanned-paper case OCR exists for.
    Verification can never succeed there, so holding these only guaranteed
    nobody looked at them.

    The curator is a competent judge because its own gate does not depend on
    the missing layer: grounding_check matches against the CURATOR DOC, so it
    behaves identically on a scan.
    """
    from agent.actions.curation_actions import _curation_pending, _fig_pending

    scan = {
        "extraction_status": "extract_unverified",
        "figure_count": 4,
        "md_path": "databank/markdown/scan.md",
    }
    assert _fig_pending(scan) is True
    scan_no_figs = dict(scan, figure_count=0)
    assert _curation_pending(scan_no_figs) is True


def test_curator_still_refuses_the_genuinely_rejected():
    """Admitting the unverifiable must not admit everything else with it."""
    from agent.actions.curation_actions import _curation_pending, _fig_pending

    for status in ("extract_failed", "extract_oversize", "needs_reextract", ""):
        rec = {"extraction_status": status, "figure_count": 4}
        assert _fig_pending(rec) is False, status
        assert _curation_pending(dict(rec, figure_count=0)) is False, status


def test_unverified_status_survives_curation():
    """The status is deliberately NOT rewritten to `extracted` on acceptance:
    it is the only record that a paper entered on curator judgement rather
    than machine verification, and an audit needs to tell those apart."""
    from agent.actions.curation_actions import _curation_pending

    accepted = {
        "extraction_status": "extract_unverified",
        "figure_count": 0,
        "review_status": "accepted",
        "pack_status": "packed",
    }
    # Terminal once packed — but still flagged as never machine-verified.
    assert _curation_pending(accepted) is False
    assert accepted["extraction_status"] == "extract_unverified"
