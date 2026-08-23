#!/usr/bin/env python3
"""Repair figure-caption pairing in figtext artifacts from the markdown.

WHY THIS EXISTS (2026-08-22). 40% of figure records (2,928/7,409) carried
empty captions. The captions were never missing — paddle extracts them
faithfully as centered text divs — but the positional image↔caption
pairing breaks on MULTI-PANEL figures: sub-panels extract as a run of
consecutive <img> divs with ONE caption div after the run, so only the
adjacent crop wins the caption and the rest get nothing (or a subpanel
letter). Crop file-ordering also disagrees with reading order, so even
single captions can bind to the wrong neighbor.

WHAT IT DOES. For each figtext artifact, re-reads the paper's markdown,
groups <img> tags into runs, collects the caption-shaped text blocks
around each run (Figure/Fig./Figura/Abb/Рис/图 ...), and assigns:
  - 1:1 in order when the caption count matches the run length,
  - otherwise the run's nearest caption to EVERY empty-captioned member
    (a panel legitimately shares its figure's caption).
ADDITIVE ONLY: an existing caption >= 5 chars is never touched, so the
pass is idempotent and safe to re-run at any time.

WHEN TO RUN: as a pre-training step (see ouroboros-ops/PRETRAIN_PREP.md)
and after any large extraction wave. Usage:

    .venv/bin/python tools/figtext_caption_repair.py [--dry-run] \
        [--root ~/corpora/ouroboros-spectra]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

IMG_RE = re.compile(r'<img[^>]*?src="[^"]*?(fig_\d+\.png)"[^>]*>')
CAPTION_RE = re.compile(
    r"^\s*(?:\*+)?\s*(?:Figure|Fig\.?|Figura|Abbildung|Abb\.?|Рис(?:унок)?\.?|图|図)\s*\.?\s*\d+",
    re.IGNORECASE,
)
DIV_TEXT_RE = re.compile(r"<div[^>]*>(.*?)</div>", re.DOTALL)
NOT_CAPTION = ("[figure removed by extraction filter]",)


def tokens_of(md: str):
    """Position-sorted (pos, kind, payload) over the WHOLE document —
    divs spanning lines included (the line-based first version missed
    every wrapped caption)."""
    toks = []
    for m in IMG_RE.finditer(md):
        toks.append((m.start(), "img", m.group(1)))
    for m in re.finditer(r"<div[^>]*>(.*?)</div>", md, re.DOTALL):
        inner = m.group(1)
        if "<img" in inner:
            continue
        text = re.sub(r"<[^>]+>", "", inner)
        text = re.sub(r"\s+", " ", text).strip()
        if any(n in text for n in NOT_CAPTION):
            continue
        if CAPTION_RE.match(text) and len(text) >= 15:
            toks.append((m.start(), "cap", text))
    toks.sort()
    return toks


def caption_map(md: str) -> dict:
    """fig name -> caption via img-run grouping over positioned tokens."""
    toks = tokens_of(md)
    out: dict = {}
    i = 0
    while i < len(toks):
        if toks[i][1] != "img":
            i += 1
            continue
        run = [toks[i][2]]
        j = i + 1
        while j < len(toks) and toks[j][1] == "img":
            run.append(toks[j][2])
            j += 1
        caps = []
        k = j
        while k < len(toks) and toks[k][1] == "cap":
            caps.append(toks[k][2])
            k += 1
        if not caps and i > 0 and toks[i - 1][1] == "cap":
            caps = [toks[i - 1][2]]
        if caps:
            if len(caps) == len(run):
                for fig, cap in zip(run, caps):
                    out.setdefault(fig, cap)
            else:
                for fig in run:
                    out.setdefault(fig, caps[0])
        i = j
    return out


_CAPNUM_RE = re.compile(
    r"(?:Figure|Fig\.?|Figura|Abbildung|Abb\.?|Рис(?:унок)?\.?|图|図)\s*\.?\s*(\d+)",
    re.IGNORECASE,
)


def numbered_captions(md: str):
    """[(figure_number, caption_text)] in document order, deduped."""
    seen = {}
    for _, kind, text in tokens_of(md):
        if kind != "cap":
            continue
        m = _CAPNUM_RE.match(text)
        if m:
            seen.setdefault(int(m.group(1)), text)
    return sorted(seen.items())


def index_fallback(figs: list, md: str) -> dict:
    """fig name -> caption for crops with NO <img> anchor in the md.

    When every crop lacks an anchor the only signal left is ordering:
    fig_00..fig_NN were cropped in reading order, and the numbered
    captions run Figure 1..N in reading order. Aligned ONLY when the
    counts agree exactly — a partial alignment would confidently attach
    wrong captions, and a wrong caption is worse than an empty one.
    """
    caps = numbered_captions(md)
    names = sorted(f.get("fig", "") for f in figs)
    if not caps or len(caps) != len(names):
        return {}
    return {name: cap for name, (_, cap) in zip(names, caps)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/corpora/ouroboros-spectra"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root = args.root
    ftdir = os.path.join(root, "databank", "figtext")
    papers = {}
    for fn in ("databank/papers.jsonl", "databank/extraction.jsonl"):
        for line in open(os.path.join(root, fn), encoding="utf-8"):
            d = json.loads(line)
            papers.setdefault(d["paper_key"], {}).update(d)
    scanned = repaired_figs = repaired_files = still_empty = no_md = 0
    for f in sorted(os.listdir(ftdir)):
        if not f.endswith(".json"):
            continue
        path = os.path.join(ftdir, f)
        art = json.load(open(path, encoding="utf-8"))
        figs = art.get("figs") or []
        needs_repair = any(len(fig.get("caption") or "") < 5 for fig in figs)
        needs_sanitize = any(
            any(n in (fig.get("caption") or "") for n in NOT_CAPTION)
            or (fig.get("caption") or "").startswith(("---", "—"))
            for fig in figs
        )
        if not (needs_repair or needs_sanitize):
            continue
        scanned += 1
        changed_any = False
        rec = papers.get(art.get("paper_key", ""), {})
        md_rel = rec.get("md_path") or ""
        md_path = os.path.join(root, md_rel)
        if not os.path.isfile(md_path):
            no_md += 1
            continue
        # SANITIZE pre-existing captions first: the original pairing glued
        # extraction-filter placeholders and rule markers onto some caption
        # fronts; stripping junk is not "overwriting a caption".
        for fig in figs:
            cap = fig.get("caption") or ""
            clean = cap
            for n in NOT_CAPTION:
                clean = clean.replace(f"*{n}*", " ").replace(n, " ")
            clean = re.sub(r"^[\s*\-—]+", "", re.sub(r"\s+", " ", clean)).strip()
            if clean != cap:
                fig["caption"] = clean
                fig["caption_sanitized"] = True
                changed_any = True

        md_text = open(md_path, encoding="utf-8", errors="replace").read()
        cmap = caption_map(md_text)
        fallback = None
        changed = changed_any
        for fig in figs:
            if len(fig.get("caption") or "") >= 5:
                continue
            name = fig.get("fig", "")
            cap = cmap.get(name)
            how = "run-pairing"
            if not cap:
                if fallback is None:
                    fallback = index_fallback(figs, md_text)
                cap = fallback.get(name)
                how = "index-alignment"
            if cap:
                fig["caption"] = cap
                fig["caption_repaired"] = how
                repaired_figs += 1
                changed = True
            else:
                still_empty += 1
        if changed:
            repaired_files += 1
            if not args.dry_run:
                json.dump(
                    art, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1
                )
    print(
        f"{'DRY-RUN ' if args.dry_run else ''}artifacts with empty captions: {scanned} | "
        f"figures repaired: {repaired_figs} | files touched: {repaired_files} | "
        f"still empty: {still_empty} | markdown missing: {no_md}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
