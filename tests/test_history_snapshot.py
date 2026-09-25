"""Workspace tree snapshots: what the history versions and how.

The store versions the WORKSPACE, from the filesystem — so a file a PTY shell
or a formatter wrote is caught at the next boundary even though no
``write_file`` saw it. These tests pin what goes into the tree (and what
never does), that an unchanged tree makes no commit, that the stat cache
avoids re-hashing, that big or binary files become stubs, and that the
restore path — which rollback rides — puts bytes back exactly.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent.effects.local import LocalEffects
from agent.history import reader
from agent.history.snapshot import WorkspaceSnapshotter
from agent.trace import CycleStart, StepEnd, step_context

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _ws(tmp_path) -> str:
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "mission.json").write_text('{"id": "m1", "goals": []}')
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("A = 1\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "big.py").write_text("x" * 1000)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("1")
    (tmp_path / ".env").write_text("SECRET=1\n")
    return str(tmp_path)


def _snap(ws: str, **kw) -> WorkspaceSnapshotter:
    return WorkspaceSnapshotter(
        ws, os.path.join(ws, ".agent", "history", "repo.git"), **kw
    )


def test_first_snapshot_carries_the_files_and_mission_state_but_no_excluded_dir(
    tmp_path,
):
    ws = _ws(tmp_path)
    snap = _snap(ws)
    res = snap.snapshot(message="run_start", source="run_start")
    assert res is not None and res.parent_sha == ""
    paths = {p for p, _, _ in snap.tree_entries(res.commit_sha)}
    assert paths == {"main.py", "pkg/a.py", ".agent/mission.json"}
    assert not any(
        p.startswith((".venv", ".git", "__pycache__", "node_modules")) for p in paths
    )
    assert ".env" not in paths
    assert snap.head() == res.commit_sha
    assert not os.path.exists(
        os.path.join(ws, ".git", "objects")
    ), "the workspace's own .git is untouched"
    assert not os.path.exists(os.path.join(ws, ".git", "refs", "heads", "history"))


def test_an_unchanged_tree_makes_no_commit(tmp_path):
    ws = _ws(tmp_path)
    snap = _snap(ws)
    first = snap.snapshot(message="a", source="run_start")
    assert snap.snapshot(message="b", source="step_end") is None
    assert snap.head() == first.commit_sha


def test_an_out_of_band_write_is_caught_at_the_next_boundary(tmp_path):
    """Simulates a PTY shell or formatter: plain open().write, no effects."""
    ws = _ws(tmp_path)
    snap = _snap(ws)
    snap.snapshot(message="a", source="run_start")
    with open(os.path.join(ws, "pkg", "b.py"), "w") as f:
        f.write("B = 2\n")
    with open(os.path.join(ws, "main.py"), "w") as f:
        f.write("print('changed')\n")
    os.unlink(os.path.join(ws, "pkg", "a.py"))
    res = snap.snapshot(message="step_end", source="step_end")
    kinds = {c["path"]: c["kind"] for c in res.changes}
    assert kinds == {"pkg/b.py": "add", "main.py": "modify", "pkg/a.py": "delete"}
    assert res.counts == (1, 1, 1) and res.workspace_changed is True


def test_a_mission_state_only_change_is_marked(tmp_path):
    ws = _ws(tmp_path)
    snap = _snap(ws)
    snap.snapshot(message="a", source="run_start")
    (tmp_path / ".agent" / "mission.json").write_text('{"id": "m1", "goals": [1]}')
    res = snap.snapshot(message="b", source="step_end")
    assert [c["path"] for c in res.changes] == [".agent/mission.json"]
    assert res.workspace_changed is False


def test_the_stat_cache_skips_unchanged_files(tmp_path):
    ws = _ws(tmp_path)
    snap = _snap(ws)
    first = snap.snapshot(message="a", source="run_start")
    assert first.bytes_hashed > 0
    (tmp_path / "pkg" / "c.py").write_text("C = 3\n")
    second = snap.snapshot(message="b", source="step_end")
    assert second.bytes_hashed == len("C = 3\n"), "only the new file was read"
    assert second.files_scanned == first.files_scanned + 1


def test_big_and_binary_files_are_stubs(tmp_path):
    ws = _ws(tmp_path)
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4" + b"\x00" * 500)
    (tmp_path / "blob.dat").write_bytes(b"z" * 2048)
    snap = _snap(ws, max_blob_bytes=1024)
    res = snap.snapshot(message="a", source="run_start")
    stubbed = {s["path"] for s in res.large_files}
    assert stubbed == {"paper.pdf", "blob.dat"}
    entries = {p: sha for p, _, sha in snap.tree_entries(res.commit_sha)}
    stub = json.loads(snap.read_blob(entries["paper.pdf"]))
    assert (
        stub["ouro_large_file"] is True
        and stub["size"] == 508
        and len(stub["sha256"]) == 64
    )


def test_symlinks_round_trip(tmp_path):
    ws = _ws(tmp_path)
    os.symlink("main.py", os.path.join(ws, "link.py"))
    snap = _snap(ws)
    res = snap.snapshot(message="a", source="run_start")
    modes = {p: m for p, m, _ in snap.tree_entries(res.commit_sha)}
    assert modes["link.py"] == 0o120000
    os.unlink(os.path.join(ws, "link.py"))
    snap.snapshot(message="b", source="step_end")
    snap.restore(res.commit_sha)
    assert (
        os.path.islink(os.path.join(ws, "link.py"))
        and os.readlink(os.path.join(ws, "link.py")) == "main.py"
    )


def test_budget_degrade_pauses_per_write_scans_only(tmp_path):
    ws = _ws(tmp_path)
    snap = _snap(ws, budget_ms=0.0)  # every scan is "slow"
    snap.snapshot(message="a", source="run_start")
    (tmp_path / "x.py").write_text("1")
    snap.snapshot(message="b", source="step_end")  # second slow scan → degraded
    assert snap.degraded
    (tmp_path / "y.py").write_text("2")
    assert snap.snapshot(message="c", source="effects_write") is None
    assert snap.dirty is True
    res = snap.snapshot(message="d", source="step_end")  # boundaries still scan
    assert res is not None and {c["path"] for c in res.changes} == {"y.py"}
    assert snap.dirty is False


def test_extra_excludes_prune_project_specific_trees(tmp_path):
    ws = _ws(tmp_path)
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "huge.py").write_text("g")
    snap = _snap(ws, extra_excludes=("generated",))
    res = snap.snapshot(message="a", source="run_start")
    assert not any(
        p.startswith("generated/") for p, _, _ in snap.tree_entries(res.commit_sha)
    )


def test_restore_puts_bytes_back_and_removes_later_files(tmp_path):
    ws = _ws(tmp_path)
    snap = _snap(ws)
    first = snap.snapshot(message="a", source="run_start")
    (tmp_path / "main.py").write_text("print('v2')\n")
    (tmp_path / "pkg" / "new.py").write_text("N\n")
    (tmp_path / ".agent" / "mission.json").write_text('{"id": "m1", "goals": [1, 2]}')
    snap.snapshot(message="b", source="step_end")
    report = snap.restore(first.commit_sha, dry_run=True)
    assert set(report.restored) == {
        "main.py",
        ".agent/mission.json",
    } and report.deleted == ["pkg/new.py"]
    assert (tmp_path / "pkg" / "new.py").exists(), "dry run changes nothing"
    report = snap.restore(first.commit_sha)
    assert (tmp_path / "main.py").read_text() == "print('hi')\n"
    assert not (tmp_path / "pkg" / "new.py").exists()
    assert json.loads((tmp_path / ".agent" / "mission.json").read_text()) == {
        "id": "m1",
        "goals": [],
    }
    assert (
        tmp_path / ".venv" / "lib" / "big.py"
    ).exists(), "excluded dirs are never touched"
    assert (tmp_path / ".git" / "HEAD").exists()
    # the restored tree IS the first commit's tree
    again = snap.snapshot(message="c", source="step_end")
    assert again is not None and again.tree_sha == first.tree_sha


def test_restore_can_keep_mission_state(tmp_path):
    ws = _ws(tmp_path)
    snap = _snap(ws)
    first = snap.snapshot(message="a", source="run_start")
    (tmp_path / ".agent" / "mission.json").write_text('{"id": "m1", "goals": [9]}')
    (tmp_path / "main.py").write_text("v2")
    snap.snapshot(message="b", source="step_end")
    snap.restore(first.commit_sha, keep_agent_state=True)
    assert (tmp_path / "main.py").read_text() == "print('hi')\n"
    assert (
        tmp_path / ".agent" / "mission.json"
    ).read_text() == '{"id": "m1", "goals": [9]}'


# ── through the effects ───────────────────────────────────────────────


def _effects(tmp_path) -> LocalEffects:
    _ws(tmp_path)
    return LocalEffects(str(tmp_path))


def test_the_store_gets_a_run_start_commit_and_one_commit_per_write(tmp_path):
    eff = _effects(tmp_path)
    _run(
        eff.emit_trace(CycleStart(mission_id="m1"))
    )  # opens the store, run_start snapshot
    with step_context("m1", 0, "build", "write"):
        _run(eff.write_file("pkg/one.py", "1\n"))
        _run(eff.write_file("pkg/two.py", "2\n"))
        _run(eff.write_file("pkg/two.py", "2\n"))  # byte-identical: no commit
    _run(eff.history_close())
    commits = reader.load_commits(str(tmp_path / ".agent"))
    assert [c["source"] for c in commits] == [
        "run_start",
        "effects_write",
        "effects_write",
    ]
    assert [c["trigger"] for c in commits[1:]] == ["pkg/one.py", "pkg/two.py"]
    assert json.loads(commits[1]["changes_json"])[0]["path"] == "pkg/one.py"
    assert commits[1]["flow"] == "build" and commits[1]["step"] == "write"
    (run,) = reader.list_runs(str(tmp_path / ".agent"))
    assert run["head_sha"] == commits[-1]["commit_sha"] and run["commits"] == 3


def test_the_run_start_commit_names_the_event_that_opened_the_store(tmp_path):
    """It was checkpointed with no ctx, so the first commit of every run had
    no cycle, flow or step (tier_20260924-191710 commit abcbac4e2b4f)."""
    eff = _effects(tmp_path)
    _run(eff.emit_trace(CycleStart(mission_id="m1", cycle=3, flow="mission_control")))
    _run(eff.history_close())
    (first,) = reader.load_commits(str(tmp_path / ".agent"))
    assert first["source"] == "run_start"
    assert first["cycle"] == 3 and first["flow"] == "mission_control"


def test_a_turn_records_the_tree_it_saw(tmp_path):
    from agent.trace import InferenceCall

    eff = _effects(tmp_path)
    _run(eff.emit_trace(CycleStart(mission_id="m1")))
    head0 = eff.history.head()
    with step_context("m1", 0, "build", "draft"):
        _run(eff.emit_trace(InferenceCall(mission_id="m1", step="draft")))
        _run(eff.write_file("out.py", "x\n"))
        _run(eff.emit_trace(StepEnd(mission_id="m1", step="draft")))
    _run(eff.history_close())
    (turn,) = reader.load_turns(str(tmp_path / ".agent"))
    commits = reader.load_commits(str(tmp_path / ".agent"))
    assert turn["tree_before"] == head0 == commits[0]["commit_sha"]
    assert turn["tree_after"] == commits[1]["commit_sha"]


def test_a_step_end_checkpoint_catches_out_of_band_writes(tmp_path):
    eff = _effects(tmp_path)
    _run(eff.emit_trace(CycleStart(mission_id="m1")))
    with open(tmp_path / "shell_made.txt", "w") as f:  # no effects call
        f.write("from a PTY\n")
    _run(
        eff.history_checkpoint(
            "step_end",
            "run",
            {"mission_id": "m1", "cycle": 0, "flow": "interact", "step": "run"},
        )
    )
    _run(eff.history_close())
    commits = reader.load_commits(str(tmp_path / ".agent"))
    assert commits[-1]["source"] == "step_end" and commits[-1]["step"] == "run"
    assert json.loads(commits[-1]["changes_json"]) == [
        {
            "path": "shell_made.txt",
            "kind": "add",
            "old_sha": "",
            "new_sha": commits[-1]
            and json.loads(commits[-1]["changes_json"])[0]["new_sha"],
            "old_mode": None,
            "new_mode": 0o100644,
        }
    ]


def test_snapshots_can_be_turned_off(tmp_path):
    _ws(tmp_path)
    eff = LocalEffects(str(tmp_path), history_snapshot=False)
    _run(eff.emit_trace(CycleStart(mission_id="m1")))
    _run(eff.write_file("a.py", "1"))
    _run(eff.history_close())
    assert reader.load_commits(str(tmp_path / ".agent")) == []
    assert not os.path.isdir(tmp_path / ".agent" / "history" / "repo.git")


@pytest.mark.asyncio
async def test_a_snapshot_failure_never_fails_the_write(tmp_path):
    eff = _effects(tmp_path)
    await eff.emit_trace(CycleStart(mission_id="m1"))

    def boom(**_kw):
        raise RuntimeError("disk full")

    eff._history_snapshotter.snapshot = boom  # type: ignore[method-assign]
    res = await eff.write_file("ok.py", "fine")
    assert res.success and (tmp_path / "ok.py").read_text() == "fine"
    await eff.history_close()
