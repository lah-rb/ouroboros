#!/usr/bin/env python3
"""Does the inverse endpoint carry ANY signal that greedy decoding hides under its prior?

For N held-out items, score log P(" <species> " | identify prompt) for the TRUE species
and for a fixed candidate set (the K most frequent training species + the true one), and
report the rank of the true species among the candidates, top-1/top-5 by likelihood, and
the mean rank. Chance = (K+1)/2. If the true species ranks near chance the model has no
mapping; if it ranks well above chance but greedy still says "Beryl", the signal exists
and is buried by the prior — a training-objective problem, not a representation one.
    ./.venv/bin/python rank_inverse.py --model DIR --corpus DIR --n 200 --k 63
"""
import argparse, json, os, sys, collections, random, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from corpus_inverse import identify_prompt
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True); ap.add_argument("--corpus", required=True)
ap.add_argument("--n", type=int, default=200); ap.add_argument("--k", type=int, default=63)
ap.add_argument("--split", default="heldout"); ap.add_argument("--out", default="")
a = ap.parse_args()
docs = os.path.join(os.path.expanduser(a.corpus), "docs")
items = json.load(open(os.path.join(docs, "probe_items.json")))[a.split]
freq = collections.Counter()
for l in open(os.path.join(docs, "inverse_train.jsonl")):
    d = json.loads(l)
    if d["kind"] == "identify": freq[d["species"]] += 1
top = [s for s, _ in freq.most_common(a.k)]
rng = random.Random(0); rng.shuffle(items); items = items[: a.n]
tok = AutoTokenizer.from_pretrained(os.path.expanduser("~/models/OLMo-2-0425-1B"))
dev = "cuda" if torch.cuda.is_available() else "cpu"
model = AutoModelForCausalLM.from_pretrained(os.path.expanduser(a.model), dtype=torch.bfloat16).to(dev).eval()

def score(prompt, names):
    """sum log P of ' Name' tokens after the prompt, batched over names."""
    p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
    seqs, lens = [], []
    for nm in names:
        c = tok(" " + nm, add_special_tokens=False)["input_ids"]
        seqs.append(p_ids + c); lens.append(len(c))
    L = max(len(s) for s in seqs)
    ids = torch.full((len(seqs), L), tok.pad_token_id, dtype=torch.long)
    for i, s in enumerate(seqs): ids[i, : len(s)] = torch.tensor(s)
    with torch.no_grad():
        lp = torch.log_softmax(model(ids.to(dev)).logits.float(), -1)
    out = []
    for i, s in enumerate(seqs):
        n = lens[i]; start = len(p_ids)
        tgt = torch.tensor(s[start:], device=dev)
        out.append(lp[i, start - 1 : start - 1 + n].gather(1, tgt[:, None]).sum().item())
    return out

ranks, t0 = [], time.time(); hit1 = hit5 = 0; per_item = []
for it in items:
    cands = [it["species"]] + [s for s in top if s != it["species"]][: a.k]
    sc = score(identify_prompt(it["peaks"], it["laser"]), cands)
    order = sorted(range(len(cands)), key=lambda i: -sc[i])
    r = order.index(0) + 1; ranks.append(r); hit1 += r == 1; hit5 += r <= 5
    per_item.append({"species": it["species"], "rank": r, "best": cands[order[0]]})
n, K = len(items), len(cands)
res = {"n": n, "candidates": K, "chance_rank": (K + 1) / 2, "mean_rank": sum(ranks) / n, "median_rank": sorted(ranks)[n // 2],
       "top1": hit1 / n, "top5": hit5 / n, "rank_le_10": sum(r <= 10 for r in ranks) / n,
       "best_answer_hist": collections.Counter(p["best"] for p in per_item).most_common(5), "seconds": time.time() - t0}
print(json.dumps({k: v for k, v in res.items()}, default=str, indent=1))
if a.out: json.dump({"summary": res, "items": per_item}, open(os.path.expanduser(a.out), "w"), indent=1, default=str)
