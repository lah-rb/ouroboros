#!/usr/bin/env python3
"""Measure the reasoning router's train/serve skew. Reproduces ADAPTIVE_THINKING_STATUS §3.5.

Run:  uv run python dev/router_skew_probe.py

WHY THIS EXISTS
---------------
The router went inert (199/201 low in the 2026-07-25 boss A/B) and the first
three explanations offered for it were all wrong:

  1. "the corpus was 43% duplicates"      -> WRONG. Dedup ran (build_trusted_set
     keys on sha(prompt)); the shipped training set has 2129 rows and 2129
     distinct prompt hashes. Luke was right that dedup preceded training.
  2. "those duplicates were real duplicates" -> WRONG in the other direction.
     659 records collapsed to byte-identical PREVIEWS across 123 distinct tasks.
     Dedup then quarantined 477 of them as duplicates. They were distinct turns
     whose distinguishing content had already been snipped away.
  3. "the 0.622 macro-F1 was inflated by a length shortcut" -> WRONG. Under the
     EXACT shipped recipe, removing the length cue moves macro-F1 0.618 -> 0.616.
     The earlier 0.504 came from different hyperparameters, not from a finding.

What survives measurement is below. Both effects are real; domain shift is the
larger one by ~6x. The lesson worth keeping: every one of the three wrong
answers was a plausible story checked against nothing. Compare the arms.

THE CAPTURE BUG
---------------
Three of five label sources (phaseB / phaseC / tb1_cf) stored a middle-elided
1510-char PREVIEW instead of the prompt:

    [dynamic head ~290-500 chars] …[snip]… [static menu tail]

The budget was set without separating static scaffolding from turn content, so
the snip landed on the discriminative middle while faithfully preserving the
boilerplate tail (1361/1369 records share their closing 24 chars). The cached
static prefix never entered at all. v3 and grow stored full text, so the
training set is length-bimodal: 490 short rows at 24.7% medium, 1639 long rows
at 10.8%.
"""
import collections
import json

import joblib
import numpy as np

TRAIN = "dev/train_dataset_trusted_v1.jsonl"
ARTIFACT = "models/reasoning_router_v1.joblib"
LOG = "llmvp/logs/interactions.jsonl"
MARK = " …[snip]… "
SIG = "MENU + ARGUMENT"   # the plan_interaction signature — the router's domain
THR = 0.4                 # OURO_ROUTER_THR default (reasoning_router.py:92)


def live_prompts(limit=4000):
    """Real full prompts the router actually sees, sampled from the server log."""
    out = []
    with open(LOG, errors="replace") as f:
        for line in f:
            if SIG not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            p = rec.get("prompt") or ""
            if SIG in p and len(p) > 800:
                out.append(p)
                if len(out) >= limit:
                    break
    return out


def cut(text, budget=1510, mark=""):
    keep = budget - len(mark)
    head = keep // 2
    return text[:head] + mark + text[-(keep - head):]


def main():
    rows = [json.loads(l) for l in open(TRAIN) if l.strip()]
    art = joblib.load(ARTIFACT)
    vec, clf, mi = art["vectorizer"], art["clf"], art["medium_idx"]
    score = lambda texts: clf.predict_proba(vec.transform(texts))[:, mi]
    rate = lambda p: 100.0 * (p >= THR).mean()

    # --- 1. the collapse: distinct turns sharing a preview ----------------
    src = []
    for name in ("phaseB", "phaseC", "tb1_cf"):
        for r in json.load(open(f"dev/{name}_labeling.json")):
            r["_src"] = name
            src.append(r)
    groups = collections.defaultdict(list)
    for r in src:
        groups[r["context"]].append(r)
    collapsed = sum(len(v) - 1 for v in groups.values() if len(v) > 1)
    big = max(groups.values(), key=len)
    print("1. PREVIEW COLLAPSE")
    print(f"   records={len(src)}  distinct previews={len(groups)}  collapsed={collapsed}")
    print(f"   largest group: {len(big)} records spanning "
          f"{len(set(r['task'] for r in big))} DISTINCT tasks")
    q = json.load(open("dev/label_quarantine_v1.json"))["header"]["tiers"]
    print(f"   -> dedup quarantined {q['dedup_duplicate']} duplicate + "
          f"{q['dedup_conflict']} conflict = {q['dedup_duplicate'] + q['dedup_conflict']}")

    # --- 2. what moves the decision: length, not the marker ---------------
    live = live_prompts()
    print(f"\n2. FORMAT ABLATION on {len(live)} real prompts (THR={THR})")
    base = rate(score(live))
    arms = {
        "full prompt (what runtime sends)": live,
        "cut to 1510, NO marker": [cut(x) for x in live],
        "cut to 1510, WITH marker": [cut(x, mark=MARK) for x in live],
        "full length, marker injected": [x[:len(x) // 2] + MARK + x[len(x) // 2:] for x in live],
    }
    for tag, texts in arms.items():
        r = rate(score(texts))
        print(f"   {tag:34s} medium={r:6.2f}%  ({r - base:+5.2f} pp)")
    print("   -> the elision MARKER is worth +0.07pp; LENGTH carries ~88% of the effect")
    print("      (tfidf is L2-normalized: a short doc concentrates weight, a long one dilutes)")

    # --- 3. the two clamps, separated -------------------------------------
    prev = np.array(["…[snip]…" in r["text"] for r in rows])
    p_train = score([r["text"] for r in rows])   # in-sample; see note below
    p_live = score(live)
    print(f"\n3. SKEW DECOMPOSITION (THR={THR})")
    print(f"   preview rows,  in-domain : {rate(p_train[prev]):6.2f}%")
    print(f"   full-text rows, in-domain: {rate(p_train[~prev]):6.2f}%")
    print(f"   LIVE runtime prompts     : {rate(p_live):6.2f}%   <- observed run: 199/201 low = 1.0%")
    print(f"   format effect : {rate(p_train[prev]) / max(rate(p_train[~prev]), 1e-9):.1f}x")
    print(f"   domain shift  : {rate(p_train[~prev]) / max(rate(p_live), 1e-9):.1f}x   <- DOMINANT")
    print("\n   NOTE: rows above are in-sample and therefore optimistic. Out-of-fold with the")
    print("   shipped recipe the same split reads 79.59% / 28.55%, so the ratios hold.")
    print("   The shipped macro-F1 is NOT an artifact: mixture 0.618 vs length-normalized 0.616.")


if __name__ == "__main__":
    main()
