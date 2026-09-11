#!/usr/bin/env python3
"""Frozen-representation head: is the species readable from the LM's encoding of a peak list?

rock venv, GPU. The generation readout of the inverse endpoint scored 0.001 with zero
prompt sensitivity (PROCEDURE.md §20). This asks the prior question: does the frozen
model's hidden state over the identify prompt contain species information that a
classifier can read? Three models (inverse endpoint / stage-1 endpoint / base), three
poolings (final-layer last token, final-layer mean, mid-layer mean), two heads (linear
softmax; 2048→1024→C MLP). Controls: the same heads over a 10 cm⁻¹ intensity-binned
peak vector, and kNN (from probe_inverse.py's control run).

    ./.venv/bin/python head_inverse.py --corpus ~/corpora/rock-olmo-training/v4/inverse_exp \
        --models inv=~/models/olmo2-1b-spectra-full/inverse_final,s1=~/models/olmo2-1b-spectra-full/stage1_final,base=~/models/OLMo-2-0425-1B \
        --out ~/tmp/analysis/inverse_exp/head_inverse.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from corpus_inverse import identify_prompt  # noqa: E402

import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.nn as nn  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

TOK = os.path.expanduser("~/models/OLMo-2-0425-1B")
MID_LAYER = 8


def load_items(corpus):
    docs = os.path.join(corpus, "docs")
    train = []
    for l in open(os.path.join(docs, "inverse_train.jsonl")):
        d = json.loads(l)
        if d["kind"] == "identify":
            sid = d["fact_id"].rsplit(":v", 1)[0]
            train.append({"prompt": d["prompt"], "species": d["species"], "sid": sid})
    probe = json.load(open(os.path.join(docs, "probe_items.json")))
    held = [{"prompt": identify_prompt(it["peaks"], it["laser"]), "species": it["species"], "peaks": it["peaks"]} for it in probe["heldout"]]
    rod = [{"prompt": identify_prompt(it["peaks"], it["laser"]), "species": it["species"], "peaks": it["peaks"]} for it in probe["rod"]]
    # peaks for the binned control of TRAIN prompts: parse back from the prompt text
    import re
    for t in train:
        t["peaks"] = [{"position_cm-1": float(a), "relative_intensity": float(b)} for a, b in re.findall(r"(\d+\.\d) \((\d\.\d\d)\)", t["prompt"])]
    return train, held, rod


def binned(peaks, lo=100, hi=1400, w=10):
    v = np.zeros((hi - lo) // w, dtype=np.float32)
    for p in peaks:
        i = int((p["position_cm-1"] - lo) // w)
        if 0 <= i < len(v):
            v[i] = max(v[i], p["relative_intensity"])
    return v


@torch.no_grad()
def encode(model_dir, prompts, tok, bs=48, device="cuda"):
    model = AutoModelForCausalLM.from_pretrained(os.path.expanduser(model_dir), dtype=torch.bfloat16).to(device).eval()
    tok.padding_side = "right"
    last, mean, mid = [], [], []
    t0 = time.time()
    for i in range(0, len(prompts), bs):
        enc = tok(prompts[i : i + bs], return_tensors="pt", padding=True, add_special_tokens=False).to(device)
        out = model(**enc, output_hidden_states=True)
        hs = out.hidden_states
        m = enc["attention_mask"].unsqueeze(-1).to(hs[-1].dtype)
        lens = enc["attention_mask"].sum(1) - 1
        h_last = hs[-1]
        last.append(h_last[torch.arange(h_last.shape[0]), lens].float().cpu().numpy().astype(np.float16))
        mean.append(((h_last * m).sum(1) / m.sum(1)).float().cpu().numpy().astype(np.float16))
        h_mid = hs[MID_LAYER]
        mid.append(((h_mid * m).sum(1) / m.sum(1)).float().cpu().numpy().astype(np.float16))
        if (i // bs) % 200 == 0:
            print(f"    encoded {i+len(prompts[i:i+bs]):,}/{len(prompts):,} [{time.time()-t0:.0f}s]", flush=True)
    del model
    torch.cuda.empty_cache()
    return {"last16": np.concatenate(last), "mean16": np.concatenate(mean), f"mean{MID_LAYER}": np.concatenate(mid)}


def train_head(Xtr, ytr, Xva, yva, n_cls, kind, device="cuda", epochs=40):
    mu, sd = Xtr.mean(0, keepdims=True), Xtr.std(0, keepdims=True) + 1e-6
    f = lambda X: torch.tensor((X - mu) / sd, dtype=torch.float32, device=device)
    Xt, Xv = f(Xtr), f(Xva)
    yt, yv = torch.tensor(ytr, device=device), torch.tensor(yva, device=device)
    d = Xt.shape[1]
    if kind == "linear":
        net = nn.Linear(d, n_cls)
    else:
        net = nn.Sequential(nn.Linear(d, 1024), nn.GELU(), nn.Dropout(0.2), nn.Linear(1024, n_cls))
    net = net.to(device)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3 if kind == "linear" else 5e-4, weight_decay=1e-4)
    best, best_state, bad = -1.0, None, 0
    for ep in range(epochs):
        net.train()
        perm = torch.randperm(len(Xt), device=device)
        for j in range(0, len(Xt), 1024):
            idx = perm[j : j + 1024]
            loss = nn.functional.cross_entropy(net(Xt[idx]), yt[idx])
            opt.zero_grad(); loss.backward(); opt.step()
        net.eval()
        with torch.no_grad():
            acc = (net(Xv).argmax(1) == yv).float().mean().item()
        if acc > best:
            best, bad = acc, 0
            best_state = {k: v.detach().clone() for k, v in net.state_dict().items()}
        else:
            bad += 1
            if bad >= 5:
                break
    net.load_state_dict(best_state)
    net.eval()

    def predict(X):
        with torch.no_grad():
            return net(f(X)).cpu()
    return predict, best


def topk(logits, y, ks=(1, 3, 10)):
    y = torch.tensor(y)
    order = logits.argsort(1, descending=True)
    return {f"top{k}": round((order[:, :k] == y[:, None]).any(1).float().mean().item(), 3) for k in ks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--models", required=True, help="name=DIR[,...]")
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0, help="cap train prompts (dev)")
    args = ap.parse_args()
    corpus = os.path.expanduser(args.corpus)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    train, held, rod = load_items(corpus)
    if args.limit:
        random.Random(0).shuffle(train); train = train[: args.limit]
    species = sorted({t["species"] for t in train})
    sidx = {s: i for i, s in enumerate(species)}
    ytr_all = np.array([sidx[t["species"]] for t in train])
    yh = np.array([sidx.get(t["species"], -1) for t in held])
    rod_seen = [t for t in rod if t["species"] in sidx]
    yr = np.array([sidx[t["species"]] for t in rod_seen])
    # validation split by SPECTRUM id (all variants of a spectrum stay together)
    sids = sorted({t["sid"] for t in train}); rng = random.Random(0); rng.shuffle(sids)
    va_sids = set(sids[: max(1, len(sids) // 20)])
    va = np.array([t["sid"] in va_sids for t in train]); tr = ~va
    print(f"train prompts {len(train):,} ({tr.sum():,} fit / {va.sum():,} early-stop) over {len(species):,} species; heldout {len(held)}, ROD seen {len(rod_seen)}/{len(rod)}", flush=True)
    results = {"n_species": len(species), "chance_top1": round(1 / len(species), 5), "features": {}}

    def run(name, Ftr, Fh, Fr):
        results["features"][name] = {}
        for kind in ("linear", "mlp"):
            t0 = time.time()
            predict, best = train_head(Ftr[tr], ytr_all[tr], Ftr[va], ytr_all[va], len(species), kind, device)
            res = {"early_stop_val_top1": round(best, 3), "heldout": topk(predict(Fh), yh), "rod_seen": topk(predict(Fr), yr), "seconds": round(time.time() - t0)}
            results["features"][name][kind] = res
            print(f"  {name:22} {kind:6} held-out top1 {res['heldout']['top1']:.3f} top3 {res['heldout']['top3']:.3f} top10 {res['heldout']['top10']:.3f} | ROD(seen) top1 {res['rod_seen']['top1']:.3f} top3 {res['rod_seen']['top3']:.3f} | es-val {best:.3f} [{res['seconds']}s]", flush=True)

    # control: binned peak vector
    print("== control: 10 cm-1 binned peak vector (130 dims) ==", flush=True)
    run("binned_vector", np.stack([binned(t["peaks"]) for t in train]), np.stack([binned(t["peaks"]) for t in held]), np.stack([binned(t["peaks"]) for t in rod_seen]))
    tok = AutoTokenizer.from_pretrained(TOK)
    for spec in args.models.split(","):
        mname, mdir = spec.split("=", 1)
        print(f"== {mname}: encoding {len(train)+len(held)+len(rod_seen):,} prompts ==", flush=True)
        feats = encode(mdir, [t["prompt"] for t in train] + [t["prompt"] for t in held] + [t["prompt"] for t in rod_seen], tok, device=device)
        n1, n2 = len(train), len(train) + len(held)
        for pool, F in feats.items():
            run(f"{mname}:{pool}", F[:n1].astype(np.float32), F[n1:n2].astype(np.float32), F[n2:].astype(np.float32))
        del feats
    if args.out:
        json.dump(results, open(os.path.expanduser(args.out), "w"), indent=1)
        print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
