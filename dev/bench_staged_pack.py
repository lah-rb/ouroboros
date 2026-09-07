#!/usr/bin/env python3
"""Does section-windowed packing recover papers whole-document packing lost?

THE QUESTION. Pack rate falls from 96% (10-25k tokens) to 58% (50-100k) and
36% (>100k), and the recent failures are the grounding gate rejecting numbers
the paper never states. Two readings fit that curve: model recall decays with
context (the operator's hypothesis), or big documents are theses and reports
that simply carry less packable data. This harness separates them: the SAME
papers that failed whole-document packing, packed again in section-bounded
windows (agent/actions/pack_windows.py), through the SAME production prompt,
parser, canonicalisation and gates. If recall is the cause, they pack now; if
they were empty, they still fail.

PER WINDOW: production pack prompt (key registry block + gate feedback), one
retry with feedback, gates run against the WINDOW -- stricter than the
production whole-document check, and the one that kills fabrication.
MERGED: the per-window packs merged (lists concatenate, scalars first-wins
with conflicts recorded), then the production gates against the WHOLE doc.

Nothing is booked. Output is one JSON row per paper plus a summary.

    .venv/bin/python dev/bench_staged_pack.py --limit 12
    .venv/bin/python dev/bench_staged_pack.py --keys K1 K2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.curation_actions import (  # noqa: E402
    _curate_turn,
    _estimate_doc_tokens,
    _load_registry,
    _render_prompt,
    _run_pack_gates,
    build_curator_doc,
    canonicalize_pack_keys,
    format_key_registry,
    load_key_aliases,
)
from agent.actions.pack_windows import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    DEFAULT_TARGET_TOKENS,
    MergeReport,
    merge_packs,
    repair_shapes,
    window_sections,
)
from agent.actions.scholarly_actions import read_databank  # noqa: E402
from agent.effects.child import ChildEffects  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402
from agent.llm_json import parse_llm_json  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")


def _doc_for(root: str, key: str) -> str | None:
    md_p = f"{root}/databank/markdown/{key}.en.md"
    if not os.path.exists(md_p):
        md_p = f"{root}/databank/markdown/{key}.md"
    if not os.path.exists(md_p):
        return None
    md = open(md_p, encoding="utf-8", errors="ignore").read()
    fx = None
    ft = f"{root}/databank/figtext/{key}.json"
    if os.path.exists(ft):
        try:
            fx = json.load(open(ft))
        except Exception:
            fx = None
    return build_curator_doc(md, fx)


def _papers_side_reasons(root: str) -> dict[str, str]:
    """failure_reason as written to papers.jsonl -- the pack booking's own
    words -- which the merged view hides whenever the extraction sidecar
    carries the key (it overlays, and it usually carries "")."""
    out: dict[str, str] = {}
    with open(f"{root}/databank/papers.jsonl") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("paper_key") and "failure_reason" in r:
                out[r["paper_key"]] = str(r.get("failure_reason") or "")
    return out


def candidates(bank: dict, root: str, lo: int, hi: int) -> list[tuple[int, str, str]]:
    """Accepted papers whose PACK TURN failed (not envelope/lingual), by size."""
    out = []
    side = _papers_side_reasons(root)
    for k, r in bank.items():
        if (
            r.get("review_status") != "accepted"
            or r.get("pack_status") != "pack_failed"
        ):
            continue
        fr = side.get(k) or str(r.get("failure_reason") or "")
        # the papers-side reason is the pack booking's own; exclude the causes
        # windowing cannot touch (envelope fields, extraction/translation)
        if fr.startswith("envelope") or fr.startswith("extraction"):
            continue
        p = f"{root}/databank/markdown/{k}.md"
        if not os.path.exists(p):
            continue
        t = int(os.path.getsize(p) / 3.3)
        if lo <= t < hi:
            out.append((t, k, fr[:160]))
    out.sort()
    return out


async def pack_window(
    fx,
    win_text: str,
    registry: dict,
    aliases: dict,
    preface: str,
    a_no_repair: bool = False,
):
    """The production pack turn, applied to one window, gated on that window."""
    feedback = ""
    attempts = 0
    for _ in range(2):
        attempts += 1
        prompt = await _render_prompt(
            "curator/pack_data",
            {
                "key_registry_block": format_key_registry(registry),
                "gate_feedback": feedback,
            },
        )
        text = await _curate_turn(fx, preface + win_text + "\n\n---\n\n" + prompt, 8192)
        parsed = parse_llm_json(text)
        data = parsed if isinstance(parsed, dict) and parsed else None
        repairs: list[dict] = []
        if data is not None:
            data = canonicalize_pack_keys(data, aliases)
            if not a_no_repair:
                data, repairs = repair_shapes(data, registry)
        gates = (
            _run_pack_gates(data, win_text, registry)
            if data is not None
            else {
                "passed": False,
                "feedback": "output was not a JSON object",
                "grounding": {},
            }
        )
        gates["repairs"] = repairs
        if gates["passed"]:
            return data, gates, attempts
        feedback = gates["feedback"]
    return data, gates, attempts


async def main_async(a) -> int:
    cfg = json.load(open(f"{a.root}/.agent/mission.json"))["config"]
    parent = LocalEffects(a.root, llmvp_domains=cfg.get("llmvp_domains"))
    fx = ChildEffects(parent, branch="bench:staged_pack", inference_domain=a.domain)
    bank = await read_databank(fx)
    registry = await _load_registry(fx)
    aliases = await load_key_aliases(fx)

    if a.keys:
        todo = [
            (0, k, str(bank.get(k, {}).get("failure_reason") or "")[:160])
            for k in a.keys
        ]
    else:
        todo = candidates(bank, a.root, a.min_tokens, a.max_tokens)
        if a.limit:
            # spread across the size range rather than all-small
            import numpy as np

            idx = (
                np.linspace(0, len(todo) - 1, num=min(a.limit, len(todo)))
                .round()
                .astype(int)
            )
            todo = [todo[i] for i in dict.fromkeys(idx.tolist())]
    print(
        f"candidates: {len(todo)}  (accepted, pack_failed on the pack turn, {a.min_tokens//1000}k-{a.max_tokens//1000}k)"
    )
    print(f"window target {a.target:,} / cap {a.cap:,} tokens; domain {a.domain!r}\n")

    rows = []
    for n, (_, key, prior) in enumerate(todo, 1):
        doc = _doc_for(a.root, key)
        if not doc:
            rows.append(dict(key=key, outcome="no_doc"))
            continue
        wins = window_sections(doc, a.target, a.cap)
        dtok = _estimate_doc_tokens(doc)
        print(
            f"[{n:>2}/{len(todo)}] {key[:54]:<56} {dtok:>7,} tok -> {len(wins)} windows"
            f"{' (' + str(sum(w.oversize for w in wins)) + ' oversize)' if any(w.oversize for w in wins) else ''}",
            flush=True,
        )
        print(f"        prior whole-doc failure: {prior[:110]}")
        t0 = time.time()
        per = []
        packs = []
        for w in wins:
            preface = (
                f"[This is part {w.index + 1} of {len(wins)} of one paper — {w.section_count} "
                f'consecutive section(s) starting at "{w.first_heading}". Pack ONLY values '
                "stated in THIS part; other parts are packed separately and merged.]\n\n"
            )
            try:
                data, gates, attempts = await pack_window(
                    fx, w.text, registry, aliases, preface, a.no_repair
                )
            except Exception as e:  # noqa: BLE001
                per.append(dict(window=w.index, tokens=w.tokens, error=str(e)[:200]))
                print(
                    f"        w{w.index} {w.tokens:>6,} tok  TRANSPORT {str(e)[:80]}",
                    flush=True,
                )
                continue
            g = gates.get("grounding") or {}
            per.append(
                dict(
                    window=w.index,
                    tokens=w.tokens,
                    sections=w.section_count,
                    passed=bool(gates["passed"]),
                    attempts=attempts,
                    repairs=gates.get("repairs") or [],
                    data=data,
                    grounding_rate=g.get("grounding_rate"),
                    numeric_leaves=g.get("numeric_leaves"),
                    keys=len(data or {}),
                    feedback=str(gates.get("feedback") or "")[:200],
                )
            )
            print(
                f"        w{w.index} {w.tokens:>6,} tok  {'PASS' if gates['passed'] else 'fail'}  "
                f"keys={len(data or {}):>3} leaves={g.get('numeric_leaves')} gr={g.get('grounding_rate')} "
                f"att={attempts}"
                + ("" if gates["passed"] else f"  | {str(gates.get('feedback'))[:90]}"),
                flush=True,
            )
            if gates["passed"] and data:
                packs.append(data)
        rep = MergeReport()
        merged = merge_packs(packs, rep) if packs else {}
        final = (
            _run_pack_gates(merged, doc, registry)
            if merged
            else {"passed": False, "feedback": "no window packed", "grounding": {}}
        )
        secs = time.time() - t0
        fg = final.get("grounding") or {}
        outcome = "packed" if final["passed"] else "failed"
        rows.append(
            dict(
                key=key,
                doc_tokens=dtok,
                windows=len(wins),
                windows_passed=sum(1 for p in per if p.get("passed")),
                outcome=outcome,
                merged_keys=len(merged),
                merged_leaves=fg.get("numeric_leaves"),
                merged_grounding=fg.get("grounding_rate"),
                conflicts=rep.conflicts[:20],
                n_conflicts=len(rep.conflicts),
                final_feedback=str(final.get("feedback") or "")[:300],
                seconds=round(secs),
                prior_failure=prior,
                per_window=per,
            )
        )
        print(
            f"        => {outcome.upper()}  windows passed {rows[-1]['windows_passed']}/{len(wins)}  merged keys {len(merged)} "
            f"leaves {fg.get('numeric_leaves')} grounding {fg.get('grounding_rate')} conflicts {len(rep.conflicts)}  {secs:.0f}s"
            + (
                ""
                if final["passed"]
                else f"\n        final gate: {str(final.get('feedback'))[:160]}"
            ),
            flush=True,
        )
        json.dump(rows, open(a.out, "w"), indent=1, ensure_ascii=False)

    json.dump(rows, open(a.out, "w"), indent=1, ensure_ascii=False)
    done = [r for r in rows if r.get("outcome") in ("packed", "failed")]
    packed = [r for r in done if r["outcome"] == "packed"]
    print(
        f"\nSTAGED PACK: {len(packed)}/{len(done)} papers packed that whole-document packing had failed"
    )
    if done:
        wp = sum(r["windows_passed"] for r in done)
        wt = sum(r["windows"] for r in done)
        print(
            f"  windows passed {wp}/{wt} = {100*wp/max(wt,1):.0f}%;  scalar conflicts total {sum(r['n_conflicts'] for r in done)}"
        )
        for lo, hi in ((0, 75_000), (75_000, 100_000), (100_000, 10**9)):
            sel = [r for r in done if lo <= r["doc_tokens"] < hi]
            if sel:
                print(
                    f"  {lo//1000:>3}k-{hi//1000 if hi < 10**8 else '∞':>3}: {sum(1 for r in sel if r['outcome']=='packed')}/{len(sel)} packed"
                )
    print(f"wrote {a.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument(
        "--domain",
        default="curate_remote",
        help="llmvp_domains route to run on ('' = local)",
    )
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-tokens", type=int, default=50_000)
    ap.add_argument("--max-tokens", type=int, default=10**9)
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET_TOKENS)
    ap.add_argument(
        "--no-repair", action="store_true", help="skip repair_shapes before the gates"
    )
    ap.add_argument("--cap", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/bench_staged_pack.json"))
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
