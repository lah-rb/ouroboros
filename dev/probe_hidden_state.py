#!/usr/bin/env python3
"""Hidden-state probe, stage 1 evaluation — do gpt-oss's own hidden states beat
lexical features for reasoning-level routing (low vs medium)?

Trains class-weighted heads on the backfilled vectors (dev/hidden_states_v1.npz)
and evaluates on the SAME task-held-out split as every bake-off run (seed 0, 25%
tasks). Reference to beat (word+char TF-IDF union + LogReg on this split):
macroF1 .622 · cov@.93 = .747 · cov@.976 = .417.

Heads: class-weighted LogReg per feature variant (last_tok / tail_mean / concat),
plus a small ThinkSwitcher-style MLP (torch, weighted CE) on the best variant.
"""
import json
import random

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score
from sklearn.preprocessing import StandardScaler

REF = "word+char union reference: macroF1 .622 | cov@.93 .747 | cov@.976 .417"


def load():
    z = np.load("dev/hidden_states_v1.npz", allow_pickle=True)
    vec = {u: i for i, u in enumerate(z["uids"])}
    rows = [json.loads(l) for l in open("dev/train_dataset_trusted_v1.jsonl")]
    rows = [r for r in rows if r["uid"] in vec]
    tasks = sorted({r["task"] for r in rows})
    random.Random(0).shuffle(tasks)
    hold = set(tasks[: max(1, int(len(tasks) * 0.25))])
    tr = [r for r in rows if r["task"] not in hold]
    te = [r for r in rows if r["task"] in hold]
    feats = {"last_tok": z["last_tok"], "tail_mean": z["tail_mean"],
             "concat": np.hstack([z["last_tok"], z["tail_mean"]])}
    return rows, tr, te, vec, feats


def frontier(y, scores):
    nlow = y.count("low")
    best = {}
    for thr in np.unique(scores):
        p = np.where(scores >= thr, "medium", "low")
        rl = [i for i in range(len(y)) if p[i] == "low"]
        if not rl:
            continue
        prec = sum(1 for i in rl if y[i] == "low") / len(rl)
        cov = sum(1 for i in rl if y[i] == "low") / nlow
        for t in (0.93, 0.976):
            if prec >= t and cov > best.get(t, (0,))[0]:
                best[t] = (cov, prec)
    return best.get(0.93, (0, 0))[0], best.get(0.976, (0, 0))[0]


def report(name, y, pred, scores):
    f1 = f1_score(y, pred, average="macro")
    c93, c976 = frontier(y, scores)
    print(f"{name:36s} macroF1={f1:.3f} cov@.93={c93:.3f} cov@.976={c976:.3f}")
    return f1, c93, c976


def mlp_head(Xtr, ytr, Xte, dev="mps"):
    import torch
    torch.manual_seed(0)
    d = Xtr.shape[1]
    net = torch.nn.Sequential(
        torch.nn.Linear(d, 512), torch.nn.ReLU(), torch.nn.Dropout(0.3),
        torch.nn.Linear(512, 128), torch.nn.ReLU(), torch.nn.Dropout(0.3),
        torch.nn.Linear(128, 2),
    ).to(dev)
    yb = torch.tensor([1 if l == "medium" else 0 for l in ytr], device=dev)
    w = torch.tensor([1.0, (yb == 0).sum().item() / max(1, (yb == 1).sum().item())],
                     device=dev).float()
    lossf = torch.nn.CrossEntropyLoss(weight=w)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    X = torch.tensor(Xtr, dtype=torch.float32, device=dev)
    for ep in range(60):
        net.train()
        perm = torch.randperm(len(X), device=dev)
        for i in range(0, len(X), 64):
            idx = perm[i:i + 64]
            opt.zero_grad()
            loss = lossf(net(X[idx]), yb[idx])
            loss.backward()
            opt.step()
    net.eval()
    with torch.no_grad():
        logits = net(torch.tensor(Xte, dtype=torch.float32, device=dev))
        p = torch.softmax(logits, -1)[:, 1].cpu().numpy()
    return p


def main():
    rows, tr, te, vec, feats = load()
    y = [r["label"] for r in te]
    ytr = [r["label"] for r in tr]
    print(f"joined {len(rows)} turns | train {len(tr)} / test {len(te)}")
    print(REF + "\n")
    best = (None, -1, None, None)  # name, cov93, Xtr, Xte
    for name, F in feats.items():
        Xtr = np.stack([F[vec[r["uid"]]] for r in tr])
        Xte = np.stack([F[vec[r["uid"]]] for r in te])
        sc = StandardScaler().fit(Xtr)
        Xtr_s, Xte_s = sc.transform(Xtr), sc.transform(Xte)
        lr = LogisticRegression(class_weight="balanced", max_iter=3000, C=1.0)
        lr.fit(Xtr_s, ytr)
        mi = list(lr.classes_).index("medium")
        s = lr.predict_proba(Xte_s)[:, mi]
        _, c93, _ = report(f"hidden[{name}] + LogReg", y, lr.predict(Xte_s), s)
        if c93 > best[1]:
            best = (name, c93, Xtr_s, Xte_s)
    print(f"\nMLP on best variant ({best[0]}):")
    s = mlp_head(best[2], ytr, best[3])
    report(f"hidden[{best[0]}] + MLP", y, np.where(s >= 0.5, "medium", "low"), s)


if __name__ == "__main__":
    main()
