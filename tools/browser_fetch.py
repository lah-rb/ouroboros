#!/usr/bin/env python3
"""Fetch MDPI article PDFs with a real, windowed browser -- the operator's clicks, automated.

WHY (2026-10-01). 236 MDPI papers sat without a PDF: their records hold the
right `mdpi.com/<issn>/<vol>/<issue>/<id>/pdf` URL, but MDPI's Akamai edge
answers every non-browser client with 403 (curl with or without a browser user
agent) and headless Chromium with "Access Denied", while the operator's own
browser downloads them with one click. robots.txt neither allows nor disallows
the /pdf paths, and the operator ruled a respectful full-browser crawler fine.

WHAT IT DOES, per item of an intake fetch list (`intake.py export --doi-prefix
10.3390/`): open the article page in a WINDOWED Chromium on the operator's
desktop (persistent profile of its own), pause as a reader would, then have the
page fetch() its own "Download PDF" link -- the same browser, cookies and
request as the click, minus Chromium's download machinery, which segfaulted
under Playwright -- save the bytes into a bundle folder, and check it is a PDF.
`intake.py ingest --list <list> --bundle <folder>` then verifies each file's
first page against its record and books it, exactly as for the operator's
manual downloads.

RESPECTFUL BY CONSTRUCTION (operator ruling 2026-10-01): one paper per 20-40 s
(uniform jitter), at most 200 PDFs per local day, a stop after 3 consecutive
failures (a 403 / "Access Denied" is never retried harder), and the browser is
the stock automation-driven Chromium -- no stealth patches, no fingerprint
spoofing, no challenge solving. Failures keep a screenshot and the page HTML
under manual_intake/browser_fetch_failures/ so a later LLM fallback can be
designed from real cases.

    tools/browser_fetch/.venv/bin/python tools/browser_fetch.py \\
        --list dev/manual_intake/lists/fetch_<stamp>_n<N>.md [--start 1] [--max 200]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import json
import os
import random
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORPUS = Path(os.path.expanduser("~/corpora/ouroboros-spectra"))
LOG = CORPUS / "manual_intake" / "browser_fetch.jsonl"
FAIL_DIR = CORPUS / "manual_intake" / "browser_fetch_failures"
PROFILE = Path(os.path.expanduser("~/.cache/ouroboros/browser_fetch_profile"))
CHROMIUM = os.environ.get("OUROBOROS_CHROMIUM", "/usr/lib/chromium/chromium")
BUNDLE = Path(os.path.expanduser("~/Downloads/paper_bundle_mdpi"))

MIN_DELAY_S, MAX_DELAY_S = 20.0, 40.0
DAILY_CAP = 200
MAX_CONSECUTIVE_FAILURES = 3
READ_PAUSE_S = (2.0, 5.0)  # on the article page before the click, like a reader


# ── list + records ────────────────────────────────────────────────────


def parse_list(path: Path) -> list[tuple[int, str]]:
    """(position, paper_key) in list order, from an intake fetch list."""
    items, pos = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\d+)\. ", line)
        if m:
            pos = int(m.group(1))
            continue
        k = re.search(r"`([^`]+)`\s*$", line)
        if pos is not None and k:
            items.append((pos, k.group(1)))
            pos = None
    return items


def last_rows(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("paper_key"):
                out[r["paper_key"]] = r
    return out


def landing_url(rec: dict) -> str:
    """The article page: the record's MDPI pdf URL minus `/pdf...`, else the DOI."""
    for u in [rec.get("oa_pdf_url") or "", *(rec.get("oa_pdf_urls") or [])]:
        m = re.match(r"(https?://www\.mdpi\.com/[^?#]+?)/pdf\b", str(u))
        if m:
            return m.group(1)
    doi = str(rec.get("doi") or "").strip()
    return f"https://doi.org/{doi}" if doi else ""


# ── log + caps ────────────────────────────────────────────────────────


def read_log() -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def append_log(entry: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def fetched_today(log: list[dict]) -> int:
    today = dt.date.today().isoformat()  # the operator's local day
    return sum(
        1 for e in log if e.get("outcome") == "ok" and str(e.get("local_date")) == today
    )


# ── browser ───────────────────────────────────────────────────────────


def _prepare_profile(bundle: Path) -> None:
    """PDFs download instead of opening in the viewer, into the bundle, no prompt."""
    prefs_path = PROFILE / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs = {}
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text())
        except ValueError:
            prefs = {}
    prefs.setdefault("plugins", {})["always_open_pdf_externally"] = True
    dl = prefs.setdefault("download", {})
    dl["prompt_for_download"] = False
    dl["default_directory"] = str(bundle)
    prefs.setdefault("profile", {})["exit_type"] = "Normal"
    prefs_path.write_text(json.dumps(prefs))


