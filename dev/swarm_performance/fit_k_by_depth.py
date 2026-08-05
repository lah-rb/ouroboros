import json, numpy as np, collections

R = "dev/swarm_performance/results/"
pts = []
lad = json.load(open(R + "decode_ladder.json"))
for r in lad["rows"]:
    pts.append((r["n"], 20 + lad["gen"] / 2, r["aggregate_tok_s"]))
for f in ("context_curve", "context_surface"):
    d = json.load(open(R + f + ".json"))
    for r in d["rows"]:
        pts.append(
            (r["n"], r["context_depth"] + d["gen"] / 2, r["decode_tok_s_aggregate"])
        )
# bucket by depth (within 10%)
buck = collections.defaultdict(list)
for n, dep, a in pts:
    key = min(buck, key=lambda k: abs(np.log(k / dep))) if buck else None
    if key is None or abs(np.log(key / dep)) > 0.10:
        key = round(dep)
    buck[key].append((n, dep, a))
k0, k1 = 0.515, -0.044  # from the global fit
print(
    f"{'depth':>7} {'cells':>5} {'N range':>10} {'k OBSERVED':>11} {'k MODEL':>9} {'verdict'}"
)
print("-" * 62)
for dep in sorted(buck):
    g = sorted(buck[dep])
    if len(g) < 2:
        continue
    n = np.array([x[0] for x in g], float)
    a = np.array([x[2] for x in g], float)
    k_obs = np.polyfit(np.log(n), np.log(a), 1)[0]
    k_mod = k0 + k1 * np.log(dep)
    v = (
        "OK"
        if abs(k_obs - k_mod) < 0.10
        else ("MODEL TOO HIGH" if k_mod > k_obs else "model low")
    )
    if k_obs < 0 < k_mod:
        v = "SIGN WRONG"
    print(
        f"{dep:>7} {len(g):>5} {int(n.min()):>4}-{int(n.max()):<5} {k_obs:>11.3f} {k_mod:>9.3f}   {v}"
    )
print("\npeak-N check (aggregate is not monotone in N at depth):")
for dep in sorted(buck):
    g = sorted(buck[dep])
    if len(g) < 4:
        continue
    best = max(g, key=lambda x: x[2])
    print(
        f"  depth {dep:>6}: aggregate peaks at N={best[0]:<3} ({best[2]:.1f} tok/s); "
        f"N={max(x[0] for x in g)} gives {[x[2] for x in g if x[0]==max(y[0] for y in g)][0]:.1f}"
    )
