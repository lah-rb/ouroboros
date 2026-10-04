"""``ouroboros.py history …`` — read and operate on a mission's history store.

    history ls [--runs|--turns|--commits] [--run R] [--limit N]
    history show <turn> [--thinking]
    history diff <turn|commit>
    history rollback --to <turn> [--to-commit SHA] [--dry-run] [--keep-mission-state]
    history replay <turn> [--model M] [--n K] [--flatten-session]
    history compact [--run R]
    history gc
    history import-traces [PATHS…]

A turn is named by its ``turn_id``, by ``<run_id>:<seq>``, or by a bare
``<seq>`` in the latest run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from agent.history import reader
from agent.history.store import HistoryLocked, HistoryStore, cli_lock


def _working_dir(args: argparse.Namespace) -> str:
    return os.path.realpath(getattr(args, "working_dir", None) or ".")


def _agent_dir(args: argparse.Namespace) -> str:
    return os.path.join(_working_dir(args), ".agent")


def _short(s: Any, n: int) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


# ── ls ────────────────────────────────────────────────────────────────


def _ls(args: argparse.Namespace) -> None:
    agent_dir = _agent_dir(args)
    if not reader.has_history(agent_dir):
        print("No history at .agent/history/")
        return
    limit = int(getattr(args, "limit", None) or 50)
    run = getattr(args, "run", None)
    if getattr(args, "runs", False):
        for r in reader.list_runs(agent_dir):
            print(
                f"{r['run_id']:32s} {str(r.get('started_at') or '')[:19]:19s} "
                f"{(r.get('final_status') or 'running'):10s} "
                f"turns={r.get('turns') or 0:<5} commits={r.get('commits') or 0:<5} "
                f"cycles={r.get('cycles') or 0}"
            )
        return
    if getattr(args, "commits", False):
        rows = reader.load_commits(agent_dir, run)
        for c in rows[-limit:]:
            print(
                f"{c['run_id']}:{c.get('seq'):<6} {c['commit_sha'][:10]} "
                f"{(c.get('source') or ''):14s} cyc={c.get('cycle')} "
                f"{c.get('flow') or ''}/{c.get('step') or ''} "
                f"+{c.get('files_added') or 0} ~{c.get('files_modified') or 0} "
                f"-{c.get('files_deleted') or 0} {_short(c.get('trigger'), 40)}"
            )
        return
    turns = reader.load_turns(agent_dir, run, with_content=False)
    for t in turns[-limit:]:
        h = t["_history"]
        print(
            f"{h['run_id']}:{h['seq']:<6} {t['turn_id']} cyc={t.get('cycle')} "
            f"{t.get('flow') or ''}/{t.get('step') or ''} "
            f"{(t.get('purpose') or ''):18s} in={t.get('tokens_in') or 0:<6} "
            f"out={t.get('tokens_out') or 0:<6} {(t.get('wall_ms') or 0)/1000:.1f}s "
            f"{t.get('end_reason') or ''}"
            + (" DEGENERATE" if t.get("degenerate") else "")
        )


# ── show ──────────────────────────────────────────────────────────────


def _show(args: argparse.Namespace) -> None:
    agent_dir = _agent_dir(args)
    t = reader.get_turn(agent_dir, args.turn)
    if t is None:
        print(f"No turn {args.turn!r}")
        sys.exit(1)
    h = t["_history"]
    skip = {
        "prompt_content",
        "prompt_static",
        "prompt_dynamic",
        "response_content",
        "thinking_content",
        "_history",
    }
    print(f"turn {t['turn_id']}  ({h['run_id']}:{h['seq']})")
    for k in sorted(t):
        if k in skip:
            continue
        print(f"  {k}: {t[k]}")
    if t.get("content_dropped"):
        print("\n[content dropped: this run recorded metrics only]")
        return
    print("\n── PROMPT ──")
    print(t.get("prompt_content") or "")
    if getattr(args, "thinking", False) and t.get("thinking_content"):
        print("\n── THINKING ──")
        print(t["thinking_content"])
    print("\n── RESPONSE ──")
    print(t.get("response_content") or "")


# ── diff ──────────────────────────────────────────────────────────────


def _print_commit(c: dict, verbose: bool = True) -> None:
    import json as _json

    print(
        f"commit {c['commit_sha']}  ({c['run_id']}:{c.get('seq')})  "
        f"source={c.get('source')} cyc={c.get('cycle')} "
        f"{c.get('flow') or ''}/{c.get('step') or ''}"
        + (f"  trigger={c.get('trigger')}" if c.get("trigger") else "")
    )
    print(
        f"  +{c.get('files_added') or 0} ~{c.get('files_modified') or 0} "
        f"-{c.get('files_deleted') or 0}  scanned={c.get('files_scanned')} "
        f"hashed={c.get('bytes_hashed')}B in {c.get('scan_ms')}ms"
        + ("" if c.get("workspace_changed", True) else "  [mission state only]")
    )
    if verbose:
        for ch in _json.loads(c.get("changes_json") or "[]"):
            mark = {"add": "+", "modify": "~", "delete": "-"}.get(ch.get("kind"), "?")
            print(f"    {mark} {ch.get('path')}")


def _diff(args: argparse.Namespace) -> None:
    """What changed: for a commit, its file list; for a turn, every commit
    between its tree_before and tree_after (the turn's own footprint)."""
    agent_dir = _agent_dir(args)
    ref = str(args.ref)
    commits = reader.load_commits(agent_dir)
    by_sha = {c["commit_sha"]: c for c in commits}
    hit = [c for c in commits if c["commit_sha"].startswith(ref)]
    if len(hit) == 1:
        _print_commit(hit[0])
        return
    t = reader.get_turn(agent_dir, ref)
    if t is None:
        print(f"No turn or commit {ref!r}")
        sys.exit(1)
    h = t["_history"]
    print(
        f"turn {t['turn_id']}  ({h['run_id']}:{h['seq']})  {t.get('flow')}/{t.get('step')}"
    )
    print(
        f"  tree_before={t.get('tree_before') or '(none)'}  tree_after={t.get('tree_after') or '(none)'}"
    )
    after = t.get("tree_after") or ""
    span = [
        c
        for c in commits
        if c["run_id"] == h["run_id"]
        and (c.get("seq") or 0) > (h["seq"] or 0)
        and ((c.get("seq") or 0) <= (by_sha.get(after, {}).get("seq") or 10**12))
    ]
    if not span:
        print("  no workspace change recorded after this turn")
    for c in span:
        _print_commit(c)


# ── rollback ──────────────────────────────────────────────────────────


def _rollback(args: argparse.Namespace) -> None:
    from agent.history.rollback import format_report, rollback

    try:
        report = rollback(
            _working_dir(args),
            to=getattr(args, "to", None),
            to_commit=getattr(args, "to_commit", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            keep_mission_state=bool(getattr(args, "keep_mission_state", False)),
        )
    except (HistoryLocked, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(format_report(report))


# ── replay ────────────────────────────────────────────────────────────


def _replay(args: argparse.Namespace) -> None:
    from agent.history.replay import format_results, replay

    try:
        results = replay(
            _working_dir(args),
            args.turn,
            model=getattr(args, "model", None),
            temperature=getattr(args, "temperature", None),
            max_tokens=getattr(args, "max_tokens", None),
            endpoint=getattr(args, "endpoint", None),
            n=int(getattr(args, "n", 1) or 1),
            flatten_session=bool(getattr(args, "flatten_session", False)),
        )
    except (HistoryLocked, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(format_results(results))


# ── gc ────────────────────────────────────────────────────────────────


def _gc(args: argparse.Namespace) -> None:
    """Pack the object store's loose blobs (every file version is one)."""
    from agent.history.snapshot import WorkspaceSnapshotter
    from agent.history.store import REPO_DIR, history_dir

    wd = _working_dir(args)
    repo = os.path.join(history_dir(wd), REPO_DIR)
    if not os.path.isdir(repo):
        print("No repo.git under .agent/history/")
        return
    try:
        with cli_lock(wd):
            before = sum(
                len(files) for _, _, files in os.walk(os.path.join(repo, "objects"))
            )
            WorkspaceSnapshotter(wd, repo).pack()
            after = sum(
                len(files) for _, _, files in os.walk(os.path.join(repo, "objects"))
            )
    except HistoryLocked as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"packed: {before} object files → {after}")


# ── compact ───────────────────────────────────────────────────────────


def _compact(args: argparse.Namespace) -> None:
    wd = _working_dir(args)
    agent_dir = _agent_dir(args)
    runs = reader.list_runs(agent_dir)
    run = getattr(args, "run", None)
    if run:
        runs = [r for r in runs if r["run_id"] == run]
    try:
        with cli_lock(wd):
            for r in runs:
                store = HistoryStore(
                    wd,
                    r.get("mission_id") or "",
                    "full",
                    run_id=r["run_id"],
                    lock=False,
                    write_runs_row=False,
                )
                asyncio.new_event_loop().run_until_complete(store.compact("run"))
                print(f"compacted {r['run_id']}")
    except HistoryLocked as e:
        print(f"Error: {e}")
        sys.exit(1)


# ── import-traces ─────────────────────────────────────────────────────


def _import_traces(args: argparse.Namespace) -> None:
    from agent.history.importer import import_traces

    try:
        reports = import_traces(
            _working_dir(args), list(getattr(args, "paths", None) or [])
        )
    except HistoryLocked as e:
        print(f"Error: {e}")
        sys.exit(1)
    if not reports:
        print("No .agent/traces/*.jsonl to import")
        return
    for r in reports:
        if r["skipped"]:
            print(f"skip   {r['run_id']} (already imported)  {r['path']}")
        else:
            print(
                f"import {r['run_id']} turns={r['turns']} events={r['events']}  {r['path']}"
            )


# ── parser ────────────────────────────────────────────────────────────


def add_history_parser(subparsers: Any) -> argparse.ArgumentParser:
    p = subparsers.add_parser("history", help="The mission's per-turn history store")
    sub = p.add_subparsers(dest="history_command")

    ls = sub.add_parser("ls", help="List turns (default), runs or commits")
    g = ls.add_mutually_exclusive_group()
    g.add_argument(
        "--turns", action="store_true", help="One line per inference turn (default)"
    )
    g.add_argument("--runs", action="store_true", help="One line per process run")
    g.add_argument(
        "--commits", action="store_true", help="One line per workspace change"
    )
    ls.add_argument("--run", help="Restrict to one run id")
    ls.add_argument(
        "--limit", type=int, default=50, help="Show the last N (default 50)"
    )
    ls.add_argument("--working-dir", help="Working directory (default: cwd)")

    show = sub.add_parser("show", help="Print one turn: fields, prompt, response")
    show.add_argument(
        "turn", help="turn_id, <run_id>:<seq>, or <seq> in the latest run"
    )
    show.add_argument("--thinking", action="store_true", help="Also print the thinking")
    show.add_argument("--working-dir", help="Working directory (default: cwd)")

    diff = sub.add_parser("diff", help="Files a turn (or one commit) changed")
    diff.add_argument(
        "ref", help="turn (turn_id | run:seq | seq) or a commit sha prefix"
    )
    diff.add_argument("--working-dir", help="Working directory (default: cwd)")

    rb = sub.add_parser(
        "rollback", help="Restore the workspace (and mission state) to a turn's tree"
    )
    rb.add_argument(
        "--to", help="turn (turn_id | run:seq | seq): restore its tree_before"
    )
    rb.add_argument(
        "--to-commit",
        dest="to_commit",
        help="a recorded commit sha (prefix ok) instead of a turn",
    )
    rb.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Report what would change; touch nothing",
    )
    rb.add_argument(
        "--keep-mission-state",
        dest="keep_mission_state",
        action="store_true",
        help="Leave .agent/mission.json as it is (restore code only)",
    )
    rb.add_argument("--working-dir", help="Working directory (default: cwd)")

    rp = sub.add_parser(
        "replay",
        help="Re-issue a recorded turn's prompt to a model; record and compare",
    )
    rp.add_argument("turn", help="turn_id, <run_id>:<seq>, or <seq> in the latest run")
    rp.add_argument(
        "--model", help="Model override (default: the turn's, else the server's)"
    )
    rp.add_argument(
        "--temperature", type=float, help="Override the recorded temperature"
    )
    rp.add_argument(
        "--max-tokens", dest="max_tokens", type=int, help="Override the recorded budget"
    )
    rp.add_argument("--endpoint", help="LLMVP GraphQL endpoint (default: the turn's)")
    rp.add_argument(
        "--n", type=int, default=1, help="Replay this many times (default 1)"
    )
    rp.add_argument(
        "--flatten-session",
        dest="flatten_session",
        action="store_true",
        help="Session turn: rebuild a stateless prompt from the session's prior turns",
    )
    rp.add_argument("--working-dir", help="Working directory (default: cwd)")

    gc = sub.add_parser("gc", help="Pack the snapshot store's loose objects")
    gc.add_argument("--working-dir", help="Working directory (default: cwd)")

    comp = sub.add_parser(
        "compact", help="Fold a run's part files into one (after a crash)"
    )
    comp.add_argument("--run", help="Only this run id")
    comp.add_argument("--working-dir", help="Working directory (default: cwd)")

    imp = sub.add_parser(
        "import-traces", help="Load retired .agent/traces/*.jsonl into the store"
    )
    imp.add_argument(
        "paths", nargs="*", help="JSONL files (default: every one under .agent/traces/)"
    )
    imp.add_argument("--working-dir", help="Working directory (default: cwd)")
    return p


def cmd_history(args: argparse.Namespace) -> None:
    handlers = {
        "ls": _ls,
        "show": _show,
        "diff": _diff,
        "rollback": _rollback,
        "replay": _replay,
        "gc": _gc,
        "compact": _compact,
        "import-traces": _import_traces,
    }
    handler = handlers.get(getattr(args, "history_command", None) or "")
    if handler is None:
        print("usage: ouroboros.py history {ls,show,compact,import-traces} …")
        sys.exit(2)
    handler(args)
