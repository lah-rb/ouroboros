#!/usr/bin/env python3
"""Mechanical fill-in-the-middle check: can the model put back one blanked
number or capitalised word in a held-out document window?

The primer's honest early signal (operator, 2026-09-19): a 1B model at
fine-tune scale may learn the sentinel grammar without acquiring the
bidirectional conditioning GLM gets from pretraining. Items come from
fim_transform.py (val documents only), class-stratified:
    copy_suffix  the answer also appears verbatim in the SUFFIX only — the
                 conditioning test (a left-to-right model cannot see it);
                 the stage-0 gate and bar read THIS class
    copy_prefix  in the prefix only (a plain LM can copy it — control)
    free         in neither (world knowledge)
Prompt = fim_wrap(prefix, "", suffix, order) up to <|fim_middle|>, greedy,
≤ 8 new tokens; exact = the generation starts with the answer and stops
(within two characters); prefix = the answer starts the generation. Run on
the init model for the baseline (expect ~0) and on the primed endpoint.

  ./.venv/bin/python probe_fim_recovery.py --items .../docs/fim_recovery_items.json \\
      --models base=~/models/OLMo-2-0425-1B,primed=... --device cuda:0 [--order spm]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

from fim_transform import fim_wrap  # noqa: E402


def _judge(answer: str, gen: str) -> tuple[bool, bool]:
    ans = answer.strip()
    first = gen.strip().split("\n")[0].strip()
    lead = first[: len(ans) + 2].strip()  # the model may run on into the suffix
    exact = lead.rstrip(".,;:").startswith(ans) and len(lead) <= len(ans) + 2
    return exact, gen.strip().startswith(ans)


def _tally(rows: list[tuple[dict, bool, bool]]) -> dict:
    n = len(rows)
    ex = sum(e for _, e, _ in rows)
    pr = sum(p for _, _, p in rows)
    num = [(e, p) for it, e, p in rows if it.get("numeric", it["answer"][:1].isdigit())]
    return {
        "n": n,
        "exact": round(ex / max(1, n), 3),
        "prefix": round(pr / max(1, n), 3),
        "numeric_exact": round(sum(e for e, _ in num) / max(1, len(num)), 3),
        "n_numeric": len(num),
    }


def run(name: str, path: str, items: list[dict], device: str, batch: int, max_new: int, order: str) -> dict:
    tok = AutoTokenizer.from_pretrained(path)
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16).to(device).eval()
    prompts = [fim_wrap(it["prefix"], "", it["suffix"], order) for it in items]
    gens: list[str] = []
    t0 = time.time()
    with torch.no_grad():
        for b in range(0, len(prompts), batch):
            enc = tok(prompts[b : b + batch], return_tensors="pt", padding=True).to(device)
            out = model.generate(**enc, max_new_tokens=max_new, do_sample=False, pad_token_id=tok.pad_token_id)
            cut = enc["input_ids"].shape[1]
            gens += [tok.decode(r[cut:], skip_special_tokens=True) for r in out]
    rows = [(it, *_judge(it["answer"], g)) for it, g in zip(items, gens)]
    by_class: dict[str, list] = {}
    for row in rows:
        by_class.setdefault(row[0].get("class", "all"), []).append(row)
    samples = [
        {"class": it.get("class", ""), "prefix": it["prefix"][-60:], "answer": it["answer"], "gen": g[:60], "exact": e}
        for (it, e, _), g in list(zip(rows, gens))[:24]
    ]
    del model
    torch.cuda.empty_cache()
    return {
        "model": name,
        "path": path,
        "order": order,
        **_tally(rows),
        "by_class": {c: _tally(r) for c, r in sorted(by_class.items())},
        "seconds": round(time.time() - t0),
        "samples": samples,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", required=True)
    ap.add_argument("--models", required=True, help="name=DIR[,name=DIR...]")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--max-new", type=int, default=8)
    ap.add_argument("--order", choices=("psm", "spm"), default="psm")
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/analysis/fim_smoke/recovery.json"))
    args = ap.parse_args()
    items = json.load(open(args.items))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    res = []
    for spec in args.models.split(","):
        name, path = spec.split("=", 1)
        r = run(name, os.path.expanduser(path), items, args.device, args.batch, args.max_new, args.order)
        res.append(r)
        cls = " ".join(f"{c}={v['exact']:.3f}(n={v['n']})" for c, v in r["by_class"].items())
        print(
            f"{name}: exact {r['exact']:.3f} | prefix {r['prefix']:.3f} | numeric exact {r['numeric_exact']:.3f} (n={r['n_numeric']}) | {cls} | {r['seconds']}s",
            flush=True,
        )
        json.dump(res, open(args.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
