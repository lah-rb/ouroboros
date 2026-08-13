#!/usr/bin/env python3
"""Three tenants on one GPU: scraper text, paddle OCR, curator figure-read.

WHY A SECOND PROBE. `dev/gpu_contention_probe.py` measured TWO tenants
(paddle OCR x muse text decode) at serialization 0.339 — 66% of the
theoretical overlap realized. Two-tenant numbers do not compose: the third
tenant, the curator's figure read, is NOT a third process. It goes to
`/v1/vision` on the SAME LLMVP that serves the scraper's text, and
`core/inference.py:1356` runs it inside `backend.generation_guard()`.

So the topology is not three peers. It is:

    process 1  LLMVP :8008   ── text  ─┐
                              └─ vision ┘  serialized by generation_guard
    process 2  llama-server  ── paddle OCR  (own Metal command queue)

PREDICTION, stated before the run so the probe can falsify it: T1+T3
serializes at ~1.0 (by construction, not by contention), while T2 overlaps
either at ~0.34. If T1+T3 comes back well below 1.0 the guard is not doing
what the code says. If T2+T3 comes back much worse than T1+T2, the vision
instance contends differently from text decode and the 0.339 does not
transfer.

SEVEN ARMS, fixed work per leg:

    solo    T1 text          T2 ocr          T3 vision
    pair    T1+T2            T1+T3           T2+T3
    triple  T1+T2+T3

Each tenant runs EXACTLY the production path — `extract_batch.py` and
`fig_review.py` as subprocesses (what the agent actions run), text through
the live GraphQL endpoint. Legs are sized to land near 200 s so the ratios
are readable.

serialization = (t_together - max(legs)) / (sum(legs) - max(legs))
   0.0 = free overlap     1.0 = fully serialized

Usage (repo root, ~35 min):

    .venv/bin/python dev/three_tenant_probe.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

# Run-from-anywhere: the sibling probe is imported as a package module, so
# the repo root has to be importable whether this is invoked as a path or
# with -m.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev.gpu_contention_probe import (  # noqa: E402
    CORPUS,
    REPO,
    infer_leg,
    ocr_leg,
    pick_pdfs,
    wired_mb,
)

SCRATCH = Path("/private/tmp/claude-501/ouroboros_three_tenant")

# 5 figures ~ 210 s at the 42 s/figure measured 2026-08-13 — the same order
# as the OCR leg (184 s) and the text leg (198 s).
VISION_KEY_DEFAULT = "doi_10.1007_s11214-012-9873-5"


async def vision_leg(key: str, out_dir: Path, tag: str) -> dict:
    """One fig_review dispatch — exactly what action_fig_review_batch runs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(REPO / "tools" / "fig_review" / ".venv" / "bin" / "python"),
        str(REPO / "tools" / "fig_review" / "fig_review.py"),
        "--keys",
        key,
        "--figures-root",
        str(CORPUS / "databank" / "figures"),
        "--markdown-dir",
        str(CORPUS / "databank" / "markdown"),
        "--out-dir",
        str(out_dir),
        "--vl-backend",
        "llmvp",
    ]
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    wall = time.monotonic() - t0

    report = {}
    for line in (out or b"").decode("utf-8", "replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("paper_key"):
            report = r
    # The figtext JSON is the fidelity artifact: same figures, same
    # descriptions, or the overlap is buying throughput with quality.
    text = ""
    fp = report.get("figtext_path") or ""
    if fp and Path(fp).is_file():
        text = Path(fp).read_text(encoding="utf-8", errors="replace")
    return {
        "arm": tag,
        "wall_s": round(wall, 1),
        "rc": proc.returncode,
        "figs": report.get("figs"),
        "seconds": report.get("seconds"),
        "error": report.get("error"),
        "chars": len(text),
    }


def serialization(t_together: float, legs: list[float]) -> dict:
    ideal, serial = max(legs), sum(legs)
    return {
        "t_together_s": round(t_together, 1),
        "legs_solo_s": [round(x, 1) for x in legs],
        "ideal_s": round(ideal, 1),
        "serialized_s": round(serial, 1),
        "serialization": (
            round((t_together - ideal) / (serial - ideal), 3)
            if serial > ideal
            else None
        ),
        "recovered_s": round(serial - t_together, 1),
        "recovered_pct": round((serial - t_together) / serial * 100, 1),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", type=int, default=2)
    ap.add_argument("--calls", type=int, default=14)
    ap.add_argument("--max-tokens", type=int, default=640)
    ap.add_argument("--vision-key", default=VISION_KEY_DEFAULT)
    ap.add_argument("--out", default="dev/three_tenant_results.json")
    args = ap.parse_args()

    pdfs = pick_pdfs(args.pdfs)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    R: dict = {
        "pdfs": [p.name for p in pdfs],
        "calls": args.calls,
        "vision_key": args.vision_key,
        "wired_start_mb": wired_mb(),
    }

    def T1(tag):
        return infer_leg(args.calls, args.max_tokens, tag)

    def T2(tag):
        return ocr_leg(pdfs, SCRATCH / tag, tag)

    def T3(tag):
        return vision_leg(args.vision_key, SCRATCH / tag, tag)

    async def arm(name: str, *legs) -> float:
        print(f"arm {name} ...", flush=True)
        t0 = time.monotonic()
        got = await asyncio.gather(*legs)
        wall = time.monotonic() - t0
        R[name] = {"wall_s": round(wall, 1), "legs": list(got)}
        print(f"  {round(wall, 1)}s", flush=True)
        return wall

    # ── solos ───────────────────────────────────────────────────────
    t1 = await arm("solo_T1_text", T1("T1"))
    t2 = await arm("solo_T2_ocr", T2("T2"))
    t3 = await arm("solo_T3_vision", T3("T3"))

    # ── pairs ───────────────────────────────────────────────────────
    p12 = await arm("pair_T1_T2", T1("T1_p12"), T2("T2_p12"))
    p13 = await arm("pair_T1_T3", T1("T1_p13"), T3("T3_p13"))
    p23 = await arm("pair_T2_T3", T2("T2_p23"), T3("T3_p23"))

    # ── triple ──────────────────────────────────────────────────────
    trip = await arm("triple", T1("T1_x"), T2("T2_x"), T3("T3_x"))

    R["verdict"] = {
        "solo_s": {"text": round(t1, 1), "ocr": round(t2, 1), "vision": round(t3, 1)},
        "T1_T2_text_x_ocr": serialization(p12, [t1, t2]),
        "T1_T3_text_x_vision": serialization(p13, [t1, t3]),
        "T2_T3_ocr_x_vision": serialization(p23, [t2, t3]),
        "T1_T2_T3_all": serialization(trip, [t1, t2, t3]),
        "wired_end_mb": wired_mb(),
    }

    # Fidelity: identical work, identical output, in every arm it appears in.
    solo_txt = [r.get("text", "") for r in R["solo_T1_text"]["legs"][0]["rows"]]
    for name in ("pair_T1_T2", "pair_T1_T3", "triple"):
        rows = next(
            (leg for leg in R[name]["legs"] if leg.get("arm", "").startswith("T1")), {}
        ).get("rows", [])
        same = sum(
            1 for a, b in zip(solo_txt, [r.get("text", "") for r in rows]) if a == b
        )
        R["verdict"].setdefault("text_identical", {})[name] = f"{same}/{len(solo_txt)}"

    solo_chars = R["solo_T3_vision"]["legs"][0].get("chars")
    for name in ("pair_T1_T3", "pair_T2_T3", "triple"):
        leg = next(
            (leg for leg in R[name]["legs"] if leg.get("arm", "").startswith("T3")), {}
        )
        R["verdict"].setdefault("vision_chars", {})[name] = [
            solo_chars,
            leg.get("chars"),
        ]

    solo_ocr = R["solo_T2_ocr"]["legs"][0].get("papers", [])
    for name in ("pair_T1_T2", "pair_T2_T3", "triple"):
        leg = next(
            (leg for leg in R[name]["legs"] if leg.get("arm", "").startswith("T2")), {}
        )
        same = sum(
            1
            for a, b in zip(solo_ocr, leg.get("papers", []))
            if a.get("chars") == b.get("chars")
        )
        R["verdict"].setdefault("ocr_chars_identical", {})[
            name
        ] = f"{same}/{len(solo_ocr)}"

    Path(args.out).write_text(json.dumps(R, indent=2), encoding="utf-8")
    print(json.dumps(R["verdict"], indent=2))
    print(f"\nfull results -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
