"""`tier extend`, and the state-integrity fixes that had to land under it.

An arm whose mission parks on its wall inside a FINISHED batch is in
exactly the resumable state `tier pause` produces — `status: paused` with
`cycles_consumed` on disk — but `tier resume` only finds a batch parked by
the explicit pause verb, so there was no way back. step37 sat at 15 of 30
contemplator cycles; deepseek at 22.

Three things already broken made re-entry unsafe, and all three bite
`tier resume` today:

  * `_write_state` was a full overwrite fed PARTIAL callers, so every 30s
    heartbeat dropped `results` and a re-entry narrowed `arms` to the
    remaining queue.
  * `_stage` keyed on the loop index, which on any re-entry restarts at 1
    — `tier resume` on arm 3 of 6 re-stages into `arm01` and stage.py
    rmtree's it first, destroying a DIFFERENT model's artifact.
  * `cp -a` into an existing `<label>_agent` nests instead of refreshing.

And one hazard specific to extend, which turned out to be the load-bearing
safety property: `/tmp/tier/<label>` is keyed by model name alone, so a
later batch running the same model overwrites it. Verified on disk
2026-08-05 — both arms of tier_20260803-151411 had been replaced by the
08-04 re-runs, and both replacements happened to sit at exactly 30 cycles,
so a `cycles < 30` guard would have caught it by luck alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tier import runner as tr
from agent.tier.runner import ArmResult, TierRun


def _state(base: Path, **kw) -> None:
    base.mkdir(parents=True, exist_ok=True)
    base.joinpath("STATE.json").write_text(json.dumps(kw))


def _manifest(base: Path, rows: list[tuple[str, str, str]]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    base.joinpath("MANIFEST.txt").write_text(
        "".join(f"{d}  {c}  {s}\n" for d, c, s in rows)
    )


def _mission(work: Path, **kw) -> None:
    (work / ".agent").mkdir(parents=True, exist_ok=True)
    (work / ".agent" / "mission.json").write_text(json.dumps(kw))


def _run(base: Path, arms: list[str]) -> TierRun:
    return TierRun(arms=arms, base=base)


class TestStateIsAlwaysComplete:
    def test_a_reentry_keeps_every_other_arms_result(self, tmp_path):
        base = tmp_path / "r"
        _state(
            base,
            arms=["a", "b", "c"],
            results=[{"config": c, "status": "completed"} for c in "abc"],
        )
        run = _run(base, ["c"])
        run._adopt_prior_state()
        run._record(ArmResult("c", "completed", minutes=99))
        run._write_state(finished=True)
        data = json.loads((base / "STATE.json").read_text())
        assert [r["config"] for r in data["results"]] == ["a", "b", "c"]
        assert data["results"][2]["minutes"] == 99

    def test_a_reentry_replaces_rather_than_duplicates(self, tmp_path):
        base = tmp_path / "r"
        _state(base, arms=["a"], results=[{"config": "a", "status": "completed"}])
        run = _run(base, ["a"])
        run._adopt_prior_state()
        run._record(ArmResult("a", "completed", minutes=42))
        assert len(run.results) == 1 and run.results[0].minutes == 42

    def test_a_failed_reentry_does_not_erase_a_good_record(self, tmp_path):
        """STATE.json must not end up denying an artifact still on disk."""
        base = tmp_path / "r"
        _state(
            base,
            arms=["a"],
            results=[{"config": "a", "status": "completed", "staged": "s/arm01"}],
        )
        run = _run(base, ["a"])
        run._adopt_prior_state()
        run._record(ArmResult("a", "resume_lost", detail="workspace gone"))
        assert run.results[0].status == "completed"
        assert run.results[0].staged == "s/arm01"
        assert "resume_lost" in run.results[0].detail

    def test_heartbeat_writes_do_not_drop_results(self, tmp_path):
        base = tmp_path / "r"
        base.mkdir()
        run = _run(base, ["a"])
        run.results = [ArmResult("a", "completed")]
        run._write_state(current_arm="a", index=1)  # the heartbeat shape
        assert json.loads((base / "STATE.json").read_text())["results"]

    def test_arms_never_narrows_on_reentry(self, tmp_path):
        base = tmp_path / "r"
        _state(base, arms=["a", "b", "c"], results=[])
        run = _run(base, ["c"])
        run._adopt_prior_state()
        run._write_state(finished=True)
        assert json.loads((base / "STATE.json").read_text())["arms"] == ["a", "b", "c"]

    def test_a_fresh_run_writes_exactly_its_own_arms(self, tmp_path):
        """Pins test_tier_runner.py's atomic-write expectation."""
        base = tmp_path / "r"
        base.mkdir()
        run = _run(base, ["a", "b"])
        run._write_state(finished=True)
        assert json.loads((base / "STATE.json").read_text())["arms"] == ["a", "b"]

    def test_unknown_and_missing_result_keys_are_tolerated(self, tmp_path):
        base = tmp_path / "r"
        _state(
            base,
            arms=["a"],
            results=[{"config": "a", "status": "completed", "from_the_future": 1}],
        )
        run = _run(base, ["a"])
        run._adopt_prior_state()
        assert run.results[0].config == "a"

    def test_corrupt_prior_state_does_not_abort(self, tmp_path):
        base = tmp_path / "r"
        base.mkdir()
        (base / "STATE.json").write_text("{not json")
        run = _run(base, ["a"])
        run._adopt_prior_state()
        assert run.results == []


