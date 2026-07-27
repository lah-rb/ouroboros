# Pre-registration — is the shared static prefix "free" for decode?

**Written BEFORE the run, 2026-07-26.** Recorded first so a wrong model cannot be
retrofitted into a right one afterwards — the same discipline that made the
prefill prediction useful when it was refuted (FINDINGS §4).

## The question (Luke)

> "With the static prompt, I wonder if we are measuring an effective floor. It
> would make a certain kind of sense if static content was free, pushing further
> towards the decode-dominated region. Rather than assume either way it would be
> easiest to supply an empty SOUL.md and see if it improves the decode picture
> or tracks what we already see."

## Why it is genuinely open

**Mechanically it could be free.** The batched engine forks the persona head with
`ctx.memory_seq_cp(head.seq, slot.seq, -1, -1)` (`batched_engine.py:881`). In
llama.cpp, `llama_memory_seq_cp` on the unified cache **adds a seq_id to existing
cells rather than duplicating K/V**. So the 1809-token prefix is ONE physical
copy shared by all N streams, while fresh tokens are N genuinely distinct
regions. If the attention kernel reads those shared cells once per step rather
than once per stream, static depth is nearly free and the real cost axis is
PRIVATE depth.

**Mechanically it could also not be.** gpt-oss is 72 KB/token of KV, so 1809
tokens ≈ **130 MB** — far beyond the M1 Ultra's ~48 MB system-level cache. Shared
cells cannot stay resident, so each stream's attention may stream them from HBM
regardless of physical sharing.

**Observational data cannot decide it.** Refitting all 62 measured cells with
depth defined as PRIVATE (total − 1809) instead of TOTAL gives MAPE 16.3% vs
8.2% — total fits better, weakly favouring "not free". But the two measures
differ by a constant, so log-linear regression is not a clean discriminator, and
the one row that would settle it (private ≈ 2800) was never tested above N=64.
Hence an intervention.

*(That refit also corrected an error of mine: the ladder rows have no
`context_depth` field and I had computed their depth as `20 + gen/2 = 148`,
ignoring the static prefix they also pay. Real depth ≈ 1957. `fit_optimizer.py`
carried the wrong value and is being fixed.)*

## Intervention

One line. `configs/gpt-oss-120b-a5-swarm-524k-nopersona.yaml` differs from the
production config **only** in `knowledge.tokens_bin`, pointing at
`data/nopersona.tokens.bin` = the first **8** tokens of the real file (8 rather
than 0 to avoid empty-file edge cases in the loader).

Static prefix: **1809 → 8 tokens.** Everything else — model, n_ctx 524288,
batched engine, seats, sizes, gen — held fixed.

Matched ladder: `--sizes 256,2048 --ladder 4,16,24,48,96 --gen 256`, directly
comparable to `results/swarm_regime_surface.json`.

At size 256 (fresh 188 tok, gen 256): **total depth 2253 → 452.**

## Predictions (stated in advance)

Depth elasticity measured *within* the regime surface, and the two arms at
size 256:

| N | elasticity dlnA/dlnD | H0 "free" (= baseline) | H1 "costs like fresh" |
|---|---|---|---|
| 4 | −0.424 | 82.3 | ~167 |
| 16 | −0.672 | 126.8 | ~387 |
| 24 | −0.938 | 154.8 | ~736 |
| 48 | −0.971 | 150.3 | ~752 |
| 96 | −1.413 | 198.7 | ~2073 |

**The H1 magnitudes are NOT credible as point predictions** — they extrapolate
~5× below the shallowest depth ever sampled (1957), and elasticity that steep
will not hold. They are recorded to show the arms are far apart, not to be
scored against.

**What is actually being tested is direction and order of magnitude:**

- **H0 — static is FREE:** aggregate essentially unchanged (within ~5%) at every
  N. Implication: the real cost axis is PRIVATE depth; the static floor is not a
  throughput tax; swarm sizing should be computed on private tokens only.
- **H1 — static costs like fresh:** aggregate rises substantially, and more at
  high N (elasticity grows with N). Implication: the 1809-token floor IS a real
  tax on every request, and trimming the knowledge prefix is a direct throughput
  lever.
- **H2 — partially amortized:** lands between. Implication: the depth axis must
  split into shared and private terms with a fitted weight
  `D_eff = D_priv + w·D_static`, and `w` is measurable from this one run.

## Falsifier

If aggregate at N=96 lands within 5% of 198.7, H1 is dead. If it exceeds ~400,
H0 is dead. Anything between 210 and 400 is H2 and yields `w` directly.

## Restore

`active_config.txt` returns to `gpt-oss-120b-a5-swarm-524k` and the server is
restarted on it. The nopersona config and tokens.bin are kept — this experiment
should be repeatable.
