#!/usr/bin/env python3
"""Acquire PDFs for bot-walled open-access papers from the PMC open-data bucket.

WHY THIS ROUTE EXISTS. 4,398 catalogued papers are open access on paper but
refused at the publisher ([[oa-pdf-acquisition-is-bot-walled]]: only ~31-34 %
of "verified OA" links download). Semantic Scholar's `papers` dataset — already
mirrored on the oa-mirrors drive — carries a PubMed Central id for a slice of
them, and AWS's `pmc-oa-opendata` bucket serves THE PUBLISHER'S OWN PDF
anonymously over HTTPS: no bot-wall, no credentials, free egress. Measured
2026-09-15: about 9 % of the bot-walled pool has such an id and three quarters
of those ids are ones our records do not already hold.

WHY IT BOOKS RATHER THAN EXTRACTS. The PDF is the asset the pipeline is built
on (a human-reviewable library plus PaddleOCR's accuracy on this domain), so
this tool stops at acquisition: it writes `pdfs/<key>.pdf` and flips the record
to `access_status=oa_pdf` + `pdf_path` exactly as the recovery lane does, which
is the whole of `_extraction_pending`. OCR, curation and packing then run
unchanged. (The `ocr` lane must be enabled for that queue to drain.)

Writes nothing until --apply. Run it with the mission PAUSED: the recover lane
works the same oa_unresolved pool, and a lane holding a stale copy of a record
would overwrite the pdf_path we just booked.

    .venv/bin/python tools/pmc_acquire.py --ids <s2_ids.jsonl>            # plan only
    .venv/bin/python tools/pmc_acquire.py --ids <s2_ids.jsonl> --apply
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
# Terminal extraction states: a paper there does not want another PDF.
SKIP_EXTRACTION = {
    "extracted",
    "extract_unverified",
    "extract_off_topic",
    "curate_oversize",
}


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


def candidates(bank: dict, ids: dict[str, str], base: str) -> list[dict]:
    """Merged records that want a PDF and have a PMC id."""
    out = []
    for key, rec in bank.items():
        doi = str(rec.get("doi") or "").strip().lower()
        pmc = ids.get(doi)
        if not pmc:
            continue
        if rec.get("review_status") in ("accepted", "denied"):
            continue
        if rec.get("extraction_status") in SKIP_EXTRACTION:
            continue
        path = rec.get("pdf_path") or ""
        if path and os.path.exists(
            path if os.path.isabs(path) else os.path.join(base, path)
        ):
            continue
        out.append({"key": key, "rec": rec, "pmc": pmc})
    out.sort(key=lambda c: c["key"])
    return out


async def pdf_key_for(effects, pmc: str) -> tuple[str, str]:
    """(object key, error) for the newest version's PDF of one article."""
    res = await effects.http_request(
        "GET",
        f"{BUCKET}/?list-type=2&prefix={pmc}.",
        headers={"User-Agent": UA},
        timeout=60.0,
    )
    if getattr(res, "status", 0) != 200 or not getattr(res, "text", ""):
        return "", f"list http {getattr(res, 'status', 0)}"
    try:
        root = ET.fromstring(res.text)
    except ET.ParseError as exc:
        return "", f"list parse: {exc}"
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    keys = [k.text or "" for k in root.findall(".//s3:Contents/s3:Key", ns)]
    pdfs = [k for k in keys if k.lower().endswith(".pdf")]
    if not pdfs:
        return "", "no pdf object" if keys else "article not in the open-access bucket"

    def version(k: str) -> int:
        m = re.search(r"\.(\d+)/", k)
        return int(m.group(1)) if m else 0

    return max(pdfs, key=version), ""


async def fetch_one(
    effects, cand: dict, base: str, apply: bool, sem: asyncio.Semaphore
) -> dict:
    key, pmc = cand["key"], cand["pmc"]
    async with sem:
        obj, err = await pdf_key_for(effects, pmc)
        if err:
            return {"key": key, "pmc": pmc, "ok": False, "reason": err}
        url = f"{BUCKET}/{obj}"
        if not apply:
            return {"key": key, "pmc": pmc, "ok": True, "url": url, "planned": True}
        dest = f"pdfs/{key}.pdf"
        dl = await effects.http_download(
            url, dest, headers={"User-Agent": UA}, timeout=180.0
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
        return {
            "key": key,
            "pmc": pmc,
            "ok": True,
            "url": url,
            "path": dest,
            "bytes": size,
        }


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
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    from agent.actions.scholarly_actions import append_records, read_databank
    from agent.effects.local import LocalEffects

    base = os.path.realpath(os.path.expanduser(args.working_dir))
    effects = LocalEffects(working_directory=base)
    ids = load_ids(args.ids)
    bank = await read_databank(effects)
    cands = candidates(bank, ids, base)
    if args.limit:
        cands = cands[: args.limit]
    print(
        f"{len(ids):,} DOI->PMC ids | {len(bank):,} papers in the databank | {len(cands):,} candidates"
    )
    if not cands:
        return 0

    t0 = time.time()
    sem = asyncio.Semaphore(max(1, args.concurrency))
    results = await asyncio.gather(
        *(fetch_one(effects, c, base, args.apply, sem) for c in cands)
    )
    ok = [r for r in results if r["ok"]]
    print(
        f"[{time.time()-t0:.0f}s] {len(ok)}/{len(results)} PDFs {'fetched' if args.apply else 'available'}"
    )
    reasons: dict[str, int] = {}
    for r in results:
        if not r["ok"]:
            reasons[r["reason"].split("(")[0].strip()] = (
                reasons.get(r["reason"].split("(")[0].strip(), 0) + 1
            )
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
            f"booked {len(rows)} papers as oa_pdf ({mb:.0f} MB); they now satisfy _extraction_pending"
        )
    if args.out:
        json.dump(results, open(os.path.expanduser(args.out), "w"), indent=1)
        print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