class TestTheStagedSlot:
    def test_slot_comes_from_the_manifest_not_the_loop_index(self, tmp_path):
        """The headline regression: a one-arm re-entry must not write arm01
        over a different model's artifact."""
        base = tmp_path / "r"
        _manifest(
            base,
            [
                (f"{base}/staged/arm{i:02d}", c, "staged")
                for i, c in enumerate("abcde", 1)
            ],
        )
        run = _run(base, ["e"])
        run._adopted = True
        assert run._slot_for(1, "e") == 5

    def test_bracket_labels_resolve(self, tmp_path):
        base = tmp_path / "r"
        _manifest(base, [(f"{base}/staged/arm03", "hy3-reap-200b-a21[c]", "staged")])
        assert tr.arm_slot(base, "hy3-reap-200b-a21[c]") == 3

    def test_an_arm_that_never_staged_gets_a_free_slot(self, tmp_path):
        base = tmp_path / "r"
        _manifest(
            base,
            [
                (f"{base}/staged/arm01", "a", "staged"),
                ("-", "b", "NO_ARTIFACT"),
            ],
        )
        run = _run(base, ["b"])
        run._adopted = True
        assert run._slot_for(1, "b") == 2  # not 1 — that is a's

    def test_fresh_run_numbering_is_unchanged(self, tmp_path):
        """Slots are cited in LADDER.md; a fresh run must keep using the loop
        index even when an earlier arm produced no artifact."""
        base = tmp_path / "r"
        _manifest(base, [("-", "a", "NO_ARTIFACT")])
        run = _run(base, ["a", "b"])
        assert run._slot_for(2, "b") == 2

    def test_malformed_manifest_lines_are_skipped(self, tmp_path):
        base = tmp_path / "r"
        base.mkdir()
        (base / "MANIFEST.txt").write_text("garbage\n\nalso  bad\n")
        assert tr.manifest_rows(base) == []


class TestTheWorkspaceIdentityGuard:
    def test_a_reused_workspace_is_refused(self, tmp_path, monkeypatch):
        """THE incident, with its real numbers. Asserts on the ID MISMATCH,
        not the cycle count — both real replacements sat at exactly 30, so a
        cycles-only guard would have passed them by luck."""
        base = tmp_path / "tier_x"
        _state(base, arms=["step37"], finished=True, results=[{"config": "step37"}])
        (base / "step37_agent").mkdir(parents=True)
        (base / "step37_agent" / "mission.json").write_text(
            json.dumps({"mission_id": "bfec03f24352", "cycles_consumed": 15})
        )
        work = tmp_path / "work" / "step37"
        _mission(work, mission_id="8741c9005b02", status="paused", cycles_consumed=15)

        monkeypatch.setattr(tr, "TIER_WORK_ROOT", tmp_path / "work")
        monkeypatch.setattr(tr, "ROOT", tmp_path)
        (tmp_path / "llmvp" / "configs").mkdir(parents=True)
        (tmp_path / "llmvp" / "configs" / "step37.yaml").write_text(
            "tier:\n  league: contemplator\n"
        )

        row = tr.extend_candidates(base)[0]
        assert row["verdict"] == "workspace_reused"
        assert row["snapshot_id"] == "bfec03f24352"
        assert row["live_id"] == "8741c9005b02"
        # The cycle count alone would NOT have caught it.
        assert row["cycles"] < tr.CONTEMPLATOR_CYCLES

    def test_the_runner_refuses_a_reused_workspace_too(self, tmp_path):
        """Second enforcement: discovery and boot are seconds apart."""
        base = tmp_path / "r"
        base.mkdir()
        work = tmp_path / "w"
        _mission(work, mission_id="LIVE", status="paused")
        run = _run(base, ["a"])
        res, control = run._run_arm(
            1,
            "a",
            resume={
                "config": "a",
                "work": str(work),
                "mission_id": "SNAP",
                "consumed_s": 0.0,
            },
        )
        assert res.status == "resume_lost"