async def _record_failure(page, key: str, why: str) -> str:
    FAIL_DIR.mkdir(parents=True, exist_ok=True)
    stem = FAIL_DIR / f"{key}_{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        await page.screenshot(path=f"{stem}.png", full_page=False)
        (stem.with_suffix(".html")).write_text(await page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001 -- evidence is best-effort
        pass
    return str(stem)


# fetch() a same-origin URL from the rendered page; base64 the bytes for transfer.
_FETCH_JS = """async (href) => {
  const r = await fetch(href, {credentials: 'include'});
  const buf = new Uint8Array(await r.arrayBuffer());
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  }
  return {status: r.status, len: buf.length, b64: btoa(bin),
          disposition: r.headers.get('content-disposition') || ''};
}"""


async def fetch_one(page, key: str, url: str, bundle: Path) -> dict:
    t0 = time.monotonic()
    out = {"key": key, "url": url}
    resp = None
    for attempt in range(2):
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            break
        except Exception as e:  # noqa: BLE001
            # ERR_NETWORK_CHANGED is THIS host's interfaces changing (docker
            # bridges come and go), never the site: one patient retry. Any
            # other navigation error is recorded as it is.
            if attempt == 0 and "ERR_NETWORK_CHANGED" in str(e):
                await page.wait_for_timeout(5_000)
                continue
            return {**out, "outcome": "page_error", "why": str(e)[:200]}
    await page.wait_for_timeout(int(random.uniform(*READ_PAUSE_S) * 1000))
    title = await page.title()
    status = resp.status if resp else 0
    if status in (401, 403, 429) or "access denied" in title.lower():
        return {**out, "outcome": "blocked", "why": f"HTTP {status} / {title[:80]}",
                "evidence": await _record_failure(page, key, "blocked")}  # fmt: skip
    path = re.sub(r"^https?://[^/]+", "", page.url).split("?")[0].rstrip("/")
    link = page.locator(f'a[href="{path}/pdf"]')
    if await link.count() == 0:
        link = page.locator('a[href$="/pdf"]')
    if await link.count() == 0:
        return {**out, "outcome": "no_pdf_link", "page": page.url,
                "evidence": await _record_failure(page, key, "no link")}  # fmt: skip
    # THE PAGE FETCHES ITS OWN PDF LINK; NOTHING IS "DOWNLOADED". Clicking the
    # link hands the file to Chromium's download machinery, and Chromium 153
    # driven by Playwright segfaulted there on most articles (2026-10-01). A
    # fetch() from the rendered article page goes through the same browser
    # network stack and cookies as the click -- MDPI answers it with the PDF
    # itself (Content-Disposition: attachment) -- and the bytes come back to
    # this process instead of to a download.
    href = (await link.first.get_attribute("href")) or f"{path}/pdf"
    try:
        got = await page.evaluate(_FETCH_JS, href)
    except Exception as e:  # noqa: BLE001
        return {**out, "outcome": "download_failed", "why": str(e)[:200],
                "evidence": await _record_failure(page, key, "fetch failed")}  # fmt: skip
    if got.get("status") != 200:
        return {**out, "outcome": "blocked" if got.get("status") in (401, 403, 429) else "download_failed",
                "why": f"HTTP {got.get('status')} for {href}"}  # fmt: skip
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', got.get("disposition") or "")
    name = Path(m.group(1)).name if m else f"{key}.pdf"
    dest = bundle / name
    if dest.exists():
        dest = bundle / f"{key}__{name}"
    dest.write_bytes(base64.b64decode(got["b64"]))
    head = dest.read_bytes()[:5] if dest.exists() else b""
    size = dest.stat().st_size if dest.exists() else 0
    if head != b"%PDF-" or size < 10_000:
        bad = dest.with_suffix(dest.suffix + ".notpdf")
        dest.rename(bad)
        return {**out, "outcome": "not_a_pdf", "file": str(bad), "bytes": size}
    return {**out, "outcome": "ok", "file": str(dest), "bytes": size, "source": href,
            "seconds": round(time.monotonic() - t0, 1)}  # fmt: skip


async def run(args) -> int:
    from playwright.async_api import async_playwright

    list_path = Path(args.list)
    items = [(p, k) for p, k in parse_list(list_path) if p >= args.start]
    papers = last_rows(CORPUS / "databank" / "papers.jsonl")
    log = read_log()
    done = {e["key"] for e in log if e.get("outcome") == "ok"}
    bundle = Path(args.bundle).expanduser()
    bundle.mkdir(parents=True, exist_ok=True)
    plan = []
    for pos, key in items:
        rec = papers.get(key) or {}
        if key in done or rec.get("pdf_path"):
            continue
        url = landing_url(rec)
        if url:
            plan.append((pos, key, url))
    budget = min(args.max, args.daily_cap - fetched_today(log))
    print(f"{len(plan)} to fetch from {list_path.name}; today's budget {budget} "
          f"(cap {args.daily_cap}, {fetched_today(log)} already today)", flush=True)  # fmt: skip
    if args.dry_run:
        for pos, key, url in plan[: max(0, budget)]:
            print(f"  #{pos:<4} {key:<45} {url}")
        return 0
    if budget <= 0:
        print("daily cap reached -- nothing to do today")
        return 0

    _prepare_profile(bundle)
    ok = fails = streak = 0
    last_pos = 0
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE),
            executable_path=CHROMIUM,
            headless=False,
            # Nothing here downloads: a stray download is cancelled rather than
            # handed to the machinery that segfaulted.
            accept_downloads=False,
            no_viewport=True,
            args=["--window-size=1280,900"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            for i, (pos, key, url) in enumerate(plan):
                if ok >= budget:
                    print(f"run limit reached ({budget} PDFs)", flush=True)
                    break
                if i:
                    await asyncio.sleep(random.uniform(args.min_delay, args.max_delay))
                try:
                    res = await fetch_one(page, key, url, bundle)
                except Exception as e:  # noqa: BLE001 -- the browser itself went away
                    append_log({"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "local_date": dt.date.today().isoformat(), "list": str(list_path),
                                "position": pos, "key": key, "url": url,
                                "outcome": "browser_error", "why": str(e)[:200]})  # fmt: skip
                    print(
                        f"#{pos:<4} {key:<45} browser_error -- stopping: {str(e)[:120]}",
                        flush=True,
                    )
                    last_pos = pos
                    break
                last_pos = pos
                entry = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                         "local_date": dt.date.today().isoformat(), "list": str(list_path),
                         "position": pos, **res}  # fmt: skip
                append_log(entry)
                if res["outcome"] == "ok":
                    ok += 1
                    streak = 0
                else:
                    fails += 1
                    streak += 1
                print(f"#{pos:<4} {key:<45} {res['outcome']:<11} "
                      f"{Path(res.get('file', '')).name or res.get('why', '')}", flush=True)  # fmt: skip
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"{streak} failures in a row -- stopping (not retrying harder)",
                        flush=True,
                    )
                    break
        finally:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001 -- already gone
                pass
    rel = (
        list_path.relative_to(REPO)
        if list_path.is_absolute() and REPO in list_path.parents
        else list_path
    )
    print(f"\nfetched {ok}, failed {fails}; last list position tried #{last_pos}")
    print(f"ingest: .venv/bin/python dev/manual_intake/intake.py ingest --list {rel} "
          f"--bundle {bundle} --through {last_pos}")  # fmt: skip
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--list",
        required=True,
        help="an intake fetch list (export --doi-prefix 10.3390/)",
    )
    ap.add_argument("--bundle", default=str(BUNDLE))
    ap.add_argument("--start", type=int, default=1, help="first list position to try")
    ap.add_argument(
        "--max", type=int, default=DAILY_CAP, help="at most this many PDFs this run"
    )
    ap.add_argument("--daily-cap", type=int, default=DAILY_CAP)
    ap.add_argument("--min-delay", type=float, default=MIN_DELAY_S)
    ap.add_argument("--max-delay", type=float, default=MAX_DELAY_S)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.min_delay < MIN_DELAY_S or args.daily_cap > DAILY_CAP:
        sys.exit(
            f"pace below {MIN_DELAY_S:.0f} s or cap above {DAILY_CAP}/day needs the operator's ruling"
        )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
