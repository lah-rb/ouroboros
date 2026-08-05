import json, collections, numpy as np

rows = [json.loads(l) for l in open("dev/train_dataset_trusted_v1.jsonl") if l.strip()]
v3 = [r["text"] for r in rows if r["source"] == "v3"]
pv = [r["text"] for r in rows if r["source"] != "v3"]
SIG = "MENU + ARGUMENT"
live = []
with open("llmvp/logs/interactions.jsonl", errors="replace") as f:
    for line in f:
        if SIG not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        p = r.get("prompt") or ""
        if SIG in p and len(p) > 800:
            live.append(p)
            if len(live) >= 4000:
                break


def prof(name, xs):
    L = [len(x) for x in xs]
    heads = collections.Counter(x[:24] for x in xs)
    print(
        f"\n{name}  n={len(xs)}  chars median={int(np.median(L))} p10={int(np.percentile(L,10))} p90={int(np.percentile(L,90))}"
    )
    for h, c in heads.most_common(3):
        print(f"    {100*c/len(xs):5.1f}%  {h!r}")
    for mark, label in (
        ("---ACT AS---", "ACT AS block"),
        ("=== MENU + ARGUMENT ===", "MENU header"),
        ("## Session history", "session history"),
        ("## Options", "options block"),
        ("You are Ouroboros", "SOUL persona"),
        ("---TEST CHARTER---", "charter"),
    ):
        print(f"      {label:18s}: {100*sum(1 for x in xs if mark in x)/len(xs):5.1f}%")


prof("TRAIN v3 (full text)", v3)
prof("TRAIN previews (phaseB/C/tb1)", pv)
prof("LIVE runtime prompts", live)
