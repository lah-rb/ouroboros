#!/usr/bin/env python3
"""Mirror the OpenAlex public snapshot (or a prefix of it) to a local disk.

WHY (2026-09-03). OpenAlex moved its API to a paid credit meter -- $0.10/day
free, ~100 searches -- and the mission's discovery traffic spends that by
midday, leaving identity resolution (title -> DOI) with nothing. The snapshot
is CC0 and public on s3://openalex, unsigned reads, no requester-pays: the
whole parquet tree is 784 GB (works 725, authors 53, the rest small), which
fits the 1 TB drive the operator attached. Measured from this machine:
30 MB/s per stream, so eight streams saturate a USB-3 HDD (~120 MB/s) and
the tree lands in about two hours.

WHAT IT DOES. Lists every object under --prefix, downloads what is missing or
whose size differs, eight at a time, each to a `.part` file renamed into
place on completion (a crash never leaves a truncated file masquerading as a
finished one). Re-running is the monthly refresh: the tree is partitioned by
updated_date, so a refresh touches only new partitions and the manifest.

    python dev/openalex_snapshot_sync.py --dest /media/lah-rb/openalex --dry-run
    python dev/openalex_snapshot_sync.py --dest /media/lah-rb/openalex
    python dev/openalex_snapshot_sync.py --dest ... --prefix data/parquet/works/
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore import UNSIGNED
from botocore.client import Config

BUCKET = "openalex"


def client():
    return boto3.client(
        "s3",
        config=Config(
            signature_version=UNSIGNED,
            max_pool_connections=32,
            retries={"max_attempts": 8},
        ),
        region_name="us-east-1",
    )


def list_objects(s3, prefix: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=prefix
    ):
        for o in page.get("Contents", []):
            out.append((o["Key"], int(o["Size"])))
    return out


def needs_fetch(dest: str, key: str, size: int) -> bool:
    p = os.path.join(dest, key)
    try:
        return os.path.getsize(p) != size
    except OSError:
        return True


class Progress:
    def __init__(self, total_files: int, total_bytes: int):
        self.total_files, self.total_bytes = total_files, total_bytes
        self.files = 0
        self.bytes = 0
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.last = 0.0

    def add(self, n: int) -> None:
        with self.lock:
            self.files += 1
            self.bytes += n
            now = time.time()
            if now - self.last >= 30 or self.files == self.total_files:
                self.last = now
                el = now - self.t0
                rate = self.bytes / 1e6 / max(el, 1e-6)
                left = (self.total_bytes - self.bytes) / 1e6 / max(rate, 1e-6)
                print(
                    f"  {self.files}/{self.total_files} files  {self.bytes/1e9:.1f}/{self.total_bytes/1e9:.1f} GB  "
                    f"{rate:.0f} MB/s  ~{left/3600:.1f} h left",
                    flush=True,
                )


def fetch(s3, dest: str, key: str, size: int, prog: Progress) -> tuple[str, bool, str]:
    final = os.path.join(dest, key)
    part = final + ".part"
    os.makedirs(os.path.dirname(final), exist_ok=True)
    try:
        s3.download_file(BUCKET, key, part)
        got = os.path.getsize(part)
        if got != size:
            os.remove(part)
            return key, False, f"size mismatch {got} != {size}"
        os.replace(part, final)
        prog.add(size)
        return key, True, ""
    except Exception as e:  # noqa: BLE001 -- one bad object must not stop the mirror
        try:
            os.remove(part)
        except OSError:
            pass
        return key, False, f"{type(e).__name__}: {str(e)[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True, help="mount point of the snapshot disk")
    ap.add_argument("--prefix", default="data/parquet/", help="bucket prefix to mirror")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.dry_run and not os.path.ismount(a.dest) and not os.path.isdir(a.dest):
        print(f"{a.dest} is not a directory/mount", file=sys.stderr)
        return 2
    s3 = client()
    print(f"listing s3://{BUCKET}/{a.prefix} ...", flush=True)
    objs = list_objects(s3, a.prefix)
    total = sum(s for _, s in objs)
    todo = [(k, s) for k, s in objs if a.dry_run or needs_fetch(a.dest, k, s)]
    tb = sum(s for _, s in todo)
    print(
        f"{len(objs)} objects, {total/1e9:.1f} GB in the bucket; {len(todo)} to fetch ({tb/1e9:.1f} GB)"
    )
    if a.dry_run:
        by = {}
        for k, s in objs:
            top = "/".join(k.split("/")[:3])
            by[top] = by.get(top, 0) + s
        for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:12]:
            print(f"   {v/1e9:7.1f} GB  {k}")
        return 0
    if a.dest and os.path.exists(a.dest):
        st = os.statvfs(a.dest)
        free = st.f_bavail * st.f_frsize
        print(f"free on {a.dest}: {free/1e9:.0f} GB")
        if free < tb * 1.02:
            print("NOT ENOUGH SPACE", file=sys.stderr)
            return 3

    prog = Progress(len(todo), tb)
    failed: list[tuple[str, str]] = []
    # biggest first: keeps the tail of the run from being one giant file
    todo.sort(key=lambda ks: -ks[1])
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(fetch, s3, a.dest, k, s, prog) for k, s in todo]
        for f in as_completed(futs):
            key, ok, err = f.result()
            if not ok:
                failed.append((key, err))
                print(f"  FAILED {key}: {err}", flush=True)
    el = time.time() - prog.t0
    print(
        f"\ndone: {prog.files} files, {prog.bytes/1e9:.1f} GB in {el/3600:.2f} h; failed {len(failed)}"
    )
    for k, e in failed[:20]:
        print(f"  {k}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
