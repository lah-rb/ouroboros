#!/usr/bin/env python3
"""Acquire article PDFs — and their SUPPLEMENTS — from the PMC open-data bucket.

WHY THIS ROUTE EXISTS. 4,398 catalogued papers are open access on paper but
refused at the publisher ([[oa-pdf-acquisition-is-bot-walled]]: only ~31-34 %
of "verified OA" links download). Semantic Scholar's `papers` dataset — already
mirrored on the oa-mirrors drive — carries a PubMed Central id for a slice of
them, and AWS's `pmc-oa-opendata` bucket serves THE PUBLISHER'S OWN files
anonymously over HTTPS: no bot-wall, no credentials, free egress.

WHY SUPPLEMENTS MATTER HERE (operator, 2026-09-15). In this domain the peak
tables, raw spectra and crystal structures often live in the supplementary
material rather than the article body. Measured over our PMC-indexed papers:
180 accepted papers carry 348 supplement files (590 MB excluding video),
including 11 CIFs and 29 spreadsheets; the bot-walled pool adds 109 more.

WHY IT BOOKS RATHER THAN EXTRACTS. The PDF is the asset the pipeline is built
on (a human-reviewable library plus PaddleOCR's accuracy on this domain), so
this tool stops at acquisition: `--apply` writes `pdfs/<key>.pdf` and flips the
record to `access_status=oa_pdf` + `pdf_path` exactly as the recovery lane
does, which is the whole of `_extraction_pending`. Supplements land in
`supplements/<key>/<name>` and are recorded on the paper; nothing downstream
reads them yet — attaching them to extraction/packing is a separate design.

Writes nothing until --apply. Run it with the mission PAUSED: the recover lane
works the same oa_unresolved pool, and a lane holding a stale copy of a record
would overwrite the pdf_path we just booked.

    .venv/bin/python tools/pmc_acquire.py --ids <s2_ids.jsonl>                 # plan
    .venv/bin/python tools/pmc_acquire.py --ids <s2_ids.jsonl> --apply
    .venv/bin/python tools/pmc_acquire.py --ids <s2_ids.jsonl> --supplements --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
UA = "Ouroboros-corpus-builder/1.0 (research corpus; AWS Open Data)"
MIN_PDF_BYTES = 10_000
SUPP_DIR = "supplements"
# Terminal extraction states: a paper there does not want another PDF.
SKIP_EXTRACTION = {
    "extracted",
    "extract_unverified",
    "extract_off_topic",
    "curate_oversize",
}
# The article's own renditions, and the figures we re-extract from the PDF.
_ARTICLE_RE = re.compile(r"PMC\d+\.\d+\.(pdf|txt|xml|json)$", re.IGNORECASE)
_FIGURE_RE = re.compile(r"\.(jpg|jpeg|png|gif|tif|tiff|webp)$", re.IGNORECASE)
# Video supplements are large and carry nothing this corpus reads.
VIDEO_EXT = {".mp4", ".mov", ".mpg", ".mpeg", ".avi", ".wmv", ".mkv"}


def load_ids(path: str) -> dict[str, str]:
    """doi -> PMC id, from the S2 scan output (JSON arrays or JSONL rows)."""
    out: dict[str, str] = {}
    for line in open(os.path.expanduser(path), errors="replace"):
        line = line.strip()
        if not line:
            continue
        rows = json.loads(line) if line.startswith("[") else [json.loads(line)]
        for r in rows:
            doi = str(r.get("doi") or "").strip().lower()
            pmc = str(r.get("pmc") or "").strip()
            if doi and pmc and pmc.lower() not in ("none", "null"):
                out[doi] = pmc if pmc.upper().startswith("PMC") else "PMC" + pmc
    return out


def pick_article_pdf(keys: list[str]) -> str:
    """The ARTICLE pdf among an article folder's objects, "" when there is none.

    A folder can hold supplements beside the article ("CHEM-31-e202501203-s001.pdf"
    at 372 kB next to PMC12188159.1.pdf at 2.9 MB; also mmc1.pdf,
    *_MOESM1_ESM.pdf, Data_Sheet_1.PDF). Alphabetical order picks the
    supplement, which books the WRONG DOCUMENT — 9 of the first 320 fetches did
    exactly that. The article is always named PMC<id>.<version>.pdf.
    """
    pdfs = [k for k in keys if k.lower().endswith(".pdf")]
    if not pdfs:
        return ""

    def rank(k: str) -> tuple[int, int]:
        name = k.rsplit("/", 1)[-1]
        m = re.fullmatch(r"PMC(\d+)\.(\d+)\.pdf", name, re.IGNORECASE)
        return (1 if m else 0, int(m.group(2)) if m else 0)

    best = max(pdfs, key=rank)
    return best if rank(best)[0] else ""


def pick_supplements(keys: list[str], *, skip_video: bool = True) -> list[str]:
    """Every object that is neither an article rendition nor a figure image.

    That is what "supplementary material" means in this bucket: SI documents
    (…-s001.pdf, mmc1.pdf, *_MOESM1_ESM.docx), data (xlsx, csv, txt, cif, zip)
    and media. Newest article version only — an older version's supplements are
    superseded.
    """
    if not keys:
        return []
    versions = [int(m.group(1)) for k in keys if (m := re.search(r"\.(\d+)/", k))]
    newest = max(versions) if versions else 0
    out = []
    for k in keys:
        m = re.search(r"\.(\d+)/", k)
        if m and int(m.group(1)) != newest:
            continue
        name = k.rsplit("/", 1)[-1]
        if _ARTICLE_RE.search(name) or _FIGURE_RE.search(name):
            continue
        if skip_video and os.path.splitext(name)[1].lower() in VIDEO_EXT:
            continue
        out.append(k)
    return sorted(out)


def candidates(
    bank: dict, ids: dict[str, str], base: str, supplements: bool = False
) -> list[dict]:
    """Merged records that want a fetch and have a PMC id."""
    out = []
    for key, rec in bank.items():
        doi = str(rec.get("doi") or "").strip().lower()
        pmc = ids.get(doi)
        if not pmc:
            continue
        if rec.get("review_status") == "denied":
            continue
        if supplements:
            # Supplements are worth having for any paper we keep: accepted
            # today, or unreviewed and still in the funnel. Already-fetched
            # papers are skipped by name below, in fetch_supplements.
            out.append({"key": key, "rec": rec, "pmc": pmc, "held": False})
            continue
        if rec.get("review_status") == "accepted":
            continue
        if rec.get("extraction_status") in SKIP_EXTRACTION:
            continue
        path = rec.get("pdf_path") or ""
        held = bool(path) and os.path.exists(
            path if os.path.isabs(path) else os.path.join(base, path)
        )
        # A PDF on disk whose record does not say oa_pdf is a LOST BOOKING —
        # the recover lane can overwrite one from a stale copy of the record.
        # Re-book it (no download) instead of skipping it forever.
        if held and rec.get("access_status") == "oa_pdf":
            continue
        out.append({"key": key, "rec": rec, "pmc": pmc, "held": held})
    out.sort(key=lambda c: c["key"])
    return out


async def list_objects(effects, pmc: str) -> tuple[list[str], str]:
    """(object keys, error) for one article folder."""
    res = await effects.http_request(
        "GET",
        f"{BUCKET}/?list-type=2&prefix={pmc}.",
        headers={"User-Agent": UA},
        timeout=60.0,
    )
    if getattr(res, "status", 0) != 200 or not getattr(res, "text", ""):
        return [], f"list http {getattr(res, 'status', 0)}"
    try:
        root = ET.fromstring(res.text)
    except ET.ParseError as exc:
        return [], f"list parse: {exc}"
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    return [k.text or "" for k in root.findall(".//s3:Contents/s3:Key", ns)], ""


async def fetch_one(
    effects,
    cand: dict,
    base: str,
    apply: bool,
    sem: asyncio.Semaphore,
    max_bytes: int = 50_000_000,
) -> dict:
    key, pmc = cand["key"], cand["pmc"]
    if cand.get("held"):
        return {
            "key": key,
            "pmc": pmc,
            "ok": True,
            "url": cand["rec"].get("oa_pdf_url") or f"{BUCKET}/{pmc}",
            "path": cand["rec"]["pdf_path"],
            "bytes": 0,
            "rebooked": True,
        }
    async with sem:
        keys, err = await list_objects(effects, pmc)
    if err:
        return {"key": key, "pmc": pmc, "ok": False, "reason": err}
    obj = pick_article_pdf(keys)
    if not obj:
        reason = (
            "no article pdf object (supplementary only)"
            if any(k.lower().endswith(".pdf") for k in keys)
            else "no pdf object" if keys else "article not in the open-access bucket"
        )
        return {"key": key, "pmc": pmc, "ok": False, "reason": reason}
    url = f"{BUCKET}/{obj}"
    if not apply:
        return {"key": key, "pmc": pmc, "ok": True, "url": url, "planned": True}
    dest = f"pdfs/{key}.pdf"
    async with sem:
        dl = await effects.http_download(
            url, dest, headers={"User-Agent": UA}, timeout=300.0, max_bytes=max_bytes
        )
    if not dl.success:
        return {
            "key": key,
            "pmc": pmc,
            "ok": False,
            "reason": f"download: {dl.error or dl.status}",
        }
    full = os.path.join(base, dest)
    size = os.path.getsize(full) if os.path.exists(full) else 0
    with open(full, "rb") as fh:
        magic = fh.read(5)
    if magic != b"%PDF-" or size < MIN_PDF_BYTES:
        os.remove(full)
        return {
            "key": key,
            "pmc": pmc,
            "ok": False,
            "reason": f"not a pdf ({magic!r}, {size} bytes)",
        }
    return {"key": key, "pmc": pmc, "ok": True, "url": url, "path": dest, "bytes": size}


def _safe_name(name: str) -> str:
    """A supplement's own filename, stripped of anything path-like."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name.rsplit("/", 1)[-1])[:120]


