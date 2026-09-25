"""Workspace tree snapshots in a bare dulwich object store.

The history store versions the WORKSPACE, not the write calls: PTY shells in
another process, formatters run by the validation env, ``rm -f``, model-
authored ``sh -c`` all change files without passing through ``write_file``.
So a snapshot is built straight from the filesystem — blob per file, tree from
the paths, commit on ``refs/heads/history`` — into ``.agent/history/repo.git``.
Nothing is placed in the workspace: no ``.git``, no index, no gitfile. A
brownfield repo keeps its own ``.git`` untouched (and excluded).

``.agent/mission.json`` and ``.agent/env.json`` ride in the tree at their real
paths, so a turn's commit carries the mission state as it stood, and a
rollback restores goals and notes with the code.

Cost control: a stat cache (size, mtime_ns, inode → sha) skips unchanged
files; files over ``max_blob_bytes`` are recorded as a JSON stub with their
sha256; and when two consecutive scans overrun the budget the snapshotter
DEGRADES — per-write checkpoints stop scanning and only mark the tree dirty,
while step/cycle boundaries still snapshot. The store records every scan's
``scan_ms`` so the cost is visible, never hidden.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from dulwich.diff_tree import tree_changes as _tree_changes
from dulwich.index import commit_tree
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit
from dulwich.repo import Repo

from agent.history.excludes import (
    AGENT_STATE_FILES,
    is_excluded_dir,
    is_excluded_file,
    is_stub_file,
)
from agent.history.store import SnapshotResult

logger = logging.getLogger(__name__)

HISTORY_REF = b"refs/heads/history"
AUTHOR = b"ouroboros <ouroboros@local>"
STATCACHE_FILE = "ouro-statcache.json"
LARGE_STUB_KEY = "ouro_large_file"
MODE_FILE = 0o100644
MODE_EXEC = 0o100755
MODE_LINK = 0o120000


@dataclass
class RestoreReport:
    target: str
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unchanged: int = 0
    dry_run: bool = False


def _is_large_stub(data: bytes) -> bool:
    return data.startswith(b'{"' + LARGE_STUB_KEY.encode())


class WorkspaceSnapshotter:
    """Builds and restores tree snapshots of one workspace."""

    def __init__(
        self,
        working_dir: str,
        repo_dir: str,
        *,
        extra_excludes: tuple[str, ...] | list[str] = (),
        max_blob_bytes: int = 8 * 2**20,
        budget_ms: float = 500.0,
    ) -> None:
        self.working_dir = os.path.realpath(working_dir)
        self.repo_dir = repo_dir
        self._extra_excludes = tuple(extra_excludes or ())
        self.max_blob_bytes = int(max_blob_bytes)
        self.budget_ms = float(budget_ms)
        self._repo: Optional[Repo] = None
        # rel path -> ([size, mtime_ns, ino], sha, mode)
        self._cache: dict[str, tuple[list, str, int]] = {}
        self._cache_loaded = False
        self._slow_scans = 0
        # Set when a per-write checkpoint was skipped under degrade; the next
        # boundary snapshot clears it.
        self.dirty = False

    # ── repo ──────────────────────────────────────────────────────────

    def _open(self) -> Repo:
        if self._repo is None:
            if os.path.isdir(os.path.join(self.repo_dir, "objects")):
                self._repo = Repo(self.repo_dir)
            else:
                os.makedirs(os.path.dirname(self.repo_dir), exist_ok=True)
                self._repo = Repo.init_bare(self.repo_dir, mkdir=True)
                try:
                    os.chmod(self.repo_dir, 0o700)
                except OSError:
                    pass
            self._load_cache()
        return self._repo

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        path = os.path.join(self.repo_dir, STATCACHE_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self._cache = {
                k: (list(v[0]), str(v[1]), int(v[2])) for k, v in raw.items()
            }
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            self._cache = {}

    def close(self) -> None:
        """Persist the stat cache so a resumed run does not re-hash the tree."""
        if self._repo is None:
            return
        path = os.path.join(self.repo_dir, STATCACHE_FILE)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f)
            os.replace(tmp, path)
        except OSError:
            logger.debug("statcache write skipped", exc_info=True)

    def _ref(self) -> Optional[bytes]:
        repo = self._open()
        try:
            return repo.refs[HISTORY_REF]
        except KeyError:
            return None

    def head(self) -> str:
        sha = self._ref()
        return sha.decode() if sha else ""

    def _head_tree(self) -> Optional[bytes]:
        sha = self._ref()
        return self._open()[sha].tree if sha else None

    @property
    def degraded(self) -> bool:
        """Two consecutive over-budget scans: per-write checkpoints pause."""
        return self._slow_scans >= 2

    # ── walking ───────────────────────────────────────────────────────

    def _excluded_dir(self, name: str) -> bool:
        return is_excluded_dir(name) or any(
            fnmatch.fnmatchcase(name, pat) for pat in self._extra_excludes
        )

    def _walk(self) -> Iterator[tuple[str, os.DirEntry]]:
        """(relative path, entry) for every file the tree carries, in a
        deterministic order; excluded directories are never entered."""
        stack = [self.working_dir]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not self._excluded_dir(e.name):
                            stack.append(e.path)
                        continue
                except OSError:
                    continue
                if is_excluded_file(e.name):
                    continue
                yield os.path.relpath(e.path, self.working_dir), e

    def _blob_for(
        self, rel: str, path: str, st: os.stat_result, repo: Repo
    ) -> Optional[tuple[str, int, int, Optional[dict]]]:
        """(sha, mode, bytes_hashed, large_stub) for one path, via the cache."""
        if stat.S_ISLNK(st.st_mode):
            key = [st.st_size, st.st_mtime_ns, st.st_ino]
            cached = self._cache.get(rel)
            if cached and cached[0] == key:
                return cached[1], cached[2], 0, None
            target = os.readlink(path).encode("utf-8", "surrogateescape")
            blob = Blob.from_string(target)
            if blob.id not in repo.object_store:
                repo.object_store.add_object(blob)
            self._cache[rel] = (key, blob.id.decode(), MODE_LINK)
            return blob.id.decode(), MODE_LINK, len(target), None
        if not stat.S_ISREG(st.st_mode):
            return None  # sockets, fifos, devices
        key = [st.st_size, st.st_mtime_ns, st.st_ino]
        cached = self._cache.get(rel)
        if cached and cached[0] == key:
            return cached[1], cached[2], 0, None
        mode = MODE_EXEC if (st.st_mode & 0o111) else MODE_FILE
        large: Optional[dict] = None
        if st.st_size > self.max_blob_bytes or is_stub_file(os.path.basename(rel)):
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            large = {"path": rel, "size": st.st_size, "sha256": h.hexdigest()}
            data = json.dumps({LARGE_STUB_KEY: True, **large}).encode("utf-8")
        else:
            with open(path, "rb") as f:
                data = f.read()
        blob = Blob.from_string(data)
        if blob.id not in repo.object_store:
            repo.object_store.add_object(blob)
        self._cache[rel] = (key, blob.id.decode(), mode)
        return blob.id.decode(), mode, st.st_size, large

    def _build_tree(self) -> tuple[bytes, dict]:
        """Hash the workspace into the object store; return (tree sha, stats)."""
        repo = self._open()
        t0 = time.monotonic()
        entries: list[tuple[bytes, bytes, int]] = []
        large: list[dict] = []
        scanned = hashed = 0
        seen: set[str] = set()
        for rel, e in self._walk():
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            res = self._blob_for(rel, e.path, st, repo)
            if res is None:
                continue
            sha, mode, nbytes, stub = res
            entries.append((rel.encode("utf-8", "surrogateescape"), sha.encode(), mode))
            seen.add(rel)
            scanned += 1
            hashed += nbytes
            if stub:
                large.append(stub)
        for rel in AGENT_STATE_FILES:
            path = os.path.join(self.working_dir, rel)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            res = self._blob_for(rel, path, st, repo)
            if res is None:
                continue
            sha, mode, nbytes, _ = res
            entries.append((rel.encode(), sha.encode(), mode))
            seen.add(rel)
            scanned += 1
            hashed += nbytes
        # Forget cache entries for files that vanished.
        for gone in [k for k in self._cache if k not in seen]:
            del self._cache[gone]
        tree = commit_tree(repo.object_store, entries)
        scan_ms = (time.monotonic() - t0) * 1000
        if scan_ms > self.budget_ms:
            self._slow_scans += 1
        else:
            self._slow_scans = 0
        return tree, {
            "files_scanned": scanned,
            "bytes_hashed": hashed,
            "scan_ms": round(scan_ms, 2),
            "large": large,
        }

    # ── snapshot ──────────────────────────────────────────────────────

    def snapshot(
        self, *, message: str, source: str = "", force: bool = False
    ) -> Optional[SnapshotResult]:
        """Commit the workspace as it is now. None when the tree is unchanged
        — or when a per-write checkpoint is skipped under degrade (the tree is
        then marked dirty for the next boundary snapshot). ``force`` commits
        an unchanged tree too: a MARKER (rollback records its starting point
        this way, so the pre_rollback → rollback pair is always visible)."""
        if source == "effects_write" and self.degraded:
            self.dirty = True
            return None
        repo = self._open()
        old_tree = self._head_tree()
        tree, stats = self._build_tree()
        if old_tree is not None and tree == old_tree and not force:
            self.dirty = False
            return None
        changes = self.tree_changes(old_tree, tree)
        head = self._ref()
        now = int(time.time())
        commit = Commit()
        commit.tree = tree
        commit.parents = [head] if head else []
        commit.author = commit.committer = AUTHOR
        commit.commit_time = commit.author_time = now
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = message.encode("utf-8", "replace")
        repo.object_store.add_object(commit)
        repo.refs[HISTORY_REF] = commit.id
        self.dirty = False
        state = set(AGENT_STATE_FILES)
        return SnapshotResult(
            commit_sha=commit.id.decode(),
            parent_sha=head.decode() if head else "",
            tree_sha=tree.decode(),
            changes=changes,
            large_files=stats["large"],
            files_scanned=stats["files_scanned"],
            bytes_hashed=stats["bytes_hashed"],
            scan_ms=stats["scan_ms"],
            workspace_changed=any(c["path"] not in state for c in changes),
        )

    # ── reading ───────────────────────────────────────────────────────

    def tree_changes(
        self, old_tree: Optional[bytes], new_tree: Optional[bytes]
    ) -> list[dict]:
        """[{path, kind, old_sha, new_sha, old_mode, new_mode}] between two
        tree shas (bytes or hex str; None = empty tree)."""
        repo = self._open()
        old = old_tree.encode() if isinstance(old_tree, str) else old_tree
        new = new_tree.encode() if isinstance(new_tree, str) else new_tree
        out: list[dict] = []
        for ch in _tree_changes(repo.object_store, old, new):
            o, n = ch.old, ch.new
            out.append(
                {
                    "path": ((n or o).path).decode("utf-8", "surrogateescape"),
                    "kind": str(ch.type),
                    "old_sha": o.sha.decode() if o and o.sha else "",
                    "new_sha": n.sha.decode() if n and n.sha else "",
                    "old_mode": o.mode if o else None,
                    "new_mode": n.mode if n else None,
                }
            )
        return out

    def commit_tree_sha(self, commit_sha: str) -> bytes:
        repo = self._open()
        return repo[commit_sha.encode()].tree

    def tree_entries(self, commit_sha: str) -> list[tuple[str, int, str]]:
        """(path, mode, blob sha) for every file in a commit's tree."""
        repo = self._open()
        tree = repo[commit_sha.encode()].tree
        return [
            (e.path.decode("utf-8", "surrogateescape"), e.mode, e.sha.decode())
            for e in iter_tree_contents(repo.object_store, tree)
        ]

    def read_blob(self, sha: str) -> bytes:
        repo = self._open()
        return repo[sha.encode()].data

    def pack(self) -> None:
        """Pack loose objects (``history gc``)."""
        repo = self._open()
        repo.object_store.pack_loose_objects()

    # ── restore ───────────────────────────────────────────────────────

    def _confined(self, rel: str) -> bool:
        """A tree path is writable only inside the workspace and never under
        an excluded directory (the tree never contains one, but a path is
        checked before anything is written)."""
        if not rel or os.path.isabs(rel) or ".." in rel.split("/"):
            return False
        parts = rel.split("/")
        if rel in AGENT_STATE_FILES:
            return True
        return not any(self._excluded_dir(p) for p in parts[:-1])

    def restore(
        self,
        target_commit_sha: str,
        *,
        dry_run: bool = False,
        keep_agent_state: bool = False,
    ) -> RestoreReport:
        """Make the workspace equal to a commit's tree: write the files that
        differ, delete the files the commit lacks, prune emptied directories.
        Large-file stubs are skipped and reported."""
        repo = self._open()
        report = RestoreReport(target=target_commit_sha, dry_run=dry_run)
        target_tree = repo[target_commit_sha.encode()].tree
        current_tree, _ = self._build_tree()
        for ch in self.tree_changes(current_tree, target_tree):
            rel = ch["path"]
            if keep_agent_state and rel in AGENT_STATE_FILES:
                report.skipped.append(rel)
                continue
            if not self._confined(rel):
                report.skipped.append(rel)
                continue
            path = os.path.join(self.working_dir, rel)
            if ch["kind"] == "delete":
                report.deleted.append(rel)
                if not dry_run:
                    try:
                        if os.path.islink(path) or os.path.exists(path):
                            os.unlink(path)
                    except OSError:
                        report.skipped.append(rel)
                        continue
                    self._prune_empty_dirs(os.path.dirname(path))
                    self._cache.pop(rel, None)
                continue
            data = repo[ch["new_sha"].encode()].data
            if _is_large_stub(data):
                report.skipped.append(rel)
                continue
            report.restored.append(rel)
            if dry_run:
                continue
            os.makedirs(os.path.dirname(path) or self.working_dir, exist_ok=True)
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
            if ch["new_mode"] == MODE_LINK:
                os.symlink(data.decode("utf-8", "surrogateescape"), path)
            else:
                tmp = f"{path}.{os.getpid()}.restore"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.chmod(tmp, 0o755 if ch["new_mode"] == MODE_EXEC else 0o644)
                os.replace(tmp, path)
            self._cache.pop(rel, None)
        report.unchanged = len(self.tree_entries(target_commit_sha)) - len(
            report.restored
        )
        return report

    def _prune_empty_dirs(self, d: str) -> None:
        while (
            d
            and os.path.realpath(d) != self.working_dir
            and d.startswith(self.working_dir)
        ):
            try:
                os.rmdir(d)
            except OSError:
                return
            d = os.path.dirname(d)


__all__ = ["HISTORY_REF", "RestoreReport", "WorkspaceSnapshotter"]
