import json, numpy as np
R="dev/swarm_performance/results/"

# EVERY request on this server carries the pre-tokenized knowledge prefix as KV
# depth: data/gpt-oss-120b-a5.tokens.bin is 7236 bytes = exactly 1809 int32
# tokens. Verified against the harness output: for size=256,
# context_depth(2253) - fresh_prompt(188) - gen(256) == 1809 exactly.
#
# The curve/surface harnesses report context_depth WITH the prefix already
# included. decode_ladder does NOT record a depth at all, and an earlier version
# of this script computed it as `20 + gen/2 = 148` — omitting the prefix and
# understating every ladder depth by 13x. That error propagated into the k(D)
# fit and into a regen hold-out that was scored at depth 566 instead of ~2375.
STATIC = 1809

pts=[]  # (N, mean_decode_depth, aggregate_tok_s, source)
lad=json.load(open(R+"decode_ladder.json"))
for r in lad["rows"]:
    # ~20-token prompt + the static prefix; depth grows 0->gen -> mean = +gen/2
    pts.append((r["n"], STATIC + 20 + lad["gen"]/2, r["aggregate_tok_s"], "ladder"))
for f in ("context_curve","context_surface","swarm_regime_surface"):
    d=json.load(open(R+f+".json"))
    for r in d["rows"]:
        pts.append((r["n"], r["context_depth"] + d["gen"]/2, r["decode_tok_s_aggregate"], f))
N=np.array([p[0] for p in pts], float); D=np.array([p[1] for p in pts], float)
A=np.array([p[2] for p in pts], float); S=[p[3] for p in pts]
print(f"combined dataset: {len(pts)} cells   N range {int(N.min())}-{int(N.max())}   depth range {int(D.min())}-{int(D.max())}")

# model: log A = a + k(D)*log N + b*log D,  k(D) = k0 + k1*ln D
X=np.column_stack([np.ones_like(N), np.log(N), np.log(N)*np.log(D), np.log(D)])
beta,*_=np.linalg.lstsq(X, np.log(A), rcond=None)
a,k0,k1,b = beta
pred=np.exp(X@beta)
err=100*(pred-A)/A
print(f"\nFIT  log A = {a:.3f} + ({k0:.3f} {k1:+.3f}·lnD)·lnN {b:+.3f}·lnD")
print(f"  k(D) = {k0:.3f} {k1:+.3f}·ln D   -> zero crossing at depth {np.exp(-k0/k1):,.0f}")
print(f"  in-sample MAPE = {np.abs(err).mean():.1f}%   max |err| = {np.abs(err).max():.1f}%")
print(f"\n{'src':16s} {'N':>4} {'depth':>7} {'obs':>7} {'pred':>7} {'err%':>7}")
for i in np.argsort(D):
    print(f"{S[i]:16s} {int(N[i]):>4} {int(D[i]):>7} {A[i]:>7.1f} {pred[i]:>7.1f} {err[i]:>+7.1f}")

# ---- HOLD-OUT: the corpus regen -------------------------------------------
GEN=1559528/4107      # mean generation tokens per request
PD =1544232/4107      # mean prompt tokens per request
d_reg = STATIC + PD + GEN/2   # the regen paid the static prefix too (was omitted)
lnD=np.log(d_reg); k=k0+k1*lnD
p_reg=np.exp(a + k*np.log(48) + b*lnD)
print(f"\nHOLD-OUT — corpus regen (c=48, elided previews):")
print(f"  mean prompt={PD:.0f} tok, mean gen={GEN:.0f} tok  -> mean decode depth={d_reg:.0f}")
print(f"  k({d_reg:.0f}) = {k:.3f}")
print(f"  PREDICTED aggregate at N=48: {p_reg:.1f} tok/s")
print(f"  OBSERVED  aggregate         : 120.0 tok/s")
print(f"  error: {100*(p_reg-120)/120:+.1f}%")