async def fetch_supplements(
    effects,
    cand: dict,
    base: str,
    apply: bool,
    sem: asyncio.Semaphore,
    max_bytes: int = 50_000_000,
    skip_video: bool = True,
) -> dict:
    key, pmc = cand["key"], cand["pmc"]
    async with sem:
        keys, err = await list_objects(effects, pmc)
    if err:
        return {"key": key, "pmc": pmc, "ok": False, "reason": err, "files": []}
    objs = pick_supplements(keys, skip_video=skip_video)
    if not objs:
        return {"key": key, "pmc": pmc, "ok": True, "files": [], "none": True}
    if not apply:
        return {
            "key": key,
            "pmc": pmc,
            "ok": True,
            "planned": True,
            "files": [{"name": _safe_name(o)} for o in objs],
        }
    got: list[dict] = []
    for obj in objs:
        name = _safe_name(obj)
        dest = f"{SUPP_DIR}/{key}/{name}"
        full = os.path.join(base, dest)
        if os.path.exists(full) and os.path.getsize(full) > 0:
            got.append(
                {"name": name, "bytes": os.path.getsize(full), "url": f"{BUCKET}/{obj}"}
            )
            continue
        url = f"{BUCKET}/{obj}"
        async with sem:
            dl = await effects.http_download(
                url,
                dest,
                headers={"User-Agent": UA},
                timeout=300.0,
                max_bytes=max_bytes,
            )
        if dl.success and os.path.exists(full) and os.path.getsize(full) > 0:
            got.append({"name": name, "bytes": os.path.getsize(full), "url": url})
    return {"key": key, "pmc": pmc, "ok": bool(got), "files": got}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True, help="S2 scan output: rows with doi + pmc")
    ap.add_argument(
        "--working-dir", default=os.path.expanduser("~/corpora/ouroboros-spectra")
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument(
        "--apply", action="store_true", help="download and book (default: plan only)"
    )
    ap.add_argument(
        "--supplements",
        action="store_true",
        help="fetch supplementary material instead of article PDFs",
    )
    ap.add_argument(
        "--with-video", action="store_true", help="include video supplements"
    )
    ap.add_argument(
        "--max-bytes",
        type=int,
        default=50_000_000,
        help="download cap; raise it for image-heavy papers (one XRF paper exceeds 50 MB)",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    from agent.actions.scholarly_actions import append_records, read_databank
    from agent.effects.local import LocalEffects

    base = os.path.realpath(os.path.expanduser(args.working_dir))
    effects = LocalEffects(working_directory=base)
    ids = load_ids(args.ids)
    bank = await read_databank(effects)
    cands = candidates(bank, ids, base, supplements=args.supplements)
    if args.limit:
        cands = cands[: args.limit]
    what = "supplements" if args.supplements else "article PDFs"
    print(
        f"{len(ids):,} DOI->PMC ids | {len(bank):,} papers | {len(cands):,} candidates for {what}"
    )
    if not cands:
        return 0

    t0 = time.time()
    sem = asyncio.Semaphore(max(1, args.concurrency))
    if args.supplements:
        results = await asyncio.gather(
            *(
                fetch_supplements(
                    effects,
                    c,
                    base,
                    args.apply,
                    sem,
                    args.max_bytes,
                    not args.with_video,
                )
                for c in cands
            )
        )
        withfiles = [r for r in results if r.get("files")]
        nfiles = sum(len(r["files"]) for r in withfiles)
        mb = sum(f.get("bytes", 0) for r in withfiles for f in r["files"]) / 1e6
        print(
            f"[{time.time()-t0:.0f}s] {len(withfiles)} papers have supplements, "
            f"{nfiles} files{'' if not args.apply else f', {mb:.0f} MB fetched'}"
        )
        if args.apply and withfiles:
            by_key = {c["key"]: c for c in cands}
            rows = []
            for r in withfiles:
                rec = dict(by_key[r["key"]]["rec"])
                rec["supplements"] = r["files"]
                rec["supplement_source"] = "pmc_oa_opendata"
                rows.append(rec)
            await append_records(effects, rows)
            print(
                f"recorded supplements on {len(rows)} papers (files under {SUPP_DIR}/<paper_key>/)"
            )
    else:
        results = await asyncio.gather(
            *(
                fetch_one(effects, c, base, args.apply, sem, args.max_bytes)
                for c in cands
            )
        )
        ok = [r for r in results if r["ok"]]
        print(
            f"[{time.time()-t0:.0f}s] {len(ok)}/{len(results)} PDFs {'fetched' if args.apply else 'available'}"
        )
        reasons: dict[str, int] = {}
        for r in results:
            if not r["ok"]:
                k = r["reason"].split("(")[0].strip()
                reasons[k] = reasons.get(k, 0) + 1
        if reasons:
            print("  misses:", dict(sorted(reasons.items(), key=lambda kv: -kv[1])))
        if args.apply and ok:
            by_key = {c["key"]: c for c in cands}
            rows = []
            for r in ok:
                rec = dict(by_key[r["key"]]["rec"])
                rec["pdf_path"] = r["path"]
                rec["oa_pdf_url"] = r["url"]
                rec["access_status"] = "oa_pdf"
                rec["status"] = "acquired"
                rec["failure_reason"] = ""
                rec["retrieval_method"] = "pmc_oa_opendata"
                rec["pmcid"] = r["pmc"]
                rec.setdefault("oa_attempted", []).append(r["url"])
                rows.append(rec)
            await append_records(effects, rows)
            mb = sum(r.get("bytes", 0) for r in ok) / 1e6
            print(
                f"booked {len(rows)} papers as oa_pdf ({mb:.0f} MB); they satisfy _extraction_pending"
            )
    if args.out:
        json.dump(results, open(os.path.expanduser(args.out), "w"), indent=1)
        print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
