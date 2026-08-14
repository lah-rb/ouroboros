#!/usr/bin/env python3
"""WHERE does the numeric recall go missing? Per-page attribution.

The gate reports one corpus-weighted number per paper. That number cannot
distinguish "this extraction is diffusely degraded" from "the reference list
was dropped and it is numerically dense". Those want opposite decisions, so
split the miss by page and by page KIND.

Runs in the tool venv (needs fitz + extract_batch's own oracle).
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, "/Users/lah-rb/Repos/ouroboros/tools/pdf_extract")

import fitz  # noqa: E402
from extract_batch import _NUM_RE, _norm, _prose_text  # noqa: E402

REF_HEAD = re.compile(r"^\s{0,3}#{0,4}\s*(references|bibliography|literature cited)\b",
                      re.I | re.M)
# A page is reference-like when citation furniture dominates it.
REF_MARKS = re.compile(r"\bet al\b|\bdoi\b|\bvol\b|\bpp\b|\bj\.\s|\bphys\b|\bchem\b", re.I)


def page_kind(truth: str, in_refs: bool) -> str:
    if in_refs:
        return "references"
    marks = len(REF_MARKS.findall(truth))
    words = max(1, len(truth.split()))
    if marks / words > 0.035:
        return "references"
    return "body"


def main() -> None:
    cases = json.loads(open(sys.argv[1]).read())
    out = []
    for c in cases:
        pdf_path, md_path = c["pdf"], c["md"]
        if not (os.path.isfile(pdf_path) and os.path.isfile(md_path)):
            continue
        md = open(md_path, errors="replace").read()
        pages_md = md.split("\n\n---\n\n")
        doc = fitz.open(pdf_path)
        aligned = len(pages_md) == doc.page_count
        rows = []
        in_refs = False
        for i, page in enumerate(doc):
            truth = _norm(_prose_text(page))
            if len(truth.strip()) < 200:
                continue
            pm = pages_md[i] if aligned else md
            pm_n = _norm(pm)
            pm_c = re.sub(r"\s", "", pm_n)
            nums = _NUM_RE.findall(truth)
            if not nums:
                continue
            hit = sum(1 for n in nums if n in pm_c or n in pm_n)
            raw = pages_md[i] if aligned else ""
            if REF_HEAD.search(raw) or REF_HEAD.search(_prose_text(page)):
                in_refs = True
            kind = page_kind(truth, in_refs)
            rows.append({
                "page": i + 1, "kind": kind, "nums": len(nums), "hit": hit,
                "pos": (i + 1) / doc.page_count,
                "md_chars": len(pm.strip()) if aligned else -1,
                "truth_chars": len(truth),
            })
        doc.close()
        tot = sum(r["nums"] for r in rows)
        hits = sum(r["hit"] for r in rows)
        miss_by_kind, nums_by_kind, hits_by_kind = {}, {}, {}
        for r in rows:
            k = r["kind"]
            miss_by_kind[k] = miss_by_kind.get(k, 0) + r["nums"] - r["hit"]
            nums_by_kind[k] = nums_by_kind.get(k, 0) + r["nums"]
            hits_by_kind[k] = hits_by_kind.get(k, 0) + r["hit"]
        # A page is DEAD when the VLM emitted almost nothing against real prose.
        dead = [r for r in rows if r["md_chars"] >= 0
                and r["md_chars"] < 0.15 * r["truth_chars"]]
        dead_miss = sum(r["nums"] - r["hit"] for r in dead)
        out.append({
            "case_id": c["case_id"], "aligned": aligned,
            "pages_scored": len(rows), "nums": tot, "hits": hits,
            "rate": round(hits / tot, 4) if tot else 1.0,
            "miss_total": tot - hits, "miss_by_kind": miss_by_kind,
            "nums_by_kind": nums_by_kind, "hits_by_kind": hits_by_kind,
            "ref_pos": sorted(round(r["pos"], 2) for r in rows
                              if r["kind"] == "references"),
            "dead_pages": len(dead), "dead_page_miss": dead_miss,
            "worst_pages": sorted(
                [{"p": r["page"], "k": r["kind"], "miss": r["nums"] - r["hit"],
                  "n": r["nums"], "mdc": r["md_chars"]} for r in rows],
                key=lambda d: -d["miss"])[:4],
        })
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
