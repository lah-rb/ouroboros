#!/usr/bin/env python3
"""Mirror Semantic Scholar Academic Graph datasets onto a local drive.

Stdlib only; runs under any Python 3.11+. Key from ~/.s2_key (never echoed).

    python3 tools/s2_mirror.py --release 2026-09-01 \
        --datasets papers,abstracts,paper-ids,authors,publication-venues,tldrs,citations,s2orc,s2orc_v2 \
        --dest /media/lah-rb/oa-mirrors/data/semantic-scholar --jobs 3 [--dry-run]

Why this shape:
- The Datasets API hands out PRE-SIGNED S3 URLs that expire (~30 min; S3 answers
  HTTP 400, not 403), and the API
  itself is limited to ~1 request/s per key. Files are therefore listed once
  per dataset, sizes are read with one-byte ranged GETs against S3 (no API
  cost), every dataset is re-listed when its URLs are older than 15 min, and a
  400/403 mid-run triggers a single throttled re-list rather than a retry storm.
- Resumable and idempotent: a file whose on-disk size equals the S3 size is
  skipped; partial files download into `<name>.part` with `curl -C -` and are
  renamed only on a size match, so a re-run after any interruption continues.
- Low concurrency by design: the target is an SMR (shingled) HDD, which
  handles a few sequential streams well and many interleaved writers badly.
- Layout `<dest>/<release>/<dataset>/<file>`; `manifest.jsonl` (one line per
  completed file: dataset, name, size, etag, finished_at) and README.md with
  the release id and the ODC-BY attribution the licence requires.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

API = "https://api.semanticscholar.org/datasets/v1/release"
KEY_FILE = os.path.expanduser("~/.s2_key")
API_INTERVAL_S = 1.2  # the key's datasets-endpoint budget is ~1 req/s
URL_TTL_S = (
    900  # pre-signed URLs died ~30 min after listing (HTTP 400); re-list well before
)

_api_lock = threading.Lock()
_api_last = 0.0


def _key() -> str:
    try:
        return open(KEY_FILE).read().strip()
    except OSError:
        sys.exit(f"no API key at {KEY_FILE}")


def api_get(path: str, key: str) -> dict:
    """Throttled, 429-aware GET against the Datasets API."""
    global _api_last
    url = f"{API}/{path}"
    for attempt in range(14):
        with _api_lock:
            wait = API_INTERVAL_S - (time.time() - _api_last)
            if wait > 0:
                time.sleep(wait)
            _api_last = time.time()
        try:
            req = urllib.request.Request(url, headers={"x-api-key": key})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                ra = e.headers.get("Retry-After") if e.headers else None
                delay = (
                    int(ra) if (ra and ra.isdigit()) else min(300, 15 * (attempt + 1))
                )
                time.sleep(delay)  # 14 attempts ≈ 30 min of patience before giving up
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(min(300, 15 * (attempt + 1)))
    raise RuntimeError(f"API unavailable (429/5xx) for {path} after ~30 min")


def s3_size(url: str) -> int:
    """Total object size from a one-byte ranged GET (pre-signed URLs are GET-only)."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                cr = r.headers.get("Content-Range") or ""
                m = re.search(r"/(\d+)$", cr)
                if m:
                    return int(m.group(1))
                return int(r.headers.get("Content-Length") or -1)
        except Exception:
            time.sleep(2 * (attempt + 1))
    return -1


def fname(url: str) -> str:
    return os.path.basename(urllib.parse.urlparse(url).path)