class TestVerdictLadder:
    @pytest.mark.parametrize(
        "status,cycles,expect",
        [
            ("paused", 15, "eligible"),
            ("paused", 30, "budget_spent"),
            ("completed", 12, "mission_completed"),
            ("active", 12, "mission_active"),
            ("", 0, "mission_unknown"),
        ],
        ids=["eligible", "spent", "completed", "active", "unknown"],
    )
    def test_status_and_budget(self, tmp_path, monkeypatch, status, cycles, expect):
        base = tmp_path / "r"
        _state(base, arms=["m"], finished=True, results=[])
        (base / "m_agent").mkdir(parents=True)
        (base / "m_agent" / "mission.json").write_text(json.dumps({"mission_id": "X"}))
        monkeypatch.setattr(tr, "arm_league", lambda a: (a, "contemplator"))
        monkeypatch.setattr(tr, "mission_id", lambda w: "X")
        monkeypatch.setattr(tr, "mission_state", lambda w: (status, cycles))
        monkeypatch.setattr(tr, "ROOT", tmp_path)
        (tmp_path / "llmvp" / "configs").mkdir(parents=True)
        (tmp_path / "llmvp" / "configs" / "m.yaml").write_text("x: 1\n")
        monkeypatch.setattr(Path, "is_dir", lambda self: True)
        assert tr.extend_candidates(base)[0]["verdict"] == expect

    def test_a_grinder_is_refused(self, tmp_path, monkeypatch):
        base = tmp_path / "r"
        _state(base, arms=["g"], finished=True, results=[])
        monkeypatch.setattr(tr, "arm_league", lambda a: (a, "grinder"))
        monkeypatch.setattr(tr, "ROOT", tmp_path)
        (tmp_path / "llmvp" / "configs").mkdir(parents=True)
        (tmp_path / "llmvp" / "configs" / "g.yaml").write_text("x: 1\n")
        assert tr.extend_candidates(base)[0]["verdict"] == "grinder"

    def test_a_missing_config_reports_as_such_not_as_a_grinder(
        self, tmp_path, monkeypatch
    ):
        """Order matters: config_league defaults a missing config to grinder,
        so checking league first would give a misleading remedy."""
        base = tmp_path / "r"
        _state(base, arms=["ghost"], finished=True, results=[])
        monkeypatch.setattr(tr, "ROOT", tmp_path)
        (tmp_path / "llmvp" / "configs").mkdir(parents=True)
        assert tr.extend_candidates(base)[0]["verdict"] == "no_config"


class TestBaseClaim:
    def test_a_live_owner_blocks_a_second_worker(self, tmp_path):
        base = tmp_path / "r"
        _state(base, arms=["a"], pid=1)  # pid 1 is always alive
        assert _run(base, ["a"])._claim_base() is False

    def test_a_dead_owner_does_not_block(self, tmp_path):
        base = tmp_path / "r"
        _state(base, arms=["a"], pid=999_999)
        assert _run(base, ["a"])._claim_base() is True

    def test_a_fresh_base_is_claimable(self, tmp_path):
        base = tmp_path / "r"
        base.mkdir()
        assert _run(base, ["a"])._claim_base() is True


class TestAgentSnapshot:
    def test_it_does_not_nest_on_reentry(self, tmp_path):
        """The identity guard reads <label>_agent/mission.json; a nested copy
        would leave it reading a stale snapshot forever."""
        base = tmp_path / "r"
        base.mkdir()
        work = tmp_path / "w"
        _mission(work, mission_id="A")
        run = _run(base, ["m"])
        run._snapshot_agent(work, "m")
        _mission(work, mission_id="B")
        run._snapshot_agent(work, "m")
        assert not (base / "m_agent" / ".agent").exists()
        assert tr.snapshot_mission_id(base, "m") == "B"
