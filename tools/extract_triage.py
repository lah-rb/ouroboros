#!/usr/bin/env python3
"""Inspect what the extraction gate rejected — and promote what survives a look.

WHY THIS EXISTS. The gate refuses an extraction it cannot verify, and
verification compares our markdown against the PDF's OWN text layer. A scanned
paper has no text layer, so there is nothing to compare, so it scores a vacuous
1.00 and is refused — which is precisely the document OCR exists to handle.

Measured on the spectra corpus 2026-08-14: 81 failed records held **64 usable
markdowns totalling 5.0M characters and 1,353 extracted figures**, none of
which any downstream stage could see (`_fig_pending` gates on
`extraction_status == "extracted"`). Spot samples read fine — a 63k-char
mineralogy paper, a 107k-char aerosol-optics paper — correct extractions,
discarded for failing a check that had nothing to check.

THE GATE IS NOT WRONG TO REFUSE THEM. Unverifiable means unproven, and
silently admitting unproven text is how a corpus rots. What was wrong is that
refusal was terminal and invisible. This tool makes the rejected work
INSPECTABLE (source PDF beside extracted markdown) and gives a human the one
verdict a machine cannot supply here.

    review    what was rejected, why, and how much text/figures it holds
    pairs     write a side-by-side manifest + symlink tree for reading
    promote   mark inspected papers `extracted` so the curator can see them

Usage:
    python tools/extract_triage.py --working-dir ~/corpora/ouroboros-spectra
    python tools/extract_triage.py --working-dir ... --pairs /tmp/inspect
    python tools/extract_triage.py --working-dir ... --promote KEY [KEY ...]
    python tools/extract_triage.py --working-dir ... --promote-file keys.txt

Stdlib only.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REJECTED = ("extract_failed", "extract_unverified")

# Read from the gate rather than restated, so a recalibration cannot leave this
# tool bucketing failures against thresholds the pipeline no longer uses.
try:
    from agent.actions.extraction_actions import MIN_NUMERIC_RATE, MIN_SPAN_RATE
except Exception:  # noqa: BLE001 — stdlib-only fallback, keep the tool runnable
    MIN_NUMERIC_RATE, MIN_SPAN_RATE = 0.75, 0.70


def read_extraction(databank: Path) -> dict:
    """paper_key -> record from the extractor's own sidecar, last-wins."""
    recs: dict = {}
    path = databank / "extraction.jsonl"
    if not path.is_file():
        sys.exit(f"no extraction sidecar at {path}")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("paper_key"):
            recs[r["paper_key"]] = r
    return recs


def why(rec: dict) -> str:
    """Which gate rejected this, in one word."""
    fr = rec.get("failure_reason") or ""
    if "no verifiable text layer" in fr or re.search(r"numeric=1\.00, span=1\.00", fr):
        return "no-text-layer"
    # A looped decode passes BOTH rates — it keeps every number — so it carries
    # no numeric=/span= pair to parse and would otherwise land in `error`
    # beside a crashed worker. Two entirely different things to do about them.
    if "degenerate decode" in fr:
        return "degenerate"
    # The extraction is FINE and the asset is wrong — the only bucket here
    # whose fix is re-acquisition rather than anything the extractor can do.
    if "truncated acquisition" in fr:
        return "truncated"
    m = re.search(r"numeric=([0-9.]+), span=([0-9.]+)", fr)
    if m:
        n, s = float(m.group(1)), float(m.group(2))
        if n < MIN_NUMERIC_RATE and s < MIN_SPAN_RATE:
            return "both-gates"
        return "numeric" if n < MIN_NUMERIC_RATE else "span"
    if "timed out" in fr or "no report" in fr:
        return "toolchain"
    return "error"


def md_chars(W: Path, rec: dict) -> int:
    p = rec.get("md_path") or ""
    f = W / p
    return len(f.read_text(encoding="utf-8", errors="replace")) if f.is_file() else 0


def cmd_review(W: Path, recs: dict) -> None:
    rej = [r for r in recs.values() if r.get("extraction_status") in REJECTED]
    tot = len(recs)
    print(f"extraction records : {tot}")
    print(
        f"  extracted        : {sum(1 for r in recs.values() if r.get('extraction_status') == 'extracted')}"
    )
    print(f"  REJECTED         : {len(rej)}\n")
    by = collections.defaultdict(list)
    for r in rej:
        by[why(r)].append(r)
    print(f"  {'reason':16s} {'n':>4}  {'w/ md':>6} {'chars':>10} {'figures':>8}")
    for k, group in sorted(by.items(), key=lambda kv: -len(kv[1])):
        chars = sum(md_chars(W, r) for r in group)
        figs = sum(int(r.get("figure_count") or 0) for r in group)
        withmd = sum(1 for r in group if md_chars(W, r) > 0)
        print(f"  {k:16s} {len(group):>4}  {withmd:>6} {chars:>10,} {figs:>8}")
    allchars = sum(md_chars(W, r) for r in rej)
    allfigs = sum(int(r.get("figure_count") or 0) for r in rej)
    print(f"\n  TOTAL WITHHELD: {allchars:,} chars, {allfigs} figures")
    print("  `no-text-layer` is the OCR case — a scan has nothing to verify")
    print("  against, so the rate is vacuous rather than earned. Read before judging.")


