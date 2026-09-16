#!/usr/bin/env python3
"""Fetch supplementary material from PUBLISHERS, for papers PMC does not carry.

WHY. The PMC open-data bucket gives supplements for the 13 % of the accepted
corpus that is PMC-indexed (tools/pmc_acquire.py --supplements). The rest sit
behind publisher sites, and in this domain the supplement is often where the
peak tables and raw spectra are. A 10-paper-per-publisher probe on 2026-09-15
measured which publishers are reachable at all: Nature 5/10, BMC 4/10,
Springer 2/10, AGU 1/10, and zero for Elsevier and IOP (their supplement links
are JavaScript-rendered) or behind hard 403/429 walls (ACS, RSC, Wiley, IUCr,
Hindawi, and even MDPI's nominally open /article/<doi>/s1).

TWO STRATEGIES, both read-only until --apply:
  landing   GET https://doi.org/<doi>, take supplement links out of the HTML
            (springer /esm/, wiley downloadSupplement, rsc suppdata, …) and
            download the first that yields a real document. LocalEffects
            .http_download refuses text/html, so a paywall page cannot pass.
  els_pii   Elsevier renders its links with JavaScript, but the landing URL
            carries the PII, and supplements live at a deterministic CDN path
            (ars.els-cdn.com/content/image/1-s2.0-<PII>-mmc<N>.<ext>).

POLITENESS. One request at a time per host with a delay between them; these
are publisher sites, not an open-data bucket.

    .venv/bin/python tools/publisher_supplements.py --publishers Nature,BMC   # plan
    .venv/bin/python tools/publisher_supplements.py --publishers all --apply
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) Ouroboros-corpus-builder/1.0 "
    "(academic corpus; polite, low rate)"
)
SUPP_DIR = "supplements"
HOST_DELAY_S = 1.5
MAX_LINKS_PER_PAPER = 4
REGISTRANTS = {
    "10.1038": "Nature",
    "10.1186": "BMC",
    "10.1007": "Springer",
    "10.1029": "AGU",
    "10.1016": "Elsevier",
    "10.1088": "IOP",
    "10.3390": "MDPI",
    "10.1002": "Wiley",
    "10.1021": "ACS",
    "10.1039": "RSC",
    "10.1107": "IUCr",
    "10.3389": "Frontiers",
    "10.1155": "Hindawi",
}
# Publishers a sampled probe showed any yield for; --publishers all uses these.
REACHABLE = ("Nature", "BMC", "Springer", "AGU", "Elsevier")
SUPP_LINK_RE = re.compile(
    r'href=["\']([^"\']*(?:suppdata|/esm/|downloadSupplement|supplementary|'
    r'supplement|mmc\d|_MOESM|/s1)[^"\']*)["\']',
    re.IGNORECASE,
)
# The article's OWN PDF. The `/s1` alternative above (MDPI's supplement page)
# also matches Springer and BMC DOI slugs — link.springer.com/content/pdf/
# 10.1007/s11214-021-00812-z.pdf — and 47 of the first 313 fetched
# "supplements" were the parent paper itself (measured 2026-09-16). An
# article PDF is never a supplement, whatever link text it hides behind.
ARTICLE_PDF_RE = re.compile(
    r"/content/pdf/|/counter/pdf/|/track/pdf/"
    r"|/articles/10\.\d{4,9}/[^/?#]+\.pdf(?:$|[?#])",
    re.IGNORECASE,
)
PII_RE = re.compile(r"/pii/(S[0-9X]+)", re.IGNORECASE)
DOC_EXT = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt", ".zip", ".cif")
# Media carries nothing this corpus reads, and a figure image is not a
# supplement — the PMC route filters both, so this one does too.
SKIP_EXT = (
    ".mp4",
    ".mov",
    ".mpg",
    ".mpeg",
    ".avi",
    ".wmv",
    ".mkv",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".tif",
    ".tiff",
    ".html",
    ".htm",
)


def publisher_of(doi: str) -> str:
    return REGISTRANTS.get(str(doi or "").split("/")[0], "")


def is_article_pdf(url: str) -> bool:
    """Is this the article's own PDF path rather than a supplement?"""
    return bool(ARTICLE_PDF_RE.search(url or ""))


def supplement_links(html: str, page_url: str) -> list[str]:
    """Absolute supplement URLs found in a landing page, in page order.

    Article-PDF paths are dropped here, at the source, so no later filter
    has to know the publishers' slug shapes."""
    out: list[str] = []
    for m in SUPP_LINK_RE.finditer(html or ""):
        href = m.group(1)
        if href.startswith("//"):
            href = "https:" + href
        url = href if href.startswith("http") else urljoin(page_url, href)
        if is_article_pdf(url):
            continue
        if url not in out:
            out.append(url)
    return out


def elsevier_urls(page_url: str, html: str) -> list[str]:
    """Deterministic Elsevier CDN supplement paths from the article's PII."""
    m = PII_RE.search(page_url) or PII_RE.search(html or "")
    if not m:
        return []
    pii = m.group(1).upper()
    base = f"https://ars.els-cdn.com/content/image/1-s2.0-{pii}-mmc"
    return [
        f"{base}{n}{ext}" for n in (1, 2) for ext in (".pdf", ".docx", ".xlsx", ".zip")
    ]


