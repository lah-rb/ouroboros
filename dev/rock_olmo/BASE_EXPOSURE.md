# Base-model exposure: chemistry and fill-in-the-middle across candidate bases

*Written 2026-09-21 during the §22 pass (PROCEDURE.md §22a–§22d). Numbers are
reproducible with `topic_census.py` in this directory and the Hub mix cards
cited below; raw outputs live in `~/tmp/analysis/v3/topic_census.json`.*

## Why this document exists

The v3 pass tried to teach OLMo-2-1B the fill-in-the-middle (FIM) sentinels
its tokenizer reserves but never trained. The primer reached a ceiling by
~16 M tokens that did not move with more tokens, a higher rate, a different
init or louder sentinel embeddings (§22a, §22b), and it cost 2–5 % on plain
text and up to 19 % on templated reference frames (§22c). That raised two
questions the corpus alone cannot answer:

1. How much chemistry / mineralogy did each candidate base see before we
   touched it? (The pile is 138 M tokens per epoch; the base's prior is what
   the forward recall of v2 leaned on.)
2. How much FIM material did each candidate base see, and where in its
   recipe?

The two quantities need different instruments. FIM exposure is a **recipe
quantity**: fill rate × tokens trained under it, read from the papers and
mix cards. Topic exposure is a **data quantity**: one fixed classifier over
uniform samples of every source, weighted by the published per-source token
counts. The same classifier on another corpus is the apples-to-apples
comparison.

## Method

**Classifier.** A lexicon of mineralogy and vibrational-spectroscopy terms
(Raman, infrared, wavenumber, cm-1, LIBS, XRD, mineral*, crystal*, lattice,
unit cell, space group, polymorph, common rock-forming minerals, anion
classes, spectroscop*, absorption band, vibrational mode, phonon). A document
counts at "≥ k distinct terms" within its first 20,000 characters (LaTeX
preambles and licence headers eat the first few thousand). ≥ 2 is mention
level; ≥ 4 is "domain-heavy". `replay.science_like` (≥ 3 generic science
keywords in the first 1,500 characters) is reported beside it as the replay
filter's view. A lexicon measures **mention, not depth**: a stub and a
review count the same.