def cmd_pairs(W: Path, recs: dict, out: Path) -> None:
    """Source PDF beside extracted markdown, for a human to compare."""
    rej = [
        r
        for r in recs.values()
        if r.get("extraction_status") in REJECTED and md_chars(W, r) > 0
    ]
    rej.sort(key=lambda r: (why(r), -md_chars(W, r)))
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.tsv"
    with manifest.open("w", encoding="utf-8") as fh:
        fh.write(
            "paper_key\treason\tmd_chars\tfigures\tpdf\tmarkdown\tfailure_reason\n"
        )
        for r in rej:
            key = r["paper_key"]
            pdf = W / (r.get("pdf_path") or "")
            md = W / (r.get("md_path") or "")
            d = out / why(r) / key
            d.mkdir(parents=True, exist_ok=True)
            # Symlinks, not copies: the corpus can be GBs and this tree is a
            # reading aid, not a second copy of the data.
            for src, name in ((pdf, "source.pdf"), (md, "extracted.md")):
                link = d / name
                if src.is_file() and not link.exists():
                    try:
                        link.symlink_to(src.resolve())
                    except OSError:
                        pass
            fh.write(
                "\t".join(
                    [
                        key,
                        why(r),
                        str(md_chars(W, r)),
                        str(r.get("figure_count") or 0),
                        str(pdf),
                        str(md),
                        (r.get("failure_reason") or "").replace("\t", " ")[:120],
                    ]
                )
                + "\n"
            )
    print(f"{len(rej)} pair(s) -> {out}")
    print(f"  manifest: {manifest}")
    print("  grouped by reason; each dir holds source.pdf + extracted.md")
    print("\n  When a paper reads correctly, promote it:")
    print(f"    python tools/extract_triage.py --working-dir {W} --promote <key>")


def mission_is_running(W: Path) -> bool:
    """ps, NOT pgrep -fa: macOS accepts -a and silently ignores it, printing
    bare PIDs, so a path match can never succeed. Fails CLOSED."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    t = str(W)
    return any(
        "ouroboros.py start" in ln and (t in ln or W.name in ln)
        for ln in out.splitlines()
    )


def cmd_promote(W: Path, recs: dict, keys: list) -> int:
    """Mark inspected papers `extracted` so downstream stages can see them."""
    if mission_is_running(W):
        print(
            "REFUSING: a mission is running on this workspace.\n"
            "  extraction.jsonl is appended by the extractor; writing now races it.\n"
            "  Pause the mission and re-run."
        )
        return 2
    updates, skipped = [], []
    for k in keys:
        r = recs.get(k)
        if r is None:
            skipped.append((k, "no such record"))
            continue
        if r.get("extraction_status") not in REJECTED:
            skipped.append(
                (k, f"status is {r.get('extraction_status')!r}, not rejected")
            )
            continue
        if md_chars(W, r) <= 0:
            skipped.append((k, "no markdown on disk — nothing was inspected"))
            continue
        r = dict(r)
        r["extraction_status"] = "extracted"
        # PROVENANCE, because a human overrode a gate. An audit must be able
        # to tell a machine-verified extraction from a human-vouched one, and
        # the original verdict must survive the promotion.
        r["promoted_by"] = "manual-inspection"
        r["promoted_from"] = recs[k].get("extraction_status")
        r["promoted_reason"] = recs[k].get("failure_reason") or ""
        updates.append(r)
    if updates:
        with (W / "databank" / "extraction.jsonl").open("a", encoding="utf-8") as fh:
            for r in updates:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"promoted {len(updates)} record(s) to `extracted`")
    for k, reason in skipped:
        print(f"  skipped {k}: {reason}")
    if updates:
        print("They now enter fig_review and the curator on the next curator run.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--working-dir", required=True)
    ap.add_argument("--pairs", default="", help="write a side-by-side tree here")
    ap.add_argument("--promote", nargs="*", default=None, help="paper_keys to promote")
    ap.add_argument(
        "--promote-file", default="", help="file of paper_keys, one per line"
    )
    args = ap.parse_args()

    W = Path(os.path.expanduser(args.working_dir)).resolve()
    recs = read_extraction(W / "databank")

    keys = list(args.promote or [])
    if args.promote_file:
        keys += [
            ln.strip()
            for ln in Path(args.promote_file).read_text().splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
    if keys:
        return cmd_promote(W, recs, keys)
    if args.pairs:
        cmd_pairs(W, recs, Path(os.path.expanduser(args.pairs)).resolve())
        return 0
    cmd_review(W, recs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
