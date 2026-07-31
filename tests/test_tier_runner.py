"""Tier batch runner — the failures it was built from.

Every test here is a bug that actually happened, either in the 2026-07-29 shell
version or while porting it. None of them need a server or a model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tier import cli as tier_cli
from agent.tier import runner
from agent.tier.runner import DEGEN_MARKER, TierRun


@pytest.fixture
def run(tmp_path: Path) -> TierRun:
    base = tmp_path / "tier_run"
    base.mkdir()
    return TierRun(arms=["a", "b"], base=base)


class TestControl:
    """A control word must fire ONCE."""

    def test_control_word_is_consumed(self, run: TierRun):
        # The shell version's SKIP sentinel had to be unlinked by hand. Leaving
        # it in place would skip the next arm, and the next, silently ending a
        # whole batch on one keystroke.
        (run.base / "CONTROL").write_text("skip")
        assert run._take_control() == "skip"
        assert run._take_control() is None
        assert not (run.base / "CONTROL").exists()

    @pytest.mark.parametrize("word", ["skip", "stop", "force-stop"])
    def test_all_three_verbs_recognised(self, run: TierRun, word: str):
        (run.base / "CONTROL").write_text(word + "\n")
        assert run._take_control() == word

    def test_garbage_is_ignored_but_cleared(self, run: TierRun):
        (run.base / "CONTROL").write_text("halt-and-catch-fire")
        assert run._take_control() is None
        assert not (run.base / "CONTROL").exists(), "a stale word must not linger"

    def test_absent_control_is_none(self, run: TierRun):
        assert run._take_control() is None


class TestAuthoredFileCounting:
    """The 622-vs-9 lesson."""

    def test_prunes_venv_cache_and_agent(self, run: TierRun, tmp_path: Path):
        work = tmp_path / "work"
        for rel in ("main.py", "world.yaml", "pkg/engine.py"):
            p = work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        # Noise that once inflated one arm to 622 files against another's 80,
        # reading as a 7x productivity gap. Real counts were 9 and 11.
        for rel in (
            ".venv/lib/thing.py",
            "__pycache__/main.pyc",
            ".ruff_cache/x",
            ".agent/mission.json",
            ".git/HEAD",
            "pkg/__pycache__/engine.pyc",
            "OUTCOME",
        ):
            p = work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("noise")

        names = sorted(p.name for p in run._authored(work))
        assert names == ["engine.py", "main.py", "world.yaml"]

    def test_nested_cache_dirs_are_pruned_at_any_depth(
        self, run: TierRun, tmp_path: Path
    ):
        work = tmp_path / "w"
        deep = work / "a" / "b" / "__pycache__" / "z.pyc"
        deep.parent.mkdir(parents=True)
        deep.write_text("x")
        assert run._authored(work) == []


class TestDegenerationCounting:
    """Count EVENTS, not log lines."""

    def test_one_abort_counts_once(self, run: TierRun, tmp_path: Path):
        # The shell heartbeat grepped `degenerat|long-cycle|repetition guard`
        # and reported 5 for a single abort, because one event writes several
        # matching lines. It nearly went into a record as a five-fold problem.
        slog = tmp_path / "server.log"
        slog.write_text(
            "INFO long-cycle guard armed\n"
            "INFO checking repetition guard window\n"
            f"WARNING Server {DEGEN_MARKER}: long-cycle: 3539/32737 distinct\n"
            "INFO long-cycle specimen written to logs/runaway_captures/x.txt\n"
            "INFO degenerate specimen size 32768\n"
        )
        assert slog.read_text().count(DEGEN_MARKER) == 1

    def test_two_aborts_count_twice(self, run: TierRun, tmp_path: Path):
        slog = tmp_path / "server.log"
        slog.write_text(f"a {DEGEN_MARKER}: x\nnoise\nb {DEGEN_MARKER}: y\n")
        assert slog.read_text().count(DEGEN_MARKER) == 2


class TestRunDiscovery:
    def test_run_dirs_excludes_sidecar_logs(self, tmp_path: Path, monkeypatch):
        # `tier_*` also matches `tier_driver_<stamp>.log`; without an is_dir
        # filter `status` reported a log FILE as the most recent run.
        monkeypatch.setattr(tier_cli, "RUNS", tmp_path)
        (tmp_path / "tier_20260729-010118").mkdir()
        (tmp_path / "tier_driver_20260729-010118.log").write_text("log")
        assert [p.name for p in tier_cli._run_dirs()] == ["tier_20260729-010118"]

    def test_finished_run_is_not_active(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(tier_cli, "RUNS", tmp_path)
        base = tmp_path / "tier_x"
        base.mkdir()
        import os

        (base / "STATE.json").write_text(
            json.dumps({"pid": os.getpid(), "finished": True})
        )
        assert (
            tier_cli._active_run() is None
        ), "a finished run must not take control verbs"

    def test_dead_worker_is_not_active(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(tier_cli, "RUNS", tmp_path)
        base = tmp_path / "tier_y"
        base.mkdir()
        (base / "STATE.json").write_text(json.dumps({"pid": 999_999_999}))
        assert tier_cli._active_run() is None

    def test_live_unfinished_run_is_active(self, tmp_path: Path, monkeypatch):
        import os

        monkeypatch.setattr(tier_cli, "RUNS", tmp_path)
        base = tmp_path / "tier_z"
        base.mkdir()
        (base / "STATE.json").write_text(json.dumps({"pid": os.getpid()}))
        assert tier_cli._active_run() == base

    def test_corrupt_state_does_not_crash_discovery(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(tier_cli, "RUNS", tmp_path)
        base = tmp_path / "tier_bad"
        base.mkdir()
        (base / "STATE.json").write_text("{not json")
        assert tier_cli._active_run() is None


class TestPauseAndResume:
    """Pause is the interleaving verb: park an arm, free the machine, come back."""

    def test_pause_is_chain_ending_and_a_control_word(self):
        from agent.tier.runner import CHAIN_ENDING, CONTROL_WORDS

        assert "pause" in CONTROL_WORDS and "pause" in CHAIN_ENDING

    def test_resumed_arm_gets_only_the_time_it_had_left(self, run: TierRun):
        run.wall = "2h"
        assert run._remaining_wall_s(0) == 7200
        assert run._remaining_wall_s(40 * 60) == 80 * 60  # paused at 40min
        # Three pauses must not buy eight hours.
        assert run._remaining_wall_s(115 * 60) == 5 * 60

    def test_remaining_wall_never_zero(self, run: TierRun):
        assert run._remaining_wall_s(99_999) == 60, "a resume must not be a no-op"

    def test_pause_keeps_the_current_arm_at_the_head_of_the_queue(
        self, run: TierRun, monkeypatch
    ):
        # The arm being paused is the one to resume, so it stays queued. `stop`
        # drops it — it finished. Getting this backwards either re-runs a
        # finished arm or silently loses an unfinished one.
        run.arms = ["a", "b", "c"]
        monkeypatch.setattr(
            run, "_run_arm", lambda i, c, resume=None: (_stub(c), "pause")
        )
        monkeypatch.setattr(run, "_finish", lambda: None)
        run.execute()
        assert run._remaining == ["a", "b", "c"]

    def test_stop_drops_the_finished_arm_from_the_queue(
        self, run: TierRun, monkeypatch
    ):
        run.arms = ["a", "b", "c"]
        monkeypatch.setattr(
            run, "_run_arm", lambda i, c, resume=None: (_stub(c), "stop")
        )
        monkeypatch.setattr(run, "_finish", lambda: None)
        run.execute()
        assert run._remaining == ["b", "c"]

    def test_only_the_first_arm_of_a_resumed_run_resumes(
        self, run: TierRun, monkeypatch
    ):
        # Everything after the resumed arm must start fresh, or arm 2 would try
        # to `mission resume` a workspace that was never created.
        seen = []
        run.arms = ["a", "b"]
        run.resume_from = {"config": "a", "work": "/tmp/tier/a", "consumed_s": 600}
        monkeypatch.setattr(
            run,
            "_run_arm",
            lambda i, c, resume=None: (seen.append(resume), (_stub(c), None))[1],
        )
        monkeypatch.setattr(run, "_finish", lambda: None)
        run.execute()
        assert seen[0] is not None and seen[1] is None

    def test_paused_run_is_neither_active_nor_lost(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(tier_cli, "RUNS", tmp_path)
        base = tmp_path / "tier_p"
        base.mkdir()
        (base / "STATE.json").write_text(
            json.dumps(
                {
                    "pid": 999_999_999,  # worker exited on pause
                    "finished": False,
                    "paused": True,
                    "paused_arm": {"config": "laguna-xs-2.1", "consumed_s": 2400},
                    "remaining_arms": ["laguna-xs-2.1", "gemma-4-31b"],
                }
            )
        )
        assert (
            tier_cli._active_run() is None
        ), "a paused run must not take live control verbs"
        found = tier_cli._paused_runs()
        assert len(found) == 1 and found[0][0] == base

    def test_finished_run_is_not_offered_for_resume(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(tier_cli, "RUNS", tmp_path)
        base = tmp_path / "tier_f"
        base.mkdir()
        (base / "STATE.json").write_text(
            json.dumps({"pid": 1, "finished": True, "paused": True})
        )
        assert tier_cli._paused_runs() == []

    def test_crashed_run_is_not_offered_for_resume(self, tmp_path: Path, monkeypatch):
        """Unfinished is NOT the same as paused. A worker that died mid-arm
        leaves `finished: false` with no `paused_arm`; offering it for resume
        would KeyError on the way in, and there is nothing to resume TO — the
        mission was never parked, so the arm has no clean restart point."""
        monkeypatch.setattr(tier_cli, "RUNS", tmp_path)
        base = tmp_path / "tier_crashed"
        base.mkdir()
        (base / "STATE.json").write_text(
            json.dumps(
                {
                    "pid": 999_999_999,  # dead worker
                    "finished": False,  # never reached _finish()
                    "current_arm": "gemma-4-31b",
                }
            )
        )  # note: no "paused", no "paused_arm"
        assert tier_cli._paused_runs() == []
        assert tier_cli._active_run() is None


def _stub(config: str):
    from agent.tier.runner import ArmResult

    return ArmResult(config, "completed")


class TestDefaults:
    def test_server_comes_down_after_the_chain(self):
        # A batch is scheduled work, not a service window. Leaving ~75-98GB
        # wired for nobody is the wrong resting state.
        assert TierRun(arms=["a"]).leave_server_up is False

    def test_state_file_is_written_atomically(self, run: TierRun):
        run._write_state(current_arm="a", index=1)
        data = json.loads((run.base / "STATE.json").read_text())
        assert data["current_arm"] == "a" and data["arms"] == ["a", "b"]
        assert not (run.base / "STATE.json.tmp").exists(), "temp file must be renamed"


class TestRubricProvenance:
    """The label naming which instrument scored an arm must come FROM the
    instrument. It was hardcoded `v1.0` and went on saying so after the rubric
    became v1.1 — a provenance field that lies is worse than one that is absent,
    because a later reader has no reason to doubt it."""

    def test_the_real_rubric_resolves_to_a_version_it_actually_contains(self):
        """Against the SHIPPED file, not a fixture — deriving a version is only
        useful if it works on the instrument in the repo. Checked by containment
        rather than by re-running the regex, so the test cannot pass by agreeing
        with a broken implementation."""
        got = runner._rubric_version()
        assert got.startswith("TIER_RUBRIC_v"), got
        version = got.removeprefix("TIER_RUBRIC_v")
        assert version[0].isdigit(), f"no version resolved: {got}"
        first_line = runner.RUBRIC.read_text().lstrip().splitlines()[0]
        assert f"v{version}" in first_line

    def test_it_tracks_the_file_rather_than_a_constant(self, tmp_path, monkeypatch):
        bumped = tmp_path / "TIER_RUBRIC_v1.md"
        bumped.write_text("# TIER_RUBRIC v9.7 — solo artifact scoring\n\nbody\n")
        monkeypatch.setattr(runner, "RUBRIC", bumped)
        assert runner._rubric_version() == "TIER_RUBRIC_v9.7"

    def test_a_missing_rubric_says_so_instead_of_asserting_a_version(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(runner, "RUBRIC", tmp_path / "gone.md")
        assert runner._rubric_version() == "TIER_RUBRIC(unreadable)"

    def test_an_unversioned_header_says_so(self, tmp_path, monkeypatch):
        plain = tmp_path / "TIER_RUBRIC_v1.md"
        plain.write_text("# The rubric\n")
        monkeypatch.setattr(runner, "RUBRIC", plain)
        assert runner._rubric_version() == "TIER_RUBRIC(unversioned)"

    def test_an_empty_rubric_does_not_raise(self, tmp_path, monkeypatch):
        empty = tmp_path / "TIER_RUBRIC_v1.md"
        empty.write_text("")
        monkeypatch.setattr(runner, "RUBRIC", empty)
        assert runner._rubric_version() == "TIER_RUBRIC(unreadable)"


class TestDegenCounterReadsTheRightFile:
    """`degen` was a dead signal until 2026-07-30.

    DEGEN_MARKER ("aborted the generation as degenerate") is written by the
    AGENT into run.log. Both counters read server.log, so the count could
    never rise: every tier arm ever recorded degen=0 — 15 arms across every
    sweep — including a laguna-s-2.1-apex arm whose long-cycle guard
    demonstrably fired and wrote a runaway capture. A counter that cannot go
    up is indistinguishable from a clean run, which is the failure shape this
    session kept finding.
    """

    def test_both_counters_read_the_run_log(self):
        import inspect

        from agent.tier import runner

        src = inspect.getsource(runner)
        # every DEGEN_MARKER count must be against rlog
        for line in src.splitlines():
            if "DEGEN_MARKER" in line and ".count(" in line:
                assert "rlog" in line, f"counts the wrong file: {line.strip()}"

    def test_tally_takes_the_run_log(self):
        import inspect

        from agent.tier.runner import TierRun

        sig = inspect.signature(TierRun._tally)
        assert "rlog" in sig.parameters, "_tally must receive the run log"
        assert "slog" not in sig.parameters

    def test_a_real_abort_line_is_counted(self, tmp_path):
        from agent.tier.runner import DEGEN_MARKER

        rlog = tmp_path / "arm_run.log"
        rlog.write_text(
            "INFO | working\n"
            "WARNING | Server aborted the generation as degenerate: long-cycle: "
            "long-cycle repetition: 3886/32737 distinct 32B n-grams\n"
            "INFO | continuing\n"
        )
        assert rlog.read_text().count(DEGEN_MARKER) == 1

    def test_one_event_counts_once_not_per_matching_line(self):
        """The pre-existing reason the marker is a full sentence: a single
        abort writes several lines mentioning degeneration."""
        from agent.tier.runner import DEGEN_MARKER

        blob = (
            "WARNING | Server aborted the generation as degenerate: long-cycle\n"
            "INFO | long-cycle repetition detected\n"
            "INFO | repetition guard tripped\n"
        )
        assert blob.count(DEGEN_MARKER) == 1