**Samples.** 20,000 documents per source: the local dolmino shards for
pes2o, wiki and dclm (`~/corpora/rock-olmo-training/v4/replay/raw/data`), and
one Hub shard each for arXiv and for three StarCoder languages from
`allenai/olmo-mix-1124` (OLMo-2's pretraining mix, ungated). Single shards
are a limitation, noted per row.

**Per-source token counts.** OLMo-2's pretraining mix per the OLMo 2 report:
DCLM 3.71 T, StarCoder 83 B, pes2o 58.6 B, arXiv 20.8 B, OpenWebMath 12.2 B,
Algebraic Stack 11.8 B, Wikipedia 3.7 B (≈ 3.9 T). A byte census of the Hub
listing reproduces those shares within a point (dclm 96.5 %, pes2o 1.4 %,
starcoder 1.4 %, arxiv 0.3 %).

## Results: topic exposure in OLMo-2's sources

| source (share of pretraining) | science_like | ≥ 2 terms (docs) | ≥ 2 (chars) | ≥ 3 | ≥ 4 | ≥ 5 |
|---|---|---|---|---|---|---|
| pes2o (1.5 %) | 17.8 % | 17.5 % | 18.5 % | 9.9 % | 6.3 % | 4.4 % |
| DCLM web (95 %) | 7.3 % | 2.1 % | 4.0 % | 0.8 % | 0.4 % | 0.2 % |
| Wikipedia (0.1 %) | 1.7 % | 0.4 % | 0.9 % | 0.2 % | 0.1 % | 0.1 % |
| arXiv (0.5 %; LaTeX source, one 38 MB shard) | 1.9 % | 0.4 % | 1.8 % | 0.1 % | 0.0 % | 0.0 % |
| StarCoder Python (of the 2 % code slice) | — | 0.2 % | 0.6 % | 0.1 % | 0.0 % | 0.0 % |
| StarCoder C# / JavaScript | — | 0.1 / 0.0 % | 0.4 / 0.1 % | 0.0 % | 0.0 % | 0.0 % |

The arXiv row under-reads: the shard is LaTeX and the sample looked
math/CS-heavy; physics papers would score higher on a body-text window.

**Estimates (order of magnitude).**

- OLMo-2-1B pretraining: ≈ 25–30 B tokens of domain-heavy text (≥ 4 terms;
  ~0.7 % of 3.9 T), about three quarters of it inside the DCLM web slice, not
  the papers; ≈ 150 B at mention level. The midtraining mix (dolmino-1124;
  pool bytes dclm 89.5 %, pes2o 6.4 %, flan 2.0 %, math 1.5 %; the sampled
  mixes upweight pes2o to 9.5 % at 100 B and 19.4 % at 300 B) adds a few
  ×10⁸ more from pes2o.
- The v6 stage-1 pile (138 M tokens per epoch, essentially all domain) is
  ≈ 1 % of the base's domain-heavy exposure and ≈ 0.1 % of its mention-level
  exposure. Stage 1's job is to concentrate, not to introduce.
- A code corpus at the measured rates: StarCoder2-3B (The Stack v2, ~3.3 T
  tokens over 17 languages; the 3B and 7B did not get the Wikipedia / arXiv /
  OpenWebMath slices the 15B did) saw at most ~10⁸ domain-heavy tokens,
  mostly scientific-computing comments and docstrings. Two to three orders
  of magnitude less latent chemistry than OLMo-2.

## Results: FIM exposure per candidate base

| base | params | FIM in pretraining | FIM in midtraining | FIM tokens (≈) | sentinels |
|---|---|---|---|---|---|
| OLMo-2-0425-1B | 1.2 B | none | none | 0 | reserved, untrained (row norm 0.88 vs 10.8) |
| OLMo-2-1B + this pass | 1.2 B | — | our primer | 49 M (+3.6 M in stage 1) | same ids, still untrained rows |
| OLMo-3-1025-7B | 7 B | none | StackEdu (FIM) = 10.0 B tokens = 10 % of the 100 B dolmino-3 mix, FIM on ~52 % of documents | ≈ 5.2 B | **identical ids to OLMo-2** (100258 / 100259 / 100260; verified) |
| StarCoder2-3B | 3 B | rate 0.5 throughout | — | ≈ 1.65 T | `<fim_prefix>` 1, `<fim_middle>` 2, `<fim_suffix>` 3 (verified) |
| Qwen2.5-Coder-1.5B, DeepSeek-Coder-1.3B, CodeGemma-2B | 1.3–2 B | native | — | ~10¹¹–10¹² | family-specific strings (see `fim_transform.SENTINEL_SETS`) |

## The sentinel rows themselves: a three-point gradient

Measured directly from the embedding matrices (CPU read, 2026-09-21). Norms
are given relative to each model's own median trained-row norm, because the
three models normalise differently in absolute terms (OLMo-2 median 10.82 at
hidden 2048, OLMo-3 8.03 at 4096, StarCoder2 0.54 at 3072).

| base | FIM exposure | `<fim_prefix>` | `<fim_middle>` | `<fim_suffix>` | pairwise cos among the three |
|---|---|---|---|---|---|
| OLMo-2-1B | 0 | 0.08× | 0.08× | 0.08× | 0.02 / −0.02 / 0.05 (orthogonal) |
| OLMo-3-7B | ≈ 5.2 B (midtraining) | 0.30× | **0.32×** | 0.30× | 0.24 / 0.28 / 0.26 (a shared subspace) |
| StarCoder2-3B | ≈ 1.65 T (pretraining) | 0.93× | **1.63×** | 1.45× | 0.21 / 0.18 / 0.31 |

Three readings:

1. **Midtraining moves the rows but does not finish them.** OLMo-3's
   sentinels grew about 4× relative to their initialisation yet remain at a
   third of a normal token's amplitude after 5.2 B FIM tokens. Our 49 M-token
   primer moved OLMo-2's by 0.003 (§22b), which is what the same slope
   predicts at 1 % of the tokens.
2. **The three tokens acquire a shared direction.** At initialisation they
   are mutually orthogonal (cos ≈ 0.02, as random vectors in 2,048 dimensions
   are); after training they carry a common component (cos ≈ 0.25 in OLMo-3,
   0.18–0.31 in StarCoder2). A "this is FIM grammar" subspace is part of what
   gets learned, and it is not something rescaling can manufacture.
3. **`<fim_middle>` ends up the loudest of the three** in the base that
   learned FIM from scratch (1.63× against 0.93× for the prefix sentinel).
   That is the token at which the model must stop continuing the suffix and
   resume the prefix — the switch §22b was about. Amplitude is therefore a
   *correlate* of a working switch, not its cause: rescaling OLMo-2's rows to
   1.0× median changed nothing (§22b, PSM 0.179 vs 0.157), because the layers
   that would read a loud sentinel were never trained alongside one.

Two things follow from the OLMo-3 row.

- **OLMo 3 is the first OLMo whose sentinels were trained.** OLMo-2 and
  OLMo-3 share a tokenizer, so every FIM artefact of this pass (the
  transformation, the packer's atomic rule, the recovery probe, the XML
  records and their blanks) runs unchanged on OLMo-3. Whether 5.2 B tokens
  of code FIM in midtraining transfer to prose and to our records is exactly
  what the staged calibration probe measures (`probes_calibration.sh`, 402
  prose items, PSM and SPM, against our primer's PSM ≈ 0.18 / SPM ≈ 0.53).
- **It is ~100× the FIM exposure our primer could inject locally**, at a
  rate and scale (midtraining, code, 7 B) we cannot reproduce on two consumer
  cards. The 7 B also brings the parameter jump the operator noted, which is
  the other reason to weigh a run on the Mac.

## The OLMo-2 and OLMo-3 midtraining mixes side by side

From the dataset cards (`allenai/dolmino-mix-1124`,
`allenai/dolma3_dolmino_mix-100B-1025`).

| slice | dolmino-1124 (100 B mix) | dolma-3 dolmino (100 B) |
|---|---|---|
| high-quality web | ~50 % (dclm) | 22.5 % (Common Crawl HQ) + 5 % STEM-heavy crawl |
| scientific text | pes2o 9.5 % (real papers) | olmOCR science PDFs 5.0 % (real) |
| code | — | StackEdu 10 % (FIM) + CraneCode 10 % (synthetic Python) |
| math | ~10 % (dolmino math) | ~19 % (Dolmino Math, CraneMath, MegaMatt, TinyMATH; synthetic) |
| QA / instruction | flan ~5 % | ~14 % synthetic QA + Tulu 1.1 % + Flan 5 % |
| reasoning traces | — | ~9 % |
| synthetic share | small | ≈ 57 % |

The dolma-3 mix carries half the real-paper share of dolmino-1124 and more
than half synthetic content. For a replay stream that is meant to be "the
base's own distribution", the OLMo-2 replay (`replay.py`, pes2o-heavy) is the
cleaner instrument; for an OLMo-3 run the replay would have to be rebuilt
from the dolma-3 sources, and the STEM crawl plus olmOCR PDFs are the natural
science-biased slices.

## What the pass taught about fine-tune-scale FIM (for anyone repeating this)

- From a converged base, FIM acquisition on prose plateaus by ~16 M tokens at
  PSM copy-from-suffix ≈ 0.18 and SPM ≈ 0.53 on 1,500-character windows; the
  same ceiling from two inits, two rates, and with the sentinel embedding
  rows rescaled to a trained token's norm (§22a, §22b). The model fills from
  whichever side is adjacent to the generation point; the PSM "switch" is not
  installed at this scale.
- The rows themselves cannot move: Adam bounds a coordinate's drift by
  lr × steps (observed 0.003 over 1,500 steps). The skill lives in the layers.
- A 90 % fill rate on the base's own text costs ~2 % on held-out plain text
  and 5–19 % on the domain val sets, and decaying the rate to zero recovers
  none of it (§22a anneal). Bavarian et al.'s "FIM for free" is a
  from-scratch result; adapting a converged model is not free.
- Prose gives weak pressure to read the suffix (a suffix-continuation is
  usually plausible text); code gives strong pressure (it is syntactically
  wrong within a few tokens). That is why every successful FIM recipe in the
  literature is code-first, and why a code-shard primer is the one untested
  data-side lever (§22b follow-ups).

## Reproduce

```bash
cd dev/rock_olmo
./.venv/bin/python topic_census.py --docs 20000            # all five sources, fetches two small Hub shards
./.venv/bin/python topic_census.py --docs 20000 --only pes2o,dclm
# FIM calibration probes (needs the 3090 free):
bash /path/to/probes_calibration.sh                         # OLMo-3-7B + StarCoder2-3B on the 402 prose items
```

Mix cards: `allenai/olmo-mix-1124`, `allenai/dolmino-mix-1124`,
`allenai/dolma3_dolmino_mix-100B-1025` (README tables). StarCoder2 figures
from the StarCoder2 / The Stack v2 report; its Hub data listing is gated.
