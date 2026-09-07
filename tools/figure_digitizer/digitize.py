#!/usr/bin/env python3
"""Digitise published spectrum plots into peak sets.

Phase 1a scope: locate each figure crop in its source PDF, choose the best
available pixel source, and record what that source can resolve. Axes, curve
tracking and peaks land in later phases; until then a figure's status is
``sourced`` rather than ``digitized``.

Reads the corpus and writes ONLY to ``databank/figdata/``. Nothing here
touches the running mission's tables, flows or packs.

    .venv/bin/python -m tools.figure_digitizer.digitize --key <paper_key>
    .venv/bin/python -m tools.figure_digitizer.digitize --survey --limit 250
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

import fitz  # pymupdf

from . import graphmeta, schema, source


def _hints_by_fig(working_dir: str, key: str) -> dict:
    return {h.fig: h for h in graphmeta.hints(working_dir, key)}


def process_paper(
    working_dir: str,
    key: str,
    *,
    technique: str | None = None,
    report: schema.RunReport | None = None,
    max_figs: int = 0,
) -> dict | None:
    """Source every candidate figure of one paper."""
    pdf = source.find_pdf(working_dir, key)
    if not pdf:
        return None
    hints = _hints_by_fig(working_dir, key)
    if not hints:
        return None

    try:
        doc = fitz.open(pdf)
    except Exception:  # noqa: BLE001 — an unopenable PDF is a datum, not a crash
        return None

    figs: list[dict] = []
    try:
        index = source.PageIndex(doc)
        wanted = [
            h
            for h in hints.values()
            if h.is_candidate and (technique is None or h.technique == technique)
        ]
        if max_figs:
            wanted = wanted[:max_figs]
        for h in wanted:
            crop = os.path.join(working_dir, "databank", "figures", key, h.fig)
            if not os.path.isfile(crop):
                figs.append(
                    schema.figure_record(
                        h.fig,
                        "rejected",
                        reject_reason="crop_missing",
                        technique=h.technique,
                        caption=h.caption,
                    )
                )
                continue

            reloc = source.relocate(crop, doc, index)
            if reloc is None:
                figs.append(
                    schema.figure_record(
                        h.fig,
                        "rejected",
                        reject_reason="crop_unreadable",
                        technique=h.technique,
                        caption=h.caption,
                    )
                )
                continue

            if report is not None:
                report.relocation_ncc.append(reloc.ncc)

            if not reloc.located:
                figs.append(
                    schema.figure_record(
                        h.fig,
                        "rejected",
                        reject_reason="not_relocated",
                        technique=h.technique,
                        caption=h.caption,
                    )
                )
                continue

            src = source.choose_source(doc, reloc)

            # A crop the size of its whole page is the extractor's known
            # full-page leak, not a figure. It relocates perfectly and would
            # otherwise sail through as a plot.
            if reloc.full_page:
                figs.append(
                    schema.figure_record(
                        h.fig,
                        "not_a_plot",
                        reject_reason="full_page_render",
                        technique=h.technique,
                        caption=h.caption,
                        reloc=reloc,
                        src=src,
                    )
                )
                continue

            figs.append(
                schema.figure_record(
                    h.fig,
                    "sourced",
                    technique=h.technique,
                    caption=h.caption,
                    reloc=reloc,
                    src=src,
                )
            )
            if report is not None and src.resolution_gain_vs_crop != float("inf"):
                report.gain.append(src.resolution_gain_vs_crop)
    finally:
        doc.close()

    if report is not None:
        for f in figs:
            report.figures += 1
            report.bump("status", f["status"])
            if f.get("reject_reason"):
                report.bump("reject_reason", f["reject_reason"])
            if f.get("source"):
                report.bump("tier", f["source"]["tier"])

    return schema.paper_record(key, figs)


def _cohort(working_dir: str, technique: str) -> list[str]:
    """Papers holding at least one candidate figure of a technique."""
    import glob

    keys = []
    for path in sorted(glob.glob(f"{working_dir}/databank/figtext/*.json")):
        key = os.path.basename(path)[:-5]
        if any(
            h.is_candidate and h.technique == technique
            for h in graphmeta.hints(working_dir, key)
        ):
            keys.append(key)
    return keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/corpora/ouroboros-spectra"))
    ap.add_argument("--key", help="digitise one paper")
    ap.add_argument("--survey", action="store_true", help="measure a cohort")
    ap.add_argument("--technique", default="libs")
    ap.add_argument("--limit", type=int, default=0, help="max papers in a survey")
    ap.add_argument("--max-figs", type=int, default=0, help="max figures per paper")
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--dry-run", action="store_true", help="measure, write nothing")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not args.key and not args.survey:
        ap.error("one of --key or --survey is required")

    report = schema.RunReport()
    t0 = time.time()

    if args.key:
        keys = [args.key]
    else:
        keys = _cohort(args.root, args.technique)
        if args.limit:
            random.Random(args.seed).shuffle(keys)
            keys = keys[: args.limit]
        print(f"cohort: {len(keys)} papers with a {args.technique} candidate")

    for i, key in enumerate(keys, 1):
        rec = process_paper(
            args.root,
            key,
            technique=args.technique,
            report=report,
            max_figs=args.max_figs,
        )
        if rec is None:
            continue
        report.papers += 1
        if not args.dry_run:
            schema.write_sidecar(args.root, rec)
        if args.verbose or (args.survey and i % 10 == 0):
            print(
                f"  [{i}/{len(keys)}] {key} "
                f"figs={len(rec['figs'])} elapsed={time.time() - t0:.0f}s",
                flush=True,
            )

    out = report.as_dict()
    out["elapsed_s"] = round(time.time() - t0, 1)
    print(json.dumps(out, indent=1))

    if args.survey and not args.dry_run:
        runs = os.path.join(args.root, "databank", "figdata", "_runs")
        os.makedirs(runs, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        with open(os.path.join(runs, f"{stamp}.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
