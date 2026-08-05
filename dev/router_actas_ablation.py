import json, re, numpy as np, joblib, collections

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
# where does the ACT AS block end?
ends = collections.Counter()
for p in live[:400]:
    i = p.find("---ACT AS---")
    if i < 0:
        continue
    m = re.search(r"\n(?====|## |---[A-Z])", p[i + 12 :])
    ends[p[i + 12 :][m.start() : m.start() + 28].strip()[:24] if m else "<none>"] += 1
print("first section marker AFTER the ACT AS block:")
for k, v in ends.most_common(5):
    print(f"   {v:4d}  {k!r}")


def strip_actas(p):
    i = p.find("---ACT AS---")
    if i < 0:
        return p
    m = re.search(r"\n(?====|## |---[A-Z])", p[i + 12 :])
    return p[:i] + (p[i + 12 :][m.start() + 1 :] if m else "")


a = joblib.load("models/reasoning_router_v1.joblib")
vec, clf, mi = a["vectorizer"], a["clf"], a["medium_idx"]
sc = lambda xs: clf.predict_proba(vec.transform(xs))[:, mi]
rate = lambda p: 100 * (p >= 0.4).mean()
base = sc(live)
stripped = [strip_actas(p) for p in live]
s = sc(stripped)
L0 = int(np.median([len(x) for x in live]))
L1 = int(np.median([len(x) for x in stripped]))
print(f"\nABLATION on {len(live)} live prompts (THR=0.4):")
print(
    f"  as sent            median={L0:5d} chars   medium={rate(base):6.2f}%   mean p={base.mean():.4f}"
)
print(
    f"  ACT AS block removed median={L1:5d} chars  medium={rate(s):6.2f}%   mean p={s.mean():.4f}"
)
print(
    f"  >> swing: {rate(s)-rate(base):+.2f} pp   (block is {L0-L1} chars = {100*(L0-L1)/L0:.0f}% of the prompt)"
)