def safe_name(url: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1] or "supplement"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120]


class Hosts:
    """One in-flight request per host, with a delay between them."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def __call__(self, url: str):
        host = urlparse(url).netloc.lower()
        lock = self._locks.setdefault(host, asyncio.Lock())
        await lock.acquire()
        wait = HOST_DELAY_S - (time.time() - self._last.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        return host, lock

    def done(self, host: str, lock: asyncio.Lock) -> None:
        self._last[host] = time.time()
        lock.release()


async def fetch_for(
    effects, key: str, doi: str, pub: str, hosts: Hosts, apply: bool
) -> dict:
    res = {
        "key": key,
        "doi": doi,
        "pub": pub,
        "page": 0,
        "links": 0,
        "files": [],
        "why": "",
    }
    page_url = f"https://doi.org/{doi}"
    host, lock = await hosts(page_url)
    try:
        r = await effects.http_request(
            "GET", page_url, headers={"User-Agent": UA}, timeout=90.0
        )
    finally:
        hosts.done(host, lock)
    status = getattr(r, "status", 0)
    html = getattr(r, "text", "") or ""
    final_url = getattr(r, "url", page_url) or page_url
    if status != 200 or not html:
        res["why"] = f"landing page http {status}"
        return res
    res["page"] = 1
    urls = supplement_links(html, final_url)
    if pub == "Elsevier":
        urls = elsevier_urls(final_url, html) + urls
    urls = [u for u in urls if not u.lower().endswith(SKIP_EXT)][:MAX_LINKS_PER_PAPER]
    res["links"] = len(urls)
    if not urls:
        res["why"] = "no supplement links"
        return res
    if not apply:
        res["files"] = [{"name": safe_name(u), "url": u} for u in urls]
        return res
    for u in urls:
        host, lock = await hosts(u)
        try:
            dest = f"{SUPP_DIR}/{key}/{safe_name(u)}"
            dl = await effects.http_download(
                u, dest, headers={"User-Agent": UA}, timeout=180.0, max_bytes=60_000_000
            )
        finally:
            hosts.done(host, lock)
        if dl.success:
            res["files"].append(
                {"name": safe_name(u), "url": u, "bytes": dl.bytes_written}
            )
        else:
            res["why"] = f"download: {dl.error or dl.status}"
    if not res["files"] and not res["why"]:
        res["why"] = "no download succeeded"
    return res


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--working-dir", default=os.path.expanduser("~/corpora/ouroboros-spectra")
    )
    ap.add_argument(
        "--publishers", default="all", help="comma list, or 'all' for the reachable set"
    )
    ap.add_argument("--limit", type=int, default=0, help="cap papers per publisher")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    from agent.actions.scholarly_actions import append_records, read_databank
    from agent.effects.local import LocalEffects

    base = os.path.realpath(os.path.expanduser(args.working_dir))
    effects = LocalEffects(working_directory=base)
    bank = await read_databank(effects)
    wanted = (
        list(REACHABLE)
        if args.publishers.strip().lower() == "all"
        else [p.strip() for p in args.publishers.split(",") if p.strip()]
    )
    todo = []
    for key, rec in bank.items():
        if rec.get("review_status") != "accepted" or rec.get("supplements"):
            continue
        pub = publisher_of(rec.get("doi"))
        if pub in wanted:
            todo.append((key, str(rec["doi"]), pub))
    todo.sort(key=lambda t: (t[2], t[0]))
    if args.limit:
        capped: list = []
        seen: collections.Counter = collections.Counter()
        for k, d, pub in todo:
            if seen[pub] < args.limit:
                capped.append((k, d, pub))
                seen[pub] += 1
        todo = capped
    print(
        f"{len(todo)} accepted papers without supplements across {sorted(set(t[2] for t in todo))}"
    )
    if not todo:
        return 0

    hosts = Hosts()
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def one(k, d, pub):
        async with sem:
            return await fetch_for(effects, k, d, pub, hosts, args.apply)

    t0 = time.time()
    results = await asyncio.gather(*(one(k, d, pub) for k, d, pub in todo))
    print(
        f"[{time.time()-t0:.0f}s] per publisher (papers | page ok | links | WITH FILES):"
    )
    for pub in sorted(set(t[2] for t in todo)):
        g = [r for r in results if r["pub"] == pub]
        got = [r for r in g if r["files"]]
        nf = sum(len(r["files"]) for r in got)
        why = collections.Counter(
            r["why"][:40] for r in g if not r["files"]
        ).most_common(2)
        print(
            f"  {pub:10} {len(g):4} | page {sum(r['page'] for r in g):4} | "
            f"links {sum(1 for r in g if r['links']):4} | FILES {len(got):4} ({nf} files) | {why}"
        )
    if args.apply:
        rows = []
        for r in results:
            if not r["files"]:
                continue
            rec = dict(bank[r["key"]])
            rec["supplements"] = (rec.get("supplements") or []) + r["files"]
            rec["supplement_source"] = "publisher"
            rows.append(rec)
        if rows:
            await append_records(effects, rows)
            mb = sum(f.get("bytes", 0) for r in results for f in r["files"]) / 1e6
            print(f"recorded supplements on {len(rows)} papers ({mb:.0f} MB)")
    if args.out:
        json.dump(results, open(os.path.expanduser(args.out), "w"), indent=1)
        print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
