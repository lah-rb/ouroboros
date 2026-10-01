"""Replay ONE pack window through production's prompt and keep the raw output.

Why a paper's window packed what it did: _pack_windowed keeps only the
parsed, gated data, never the model's text or its reasoning/generation
counts. This renders the window exactly as _pack_windowed does (part-of-N
preface, registry block, prior_keys from the given earlier windows), sends
it N times on the chosen domain, and saves each raw response with
generatedTokens / reasoningTokens / truncated and the gate verdict.

    .venv/bin/python dev/replay_pack_window.py <paper_key> <window> [--domain pack_gemma] [--n 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.pack_windows import repair_shapes, window_sections  # noqa: E402
from agent.effects.child import ChildEffects  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402
from agent.llm_json import parse_llm_json  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
OUT = os.path.expanduser("~/tmp/pack_replay")
GEMMA = {"endpoint": "http://192.168.1.76:8008/graphql", "model": "gemma-4-12b-3060"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("window", type=int)
    ap.add_argument("--domain", default="")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument(
        "--prior-keys", default="", help="comma list, as earlier windows would pass"
    )
    ap.add_argument(
        "--target",
        type=int,
        default=0,
        help="window target tokens (default: production)",
    )
    ap.add_argument(
        "--cap", type=int, default=0, help="window cap tokens (default: production)"
    )
    ap.add_argument(
        "--append", default="", help="text appended after the pack prompt (diagnostics)"
    )
    ap.add_argument("--list-windows", action="store_true")
    args = ap.parse_args()
    cfg = json.load(open(f"{ROOT}/.agent/mission.json"))["config"]
    domains = dict(cfg.get("llmvp_domains") or {}, pack_gemma=GEMMA)
    fx = ChildEffects(
        LocalEffects(ROOT, llmvp_domains=domains),
        branch="bench:replay",
        inference_domain=args.domain,
    )
    doc = await ca._raw_curator_doc(fx, args.key)
    target, cap = ca._pack_window_sizes()
    target, cap = args.target or target, max(args.cap or cap, args.target or 0)
    wins = window_sections(doc, target, cap)
    if args.list_windows:
        import re

        for x in wins:
            tabs = sorted(set(re.findall(r"Table \d+\.", x.text)))
            print(x.index, x.tokens, "tok", x.first_heading[:40], tabs)
        return
    w = wins[args.window]
    registry = await ca._load_registry(fx)
    aliases = await ca.load_key_aliases(fx)
    preface = (
        ca._PACK_PREFACE.format(
            n=w.index + 1,
            total=len(wins),
            sections=w.section_count,
            heading=w.first_heading,
        )
        if len(wins) > 1
        else ""
    )
    ctx = {"key_registry_block": ca.format_key_registry(registry), "gate_feedback": ""}
    if args.prior_keys:
        ctx["prior_keys"] = args.prior_keys
    prompt = (
        preface
        + w.text
        + "\n\n---\n\n"
        + await ca._render_prompt("curator/pack_data", ctx)
    )
    if args.append:
        prompt += "\n\n" + args.append
    os.makedirs(OUT, exist_ok=True)
    tag = f"{args.key[:40]}_w{args.window}_{args.domain or 'muse'}"
    if args.target:
        tag += f"_t{target}"
    if args.append:
        tag += "_diag"
    rows = []
    for i in range(args.n):
        t0 = time.time()
        r = await fx.run_inference(prompt, {"max_tokens": 8192, "temperature": "t*0.4"})
        text = r.text or ""
        data = parse_llm_json(text) if text.strip() else None
        data = data if isinstance(data, dict) else None
        gates = {}
        if data is not None:
            data = ca.canonicalize_pack_keys(data, aliases)
            data, _ = repair_shapes(data, registry)
            gates = ca._run_pack_gates(data, w.text, registry)
        row = {
            "i": i,
            "seconds": round(time.time() - t0),
            "error": r.error,
            "generated": getattr(r, "generated_tokens", None) or r.tokens_generated,
            "reasoning": getattr(r, "reasoning_tokens", None),
            "truncated": getattr(r, "truncated", None),
            "text_chars": len(text),
            "top_keys": sorted((data or {}).keys()),
            "numeric_leaves": len(ca._numeric_leaf_tokens(data or {})),
            "passed": gates.get("passed"),
            "feedback": str(gates.get("feedback") or "")[:300],
        }
        rows.append(row)
        open(f"{OUT}/{tag}_{i}.txt", "w").write(text)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    json.dump(rows, open(f"{OUT}/{tag}.json", "w"), indent=1)


if __name__ == "__main__":
    asyncio.run(main())
