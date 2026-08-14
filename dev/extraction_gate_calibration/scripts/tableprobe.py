#!/usr/bin/env python3
"""Is table damage mechanically detectable? Measured against blind tiers.

`table_structure` is the largest single defect class in the blind audit — 8 of
25 tier-C papers — and the rate metrics are blind to all of it: a table whose
header is dropped and whose values bind to the wrong row still contains every
number, so recall scores it 1.00.

Three candidate signals, all cheap:

  ROW DEFICIT   PyMuPDF's find_tables() reports the rows it detects in the
                source. Compare against <tr> emitted for that page.
  CELL RECALL   find_tables() also yields CELL TEXT. A number in a source cell
                that appears nowhere in the page's markdown is a lost cell.
  TOKEN LEAK    <lcel>/<fcel>/<ecel>/<nl> are PaddleOCR's internal table
                tokens. They should never reach output; when they do, a table
                was emitted as raw token soup.

Validation, not assertion: score all 48 audited papers and check whether the
signals separate the tiers. A detector that does not separate is noise, however
reasonable it sounds.
"""

from __future__ import annotations

import json
import re
import statistics
import sys

sys.path.insert(0, "/Users/lah-rb/Repos/ouroboros/tools/pdf_extract")

import fitz  # noqa: E402
from extract_batch import _NUM_RE, _norm  # noqa: E402

D = (
    "/private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad/calib"
)
_LEAK = re.compile(r"<(?:lcel|fcel|ecel|nl|ucel)>", re.I)


def score(pdf_path: str, pages_md: list) -> dict:
    doc = fitz.open(pdf_path)
    aligned = len(pages_md) == doc.page_count
    src_rows = out_rows = 0
    cells_total = cells_hit = 0
    tables_found = 0
    for i, page in enumerate(doc):
        try:
            found = page.find_tables()
        except Exception:  # noqa: BLE001 — detection is best-effort
            continue
        tabs = list(getattr(found, "tables", []) or [])
        if not tabs:
            continue
        md = pages_md[i] if aligned else "\n".join(pages_md)
        md_n = _norm(md)
        md_c = re.sub(r"\s", "", md_n)
        out_rows += len(re.findall(r"<tr\b", md, re.I))
        for t in tabs:
            tables_found += 1
            try:
                data = t.extract()
            except Exception:  # noqa: BLE001
                continue
            src_rows += len(data)
            for row in data:
                for cell in row:
                    if not cell:
                        continue
                    for n in _NUM_RE.findall(_norm(str(cell))):
                        cells_total += 1
                        if n in md_c or n in md_n:
                            cells_hit += 1
    doc.close()
    whole = "\n".join(pages_md)
    return {
        "tables": tables_found,
        "src_rows": src_rows,
        "out_rows": out_rows,
        "row_keep": (out_rows / src_rows) if src_rows else None,
        "cell_recall": (cells_hit / cells_total) if cells_total else None,
        "cells": cells_total,
        "token_leak": len(_LEAK.findall(whole)),
    }


def main() -> None:
    key = {k["case_id"]: k for k in json.load(open(f"{D}/key.json"))}
    ver = {v["case_id"]: v for v in json.load(open(f"{D}/verdicts.json"))}
    rows = []
    for cid, k in key.items():
        pages_md = open(k["md"], errors="replace").read().split("\n\n---\n\n")
        s = score(k["pdf"], pages_md)
        s["case_id"] = cid
        s["tier"] = ver[cid]["tier"]
        s["defect"] = ver[cid]["dominant_defect"]
        rows.append(s)
        print(
            f"{cid} {s['tier']} {s['defect'][:18]:18s} tables={s['tables']:3d} "
            f"rows {s['out_rows']:4d}/{s['src_rows']:4d} "
            f"keep={s['row_keep'] if s['row_keep'] is None else round(s['row_keep'],3)} "
            f"cell_recall={s['cell_recall'] if s['cell_recall'] is None else round(s['cell_recall'],3)} "
            f"leak={s['token_leak']}",
            file=sys.stderr,
        )
    json.dump(rows, open(f"{D}/tableprobe.json", "w"), indent=1)

    print("\n=== separation by tier ===")
    for field in ("row_keep", "cell_recall", "token_leak"):
        print(f"\n{field}:")
        for tier in "ABCD":
            vals = [
                r[field]
                for r in rows
                if r["tier"] == tier and r[field] is not None and r["tables"]
            ]
            if not vals:
                print(f"  {tier}: (none)")
                continue
            print(
                f"  {tier}: n={len(vals):2d} median={statistics.median(vals):.3f} "
                f"min={min(vals):.3f} max={max(vals):.3f}"
            )
    ts = [r for r in rows if r["defect"] == "table_structure"]
    others = [r for r in rows if r["defect"] != "table_structure" and r["tables"]]
    print(f"\n=== papers whose dominant defect IS table_structure (n={len(ts)}) ===")
    for r in sorted(ts, key=lambda r: (r["cell_recall"] is None, r["cell_recall"])):
        print(
            f"  {r['case_id']} {r['tier']} keep="
            f"{r['row_keep'] if r['row_keep'] is None else round(r['row_keep'],3)} "
            f"cell_recall={r['cell_recall'] if r['cell_recall'] is None else round(r['cell_recall'],3)} "
            f"leak={r['token_leak']} cells={r['cells']}"
        )
    for field in ("row_keep", "cell_recall"):
        a = [r[field] for r in ts if r[field] is not None]
        b = [r[field] for r in others if r[field] is not None]
        if a and b:
            print(
                f"\n{field}: table_structure median {statistics.median(a):.3f} "
                f"vs everything-else median {statistics.median(b):.3f}"
            )
    la = [r["token_leak"] for r in ts]
    lb = [r["token_leak"] for r in others]
    print(
        f"token_leak: table_structure {sum(1 for x in la if x)}/{len(la)} papers leak; "
        f"others {sum(1 for x in lb if x)}/{len(lb)}"
    )


if __name__ == "__main__":
    main()