class Mirror:
    def __init__(self, release: str, dest: str, key: str, jobs: int, log):
        self.release, self.dest, self.key, self.jobs, self.log = (
            release,
            dest,
            key,
            jobs,
            log,
        )
        self.urls: dict[str, dict[str, str]] = {}  # dataset -> {fname: url}
        self.refresh_lock = threading.Lock()
        self.refreshed_at: dict[str, float] = {}
        self.manifest_lock = threading.Lock()
        self.done_bytes = 0
        self.t0 = time.time()

    def list_dataset(self, ds: str) -> dict[str, str]:
        d = api_get(f"{self.release}/dataset/{ds}", self.key)
        files = d.get("files") or []
        self.urls[ds] = {fname(u): u for u in files}
        self.refreshed_at[ds] = time.time()
        return self.urls[ds]

    def refresh(self, ds: str, force: bool = False) -> None:
        """Re-list one dataset (new pre-signed URLs), at most once per 60 s.

        Called from the TTL check before a download and from the 400/403 error
        path; concurrent callers for the same dataset collapse into one API call."""
        with self.refresh_lock:
            if time.time() - self.refreshed_at.get(ds, 0) < 60:
                return  # a sibling worker refreshed this dataset moments ago
            self.log(f"[{ds}] refreshing pre-signed URLs")
            self.list_dataset(ds)

    def manifest_add(self, rec: dict) -> None:
        with self.manifest_lock:
            with open(
                os.path.join(self.dest, self.release, "manifest.jsonl"), "a"
            ) as f:
                f.write(json.dumps(rec) + "\n")

    def download(self, ds: str, name: str, size: int) -> tuple[str, int]:
        ddir = os.path.join(self.dest, self.release, ds)
        os.makedirs(ddir, exist_ok=True)
        final = os.path.join(ddir, name)
        part = final + ".part"
        if os.path.exists(final) and os.path.getsize(final) == size:
            return "skip", 0
        hdr = part + ".hdr"
        for attempt in range(8):
            try:
                if time.time() - self.refreshed_at.get(ds, 0) > URL_TTL_S:
                    self.refresh(ds, force=True)
            except Exception as e:  # API outage: wait it out, the file is not lost
                self.log(f"[{ds}] URL refresh failed ({str(e)[:80]}); waiting 5 min")
                time.sleep(300)
                continue
            url = self.urls.get(ds, {}).get(name)
            if not url:
                self.log(f"[{ds}] {name} vanished from the release listing")
                return "fail", 0
            cmd = [
                "curl",
                "-sS",
                "-L",
                "--fail",
                "--retry",
                "3",
                "--retry-delay",
                "5",
                "--speed-limit",
                "20000",
                "--speed-time",
                "120",  # give up if <20 kB/s for 2 min
                "-C",
                "-",
                "-D",
                hdr,
                "-o",
                part,
                url,
            ]
            t = time.time()
            r = subprocess.run(cmd, capture_output=True, text=True)
            got = os.path.getsize(part) if os.path.exists(part) else 0
            if r.returncode == 0 and got == size:
                etag = ""
                try:
                    for line in open(hdr, errors="ignore"):
                        if line.lower().startswith("etag:"):
                            etag = line.split(":", 1)[1].strip().strip('"')
                except OSError:
                    pass
                os.replace(part, final)
                try:
                    os.remove(hdr)
                except OSError:
                    pass
                self.manifest_add(
                    {
                        "dataset": ds,
                        "name": name,
                        "size": size,
                        "etag": etag,
                        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                )
                return "ok", size
            err = (r.stderr or "").strip()[-160:]
            if any(c in err for c in ("400", "403", "416")) or "expired" in err.lower():
                if (
                    "416" in err
                ):  # range not satisfiable: local .part is stale/oversized
                    for p in (part, hdr):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                try:
                    self.refresh(ds)
                except Exception as e:
                    self.log(f"[{ds}] URL refresh failed ({str(e)[:80]})")
            self.log(
                f"[{ds}] {name} attempt {attempt+1} failed after {time.time()-t:.0f}s ({got:,}/{size:,} B): {err}"
            )
            time.sleep(min(60, 5 * (attempt + 1)))
        return "fail", 0

    def run(self, datasets: list[str], dry_run: bool) -> int:
        plan: list[tuple[str, str, int]] = []
        for ds in datasets:
            files = self.list_dataset(ds)
            with cf.ThreadPoolExecutor(12) as ex:
                sizes = list(ex.map(s3_size, files.values()))
            for (name, _), sz in zip(files.items(), sizes):
                plan.append((ds, name, sz))
            tot = sum(s for s in sizes if s > 0)
            have = sum(
                os.path.getsize(os.path.join(self.dest, self.release, ds, n))
                for n in files
                if os.path.exists(os.path.join(self.dest, self.release, ds, n))
            )
            self.log(
                f"[{ds}] {len(files)} files, {tot/1e9:.1f} GB, already complete on disk {have/1e9:.1f} GB"
                + (
                    f", {sum(1 for s in sizes if s<0)} unsized"
                    if any(s < 0 for s in sizes)
                    else ""
                )
            )
        total = sum(s for _, _, s in plan if s > 0)
        self.log(
            f"PLAN release {self.release}: {len(plan)} files, {total/1e9:.1f} GB -> {self.dest}/{self.release}"
        )
        if dry_run:
            return 0
        os.makedirs(os.path.join(self.dest, self.release), exist_ok=True)
        bad = [p for p in plan if p[2] <= 0]
        for ds, name, _ in bad:
            self.log(f"[{ds}] {name}: size unknown, will download without size check")
        results = {"ok": 0, "skip": 0, "fail": 0}
        failed: list[str] = []
        last_report = time.time()

        def work(item):
            ds, name, size = item
            try:
                if size <= 0:
                    size = s3_size(self.urls[ds][name])
                st, n = self.download(ds, name, size)
            except Exception as e:  # never let one file take the run down
                self.log(
                    f"[{ds}] {name} worker error: {type(e).__name__}: {str(e)[:120]}"
                )
                st, n = "fail", 0
            return item, st, n

        with cf.ThreadPoolExecutor(self.jobs) as ex:
            for item, st, n in ex.map(work, plan):
                results[st] += 1
                self.done_bytes += n
                if st == "fail":
                    failed.append(f"{item[0]}/{item[1]}")
                if time.time() - last_report > 300 or st == "fail":
                    el = time.time() - self.t0
                    self.log(
                        f"progress: {results['ok']+results['skip']}/{len(plan)} files, "
                        f"{self.done_bytes/1e9:.1f} GB new in {el/3600:.2f} h "
                        f"({self.done_bytes/1e6/max(1,el):.1f} MB/s), failed {results['fail']}"
                    )
                    last_report = time.time()
        el = time.time() - self.t0
        self.log(
            f"DONE: ok {results['ok']}, skipped {results['skip']}, failed {results['fail']}; "
            f"{self.done_bytes/1e9:.1f} GB in {el/3600:.2f} h ({self.done_bytes/1e6/max(1,el):.1f} MB/s)"
        )
        for f in failed:
            self.log(f"FAILED {f}")
        self.write_readme(datasets, plan)
        return 1 if failed else 0

    def write_readme(self, datasets: list[str], plan) -> None:
        by = {}
        for ds, _, s in plan:
            by.setdefault(ds, [0, 0])
            by[ds][0] += 1
            by[ds][1] += max(0, s)
        rows = "\n".join(
            f"| {ds} | {n} | {b/1e9:.1f} GB |" for ds, (n, b) in by.items()
        )
        text = f"""# Semantic Scholar Academic Graph — release {self.release}

Mirrored {time.strftime('%Y-%m-%d')} with `tools/s2_mirror.py` from the Semantic Scholar
Datasets API (https://api.semanticscholar.org/api-docs/datasets). Files are the
original gzip-compressed JSON Lines shards; `manifest.jsonl` lists every completed
file with its size and S3 ETag.

| dataset | files | size |
|---|---|---|
{rows}

## Licence and attribution

This collection is licensed under **ODC-BY 1.0** (https://opendatacommons.org/licenses/by/1.0/).
Attribution required by the Semantic Scholar API licence: data from **Semantic Scholar**,
Allen Institute for AI — cite *The Semantic Scholar Open Data Platform* (Kinney et al., 2023).
`s2orc` full text is parsed from open-access PDFs; per-paper `license`/`oaInfo` fields carry the
upstream licence and every third-party work remains under its own terms.

Not mirrored: `embeddings-specter_v1/v2` (~1 TB each; fetch vectors per candidate set via the API).
`s2orc_v2` is the announced replacement for `s2orc` but was 33 GB against 438 GB at this release,
so both are kept until v2 reaches parity.
"""
        open(os.path.join(self.dest, self.release, "README.md"), "w").write(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", required=True)
    ap.add_argument("--datasets", required=True, help="comma-separated dataset names")
    ap.add_argument("--dest", required=True)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    key = _key()

    def log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    rel = api_get(args.release, key)
    names = {d["name"] for d in rel["datasets"]}
    want = [d for d in args.datasets.split(",") if d]
    unknown = [d for d in want if d not in names]
    if unknown:
        sys.exit(
            f"unknown datasets for release {args.release}: {unknown}; available: {sorted(names)}"
        )
    return Mirror(args.release, args.dest, key, args.jobs, log).run(want, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
