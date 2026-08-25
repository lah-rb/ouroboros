# rock_olmo — build procedure

How the spectral training corpus was produced, in the order it was
actually done, with the measured figures and the decisions behind them.
Written to be re-runnable: a later pass should be able to follow this
and get the same corpus, or diverge deliberately rather than by
accident.

Corpus-side prep (extraction, curation, packing) is a separate
checklist: `~/tools/ouroboros-ops/PRETRAIN_PREP.md`. This document
starts where that one ends — with packed, grounded artifacts on disk.

---

## 0. Prerequisites

| input | state required | as of 2026-08-24 |
|---|---|---|
| `~/corpora/ouroboros-spectra/databank/dataset/*.json` | curator-accepted, packed, grounded | 1,199 artifacts / 1,190 accepted papers, 100% packed |
| `~/corpora/mineral-refs/rruff/*.zip` | Raman spectra + metadata | 11,415 files, 2,135 species |
| `~/corpora/mineral-refs/ecostress/ecospeclib-all/` | reflectance spectra + headers | 6,143 files, 190 mineral species |
| `~/corpora/mineral-refs/mindat/minerals_ima.jsonl` | IMA names + formulas | 6,239 species, 100% with a formula |
| `~/corpora/mineral-refs/nist_asd/asd_*.tsv` | atomic emission lines | 92 elements |
| `~/corpora/mineral-refs/sshade/sshade_bandlist.csv` | molecular-ice bands | 68 rows (separate domain) |

---

## 1. Corpus recovery (done before any serialisation)

Recovering data the pipeline had already read and discarded, because
serialising a corpus with known holes bakes them in.

1. **Repack** — 117 packs whose review summary cited peak values their
   pack never stored. 96 merged, +1,799 spectral points.
2. **Wide repack** — all 688 packs with no peak values at all. 335
   merged, **+9,780 points**. 332 correctly returned empty (the paper
   genuinely reports none); 19 rejected on grounding.
3. **Envelope recovery** — 82 accepted papers with no artifact, blocked
   by a `doi|arxiv_id` requirement that excludes institutional
   repositories and regional journals. All 82 packed with best-available
   identifiers (`identifier_type` + `identifier_evidence` recorded).
4. **Stale flags** — 14 records reading `pack_failed` while holding
   valid, grounded artifacts. Bookkeeping only.
5. **Licences** — 32 of 370 resolved from OpenAlex/Crossref metadata
   (9%; text carries ~18%, so metadata is the *weaker* source here).

Net: spectral points **3,984 → 13,055 (3.3x)**; papers with measured
peaks **380 → 798 (34% → 67%)**; accepted corpus 100% packed.

Three framework fixes came out of this and are in Ouroboros, not here:
the grounding gate's decimal-comma blindness (`63b6c71`), the pack
prompt's unit-conversion demonstration and peak-payload framing
(`0061d5b`, `0f48cb2`).

---

## 2. Canonical field schema

```bash
cd dev/rock_olmo && ../../.venv/bin/python -m pytest tests/ -q
```

`training_form.canonicalize()` — units normalised, `_min`/`_max` folded
to range objects, `*_as_packed` retaining the verbatim value.

**Runs at serialisation, never on the packs.** Rewriting `3.551` (MHz)
as `3551000` (Hz) inside an artifact would destroy the property
`grounding_check` enforces: the number would stop being findable in the
paper. Measured: 3,802 → 3,585 keys, zero artifacts losing a numeric
leaf.

Collapse is smaller than it looks because units are not where the sprawl
is — 93% of keys are used exactly once and carry real domain
specificity. The stable core is ~58 keys covering 77% of uses; the
singleton tail serialises as prose.

---

## 3. Held-out species

Stratified on **recurrence** (distinct papers mentioning the species)
AND **compositional complexity** (elements in the IMA formula, hydration
and solid solution flagged). 30 of 352, balanced 15 simple / 15 complex
across five recurrence bands.

- **Species, not papers.** The same fact appears in a paper, a RRUFF
  spectrum, an ECOSTRESS signature, a mindat formula and NIST lines
  derived from that formula. Only removing a species closes every view.
- **Quartz protected** — corpus-wide internal standard and substrate;
  removing it degrades every paper that merely mentions it.
- **Native-element minerals excluded as candidates** by rule, not by
  list: "Palladium" and "Copper" name a species AND an everyday
  material, so a corpus mention is as likely to be a catalyst.
- **The price is paid once** for this demo baseline, not repaid until
  there is something to publish.

---

## 4. Reference layer

`reference_layer.py`. Fact and derivation kept apart, deliberately:

- **FACT** — RRUFF ideal/measured chemistry, cell parameters, crystal
  system, locality, confirmation status; ECOSTRESS class/subclass,
  particle size, wavelength range; mindat formula; NIST observed line
  wavelengths.
- **DERIVED** — RRUFF publishes *spectra*, not peak lists, so any peak
  attributed to it is our peak-pick and travels as
  `derivation="peak_pick"`.

**Reflectance needs troughs, not peaks.** A Raman spectrum's information
is in emission maxima; a reflectance spectrum's is in absorption minima.
Separate function, because a maxima-finder over reflectance data returns
continuum shoulders and misses every band. Depth threshold 2%,
calibrated on a real mimetite spectrum (5% → 3 features, 1% → 42 noise,
2% → 16).

Both pickers **select by strength, present in spectral order** — ranking
output by intensity asserts a diagnostic judgement a local-extremum
finder cannot make, and edge artifacts ranked first.

---

## 5. The mindat → NIST bridge

`species → IMA formula → elements → ASD emission lines`. 350 of 352
corpus species have *every* element present in ASD. This makes NIST a
derived view rather than a disconnected 119k-line table, and it is how
LIBS identification actually works, so the derivation is the lesson.

> An earlier element parser used a `(?<![a-z])` lookbehind, which blocked
> any symbol following another symbol's lowercase letter: `CaSiO3` parsed
> as `{Ca}` and `CaCO3` as `{Ca, O}`. Every formula was silently
> understated. Rewritten to tokenise `[A-Z][a-z]?` against the element
> set.

---

## 6. Tolerance for reported-vs-reference

Set from **both** a corpus measurement and field practice, because
either alone is a guess.

- **Measured**: 22,913 paper-reported Raman values paired against the
  nearest RRUFF peak-pick. Bin-to-bin decay runs 0.51, 0.76, 0.81, 0.83,
  0.80, 0.79 through bin 6 then **flattens** (0.94, 0.97, 0.88, 0.94,
  1.14). Genuine agreement is below ~7 cm⁻¹; beyond that is coincidence
  at ~1% of pairs per bin.
- **Field**: calibrated instruments hold sub-1 cm⁻¹ accuracy; realistic
  drift reaches ±9; **±10 is the conventional comparison tolerance**.

| gap | framing |
|---|---|
| ≤1 | inside instrument accuracy — "the two agree" |
| ≤3 | reporting precision |
| ≤10 | accepted tolerance; drift OR a real shift |
| >10 | **gated out entirely** — probably a different mode |

The reference grounds; it does not adjudicate. Tested explicitly.

---

## 7. Interconnects

`interconnect.py`, five views. Repetition teaches the surface form; four
framings of one fact cost the same tokens and each exercises a different
retrieval direction.

| view | why not redundant |
|---|---|
| forward | the direction literature supplies |
| **inverse** | the actual working task; forward prose does not teach it |
| cross-modal | joint signature — 97 species carry Raman + TIR + paper, 59% of all species mentions |
| contrastive | polymorphs separable only by band detail |
| corroboration | the only view using both corpora |

---

## 8. Assembly

```bash
cd dev/rock_olmo && ../../.venv/bin/python -c \
  "import assemble, json; r = assemble.assemble(); json.dump(r['records'], open('records.json','w'))"
```

**Deterministic, ~8 seconds, no inference.** 3,206 records / ~162k
tokens: 1,686 paper-backed over 352 species, 1,520 reference-only capped
at parity and selected for mineral-class breadth.

**Views-as-weight**: a species emits every view it can support and no
more, so exposure follows evidence rather than a target. Nothing is
padded to hit a multiplier. Surface form varies by stable hash over
several phrasings per view.

Bugs that only surfaced on running it, kept here because each would
recur: `cross_modal` emitted zero twice (ECOSTRESS features never
derived; then `_fmt_peaks` hardcoding `position_cm-1` and formatting
micrometre troughs to empty); corroboration produced 79% of records
until capped; ECOSTRESS TIR runs a descending wavelength axis which
disabled de-duplication.

---

## 9. Emitter

```bash
cd dev/rock_olmo && ../../.venv/bin/python -c \
  "import emit; print(emit.emit_corpus('<out_dir>', shards=8))"
```

| | |
|---|---|
| train records | 4,456 -> **14,713 weighted**, ~1.15M tokens |
| interconnect / paper / NIST-LIBS / SSHADE | 3,035 / 1,037 / 292 / 92 |
| holdout | 322 records, written separately |
| shards | 8, balanced at 1,839 each |

**THE HOLDOUT IS A WRITE BARRIER, not a report.** The emitter refuses to
write if a held-out species appears in any train record's rendered text.
It refused three times and each was a real leak with a different cause:
contrastive siblings naming held-out species while training on a third;
paper TITLES used as citations ("Raman spectroscopic study of azurite
and malachite"); and the check reading `title+data` while the record
emitted `summary+facts`, so a review summary leaked past it. Any of the
three would have shipped a silently contaminated eval.

Matching is on WORD BOUNDARIES — bare substring flagged
"Natrojarosite" as leaking "Jarosite", a different species whose name
merely contains a held-out one.

---

## 10. Mix ratio — DECIDED

Weighting is per RECORD, not per token, so the interconnects land at
roughly **20% of the training mix** (3,035 of 4,456 unweighted records,
but the paper text carries far more tokens per record). An earlier
token-only estimate put them at 0.8%, which was the wrong denominator.

**Operator ruling 2026-08-24: 20% stands for this pass.** Reasonable on
its face, and the point of the run is to see what it produces. This is
the dominant lever on whether the model learns to *do* spectroscopy
rather than merely know facts about it, so it is the first number to
revisit if the eval disappoints — upward via more views per species
(per-sample records, more techniques, more sibling groups), not via more
copies of the same views.

---

## 11. Run 1 result — FORMAT LEARNED, FACTS PARTLY LEARNED

Read this before designing run 2. The loss curve looks like a success and
the generations show it is not.

### The numbers

| | eval loss | ppl |
|---|---|---|
| baseline (base model) | 1.8499 | 6.36 |
| epoch 1.25 | 1.6785 | 5.36 |
| **epoch 2.50** | **1.6692** | **5.31** ← best |
| epoch 3.12 | 1.6696 | |
| epoch 3.75 | 1.6698 | |

**-9.77% on held-out species.** OLMo 2 1B, LoRA r32 on attention + MLP
(24.1M trainable), corpus v2, stopped at epoch 3.75 on two consecutive
eval rises. Best adapter and full history in
`~/models/olmo2-1b-spectra-lora`.

### What generation actually shows

```
Quartz   shows Raman bands at -> 152.1, 163.1, 182.8, 195.5, 205.3, 216.3
Hematite shows Raman bands at -> 152.1, 163.1, 182.8, 195.5, 205.3, 216.3
```

IDENTICAL output for different minerals. The model is not conditioning
on the species: it learned "emit an ascending comma-separated list of
three-digit numbers". Against known bands — Quartz 1/4, Hematite 1/5,
Calcite 0/4, and those hits are coincidence from dense sequences. Asked
what has bands at 1085/712/282 (textbook calcite) it answered
"Kainosite-(Y)".

The flat eval curve was consistent with this the whole time: the
template was learned in epoch 1 and there was nothing further to gain.

### Correction: it is NOT all format

This section first concluded "format acquisition, not spectroscopy".
CPU probing of the adapter (`probe_digits.py`) shows that is too blunt
and the sharper reading changes what to do next.

**Species -> formula WAS genuinely learned: 7 of 10 probed species
answered correctly at p ~ 1.0**, with the digit distribution collapsed
to near-zero entropy on the right tokens. So the run did acquire facts;
it acquired the wrong KIND of fact, and the difference is not exposure
COUNT but exposure CONSISTENCY:

| | exposures | targets |
|---|---|---|
| species -> formula | ~12 | ONE constant string |
| species -> bands | ~8 | 2-4 COMPETING lists |

A formula is the same token sequence every time it appears. Band lists
differ between sources — different instruments, different pick
thresholds, different truncations — so the same prompt has several
mutually inconsistent continuations in the corpus and the model's best
cross-entropy play is to hedge toward the corpus-wide positional
average. That is exactly the ascending generic sequence observed.

This reframes run 2. Adding exposures does not help if the exposures
disagree; the fix is either to make the target single-valued (canonical
band list per species) or to stop asking for the answer as TOKENS at
all. §13 takes the second route.

### Why

Each species appears in **2-8** interconnect records, and those records
are **4.6% of tokens** — while the TEMPLATE appears in ~12,000 weighted
records. Format is massively reinforced; each individual fact gets a
handful of examples. A 1B model with a LoRA and 10.5M tokens learns the
former and not the latter.

### What this implies for run 2

The problem is NOT the interconnect ratio. It is EXPOSURE PER FACT, and
more copies of the same views cannot fix it:

- more DISTINCT records per species — per-sample rather than
  per-species records, more techniques, more sibling groups
- SSHADE per-band VOTables mirrored (only the 68-row catalogue is used)
- ECOSTRESS VSWIR is indexed for 156 species but only reaches
  cross-modal where Raman also exists
- or accept that factual recall needs a larger base model, and treat
  the 1B as a style/format demonstrator

(Superseded in part by the correction above: the capacity ceiling is
real but is NOT what produced the identical-output symptom.)

The corpus itself is sound — grounded, leak-verified, 13,055 spectral
points. This is a finding about model capacity and data density.

### Three eval bugs found during the run

Each would have produced a confident wrong conclusion:

1. **The eval set was the first 400 holdout records**, which is 80%
   templated material against a holdout that is 84% markdown. It scored
   the model on what it learns fastest. Fixed to stratified sampling —
   and the honest baseline moved 2.7979 -> 1.8499, so every comparison
   against the old number was meaningless.
2. **Resuming carried `best_metric` from the old eval set.** New evals
   scored ~1.85 against an inherited 1.4998 on a different scale, so no
   new best would ever be recorded and `load_best_model_at_end` would
   have shipped the 0.6-EPOCH checkpoint while the logs showed five
   epochs of training.
3. **Killing the run means `load_best_model_at_end` never fires.** The
   best checkpoint has to be selected and consolidated by hand.

The root cause of 1 and 2 is the same: THE MEASURING INSTRUMENT CHANGED
MID-EXPERIMENT. Fixing the eval was right, but a changed metric
invalidates every comparison crossing the change — baseline,
checkpoint bookkeeping, and any pre-registered expectation.

---

## 12. Remaining before a training run

- **Shards must live somewhere durable.** The build writes wherever it
  is pointed; a scratch directory is lost with the session.
- OLMo 2 1B pulled to `~/models` with the HF token.
- The 3090 freed — LLMVP holds it while the scraper runs.
- SSHADE per-band VOTables are NOT mirrored (only the 68-row
  catalogue), so no ice band positions are asserted. Mirroring them is
  the cheapest available enrichment if the ices matter more later.
- ECOSTRESS VSWIR is under-used: 156 species have reflectance troughs
  indexed but the cross-modal view only reaches those that also have
  Raman.

---

## 13. Run 2 — the AtomGPT shape: a regression head

Run 1 asked a 1B model to emit band positions as digits. §11 shows why
that cannot work here: the same prompt has several mutually inconsistent
continuations in the corpus, so hedging toward a generic ascending
sequence is the model's best cross-entropy play. AtomGPT does not ask
for digits — for numeric properties it attaches a regression head to the
transformer. Run 2 is that, with a 128-bin spectrum in place of a scalar.

### The target

`spectra_dataset.py` bins each measured Raman curve onto a fixed grid:
100–1200 cm⁻¹, 128 bins, max-pooled per bin, peak-normalised, and a
spectrum is dropped unless it covers 60% of the grid. **1,952 species /
9,655 spectra**, at `~/corpora/rock-olmo-training/spectra_binned.json`.

Three properties matter, and all three are repairs of run 1's failure mode:

- **fixed length** — no variable-length set to order, no sampling
- **the measured curve IS the target** — the peak-picker is out of the
  training path entirely, so a bad pick cannot teach a wrong fact
- **inconsistent sources average rather than compete** — two spectra of
  one mineral are two training rows, not two contradictory answers

The split is BY SPECIES. Two spectra of one mineral on opposite sides of
the split would leak the answer.

### The failed first objective, and the lesson

Run 2a copied AtomGPT's L1 directly. It reported **val 0.0747 against a
"trivial" 0.1013 — an apparent 26% win. It was fabricated.** The head had
converged to ALL ZEROS: pairwise L1 between its predictions for Quartz,
Calcite, Gypsum, Hematite, Pyrite and Diamond was 0.00000 to five
decimals, every output bin 0.000.

The cause is the target, not the model. A peak-normalised Raman spectrum
is SPARSE — flat baseline, a few narrow bands. Under L1 the optimal
constant is the per-bin MEDIAN, and for sparse bins that median is ~0. So
"predict nothing" is a real L1 minimum. It also beat the per-bin mean
(median 0.0802 vs mean 0.1013), and quoting the MEAN as the trivial
baseline is what made a degenerate head look like a 26% win.

AtomGPT regresses DENSE scalars — formation energy, band gap. L1 is
correct there and misleading here. **Copying an architecture does not
carry over its loss function; the loss belongs to the target's
distribution.**

Two durable rules:

1. **Under L1, the trivial baseline is the MEDIAN, not the mean.**
2. **A metric a degenerate predictor can win is not a metric.** Check
   that outputs VARY WITH INPUT before believing any headline number.
   Every eval here now reports `self_sim` — mean pairwise cosine between
   the model's own predictions — which pins at 1.0 the moment the head
   stops conditioning on its input, visible from epoch 0.

### The objective now

Cosine distance, equivalently spectral angle (SAM) — what spectroscopy
uses to compare spectra and what mineral-matching libraries score on. It
is scale-invariant, and undefined for the zero vector, so run 2a's
collapse is not reachable. The head's final activation moved sigmoid →
softplus for the same reason: under a scale-invariant loss the output
scale is free, and sigmoid's saturation only costs gradient near the band
peaks, which are the bins carrying the information.

**Bounds, measured before training so the result can be judged:**

| | cos | SAM (rad) | meaning |
|---|---|---|---|
| two different minerals | 0.2316 | 1.3313 | floor |
| predict the corpus mean | 0.4600 | 1.0856 | **trivial — must beat** |
| same mineral, two measurements | 0.8874 | 0.4054 | **ceiling — measurement noise** |

The usable span is 0.4600 → 0.8874. Results are quoted as percent of
THAT gap closed, not as raw cosine, because raw cosine flatters: a
predictor that has learned nothing still scores 0.46.

### The control that decides whether this is real

`retrieval_baseline.py`. A head mapping "mineral X, composition Y" to a
spectrum can succeed two ways: by learning something about how
composition and bonding produce vibrational bands, or by learning that
minerals with similar formulas have similar spectra — which is true,
useful, and **needs no model at all**. Only the second is measured by
val_cos alone.

So: parse each formula to an element-count vector and copy the mean
spectrum of the k nearest training minerals by composition cosine.
Deterministic, CPU, no training, same splits and same seed. If the LLM
head cannot beat this, the LLM is decoration.

### Where the compute went

The 3060 shares 12 GiB with paddle (7.9 GiB resident), and the 3090 is
full with muse. The frozen arm therefore runs on **CPU**, which costs
almost nothing: there are only 1,952 unique prompts, they embed once, and
the rest is an MLP. Embeddings are memoised per species — without that,
~80% of every epoch recomputes a frozen answer that cannot have changed.
The contended GPU is left to the LoRA arm, which cannot memoise because
its backbone weights move.

### Results

Three species-split seeds, frozen backbone, 40 epochs. Quoted as the
**mean of the last 10 epochs**, not `argmax(val)` — the val curve is flat
and noisy (±0.015 between adjacent epochs), so the argmax reports the
noise. The first write-up of this run said 0.6137 / +36% on exactly that
mistake; the honest number is 0.5899.

| | val (random species) | holdout (stratified) |
|---|---|---|
| trivial | 0.4600 | 0.4600 |
| retrieval control | 0.5764 ± 0.0129 | 0.4990 ± 0.0087 |
| **frozen head** | **0.5899 ± 0.0238** | **0.5668 ± 0.0072** |
| ceiling | 0.8874 | 0.8874 |

Paired head-minus-retrieval, per seed:

- val **+0.0135 ± 0.0142**, t = 1.6 — **within noise**
- holdout **+0.0678 ± 0.0100**, t = 11.8 — **significant**

So on randomly chosen minerals the head is not distinguishable from
element-count lookup. On the stratified holdout it is, closing 25.0% of
the trivial→ceiling gap against retrieval's 9.1%.

### LoRA changes nothing

Unfreezing the backbone (r16 on q/k/v/o, 4.19M trainable, 0.33%) on the
same seed: val 0.5924, holdout 0.5531, against the frozen arm's 0.5792 /
0.5588. Both differences are inside the seed-to-seed spread.

**The frozen representation was never the bottleneck.** That rules out
the cheapest explanation for the gap to the ceiling and means more
adapter capacity is not the lever.

### Where the advantage actually lives — NOT where predicted

The holdout gap looked like it should be concentrated on POLYMORPHS:
minerals sharing a formula but not a structure (Anatase/Rutile/Brookite
TiO2, Marcasite/Pyrite FeS2, Atacamite/Botallackite Cu2Cl(OH)3). Raman
reads bonding geometry, not stoichiometry, so their spectra differ
completely and composition retrieval must fail on them BY CONSTRUCTION —
it scored 0.207 on Anatase, below the 0.2316 floor for two randomly
chosen unrelated minerals. A model carrying real structural knowledge
should win exactly there.

It does not. Splitting the holdout (`eval_head.py`):

| holdout subset | head | retrieval | delta |
|---|---|---|---|
| polymorph (same-formula twin in train) | 0.4721 | 0.4991 | **−0.0269** |
| no composition twin | 0.5482 | 0.4653 | **+0.0829** |

The head is slightly WORSE than retrieval on polymorphs and scores 0.231
on Anatase — the unrelated-minerals floor. **It cannot tell Anatase from
Rutile.** The entire advantage is on species with no exact composition
twin, where retrieval must extrapolate and the head interpolates more
smoothly.

**Conclusion: this is a better interpolator over composition space, not
a carrier of structural knowledge.** The prediction was wrong in the
informative direction, and it was worth making explicitly — the
aggregate holdout number alone would have supported the flattering
reading indefinitely.

(Per-species numbers here are unweighted by spectrum count, so all-holdout
reads 0.5165 against the training log's per-spectrum 0.5588. Both are
correct; they weight differently. Do not mix them in one table.)

### The prompt ablation — the name is worse than useless

Four prompt contents, two seeds, frozen backbone. `none` is a constant
prompt: the negative control, which must land at trivial or the whole set
is void.

| prompt content | val | holdout |
|---|---|---|
| **formula only** | **0.6308** | **0.5524** |
| name + formula | 0.5851 | 0.5453 |
| name only | 0.4101 | 0.4258 |
| none (negative control) | 0.4599 | 0.4314 |
| trivial | 0.4600 | 0.4600 |

**The control landed at 0.4599 against a trivial baseline of 0.4600.** The
measurement setup is clean — a constant prompt buys exactly nothing, so
nothing is leaking through the split.

And then the finding: **name-only (0.4101) scores BELOW the
constant-prompt control (0.4599)**, and adding the name to the formula
costs 0.046. The mineral name is not weak signal, it is a distractor that
consumes head capacity. Whatever OLMo 2 1B knows about the word
"Anatase", none of it is spectroscopy — which is consistent with the
polymorph result above, where the head sat at the unrelated-minerals
floor on exactly that mineral.

**Everything this system knows, it reads off the chemical formula.**

### Is the language model doing anything at all?

Forced by the ablation: if the only useful input is a formula, does
running it through 1B parameters beat parsing it? `composition_mlp.py` is
the same head on the same splits with the same loss, fed a 92-dim
L2-normalised element-count vector instead of a 2048-dim pooled
transformer state. **Five paired seeds** (the effect is the size of the
seed spread, so two were not enough):

| | val | holdout |
|---|---|---|
| formula-only via OLMo | 0.6118 ± 0.0416 (35.5% of gap) | 0.5538 ± 0.0083 (22.0%) |
| composition MLP, NO LLM | 0.5835 ± 0.0410 (28.9%) | 0.5295 ± 0.0136 (16.3%) |
| retrieval control | 0.5773 | 0.5018 (9.1%) |

Paired LLM-minus-MLP: val **+0.0283 ± 0.0308, t = 2.06 — not
significant**; holdout **+0.0243 ± 0.0074, t = 7.38 — holds up**.

So the transformer buys a real but small increment on held-out species,
and nothing distinguishable on random ones. The plausible mechanism is
that the formula STRING carries what an element count discards —
hydration (`·4H2O`), oxidation state (`Pb^2+^2Pb^4+^O4`), parenthesised
polyhedral groups — all of which the counter flattens.

### Bottom line for run 2

On held-out minerals, as percent of the trivial→measurement-noise gap:

| system | gap closed |
|---|---|
| nearest-neighbour retrieval | 9.1% |
| composition MLP, no LLM | 16.3% |
| **OLMo formula encoder + head** | **22.0%** |

The honest claim is narrow and worth stating precisely: **a 1B language
model reading a chemical formula predicts a binned Raman spectrum
somewhat better than an element-count MLP and clearly better than
nearest-neighbour retrieval — and its pretrained knowledge of mineral
names contributes nothing.** It does not know that Anatase and Rutile
differ. Three quarters of the distance to measurement noise is unclosed.

What this rules out as the lever: adapter capacity (LoRA is a null),
frozen-representation quality (same), and prompt engineering on the name
(actively harmful). What it points at, if this line continues: the target
needs structural input — space group, coordination, or a graph — because
composition is provably insufficient for polymorphs and composition is
all this system currently has.

---

## 14. Structural augmentation — survey and earmarks

Run 2 ended on a missing-input diagnosis: Raman reads bonding geometry,
composition does not encode bonding geometry, and composition is all the
system had. This section surveys what is already on disk against that
gap and **measures** the candidates rather than ranking them by plausibility.

The framing that turned out to matter: run 1 tried to fix a fact problem
with REPETITION and failed, and the prompt ablation showed why — restating
the same species under a different name adds nothing (name-only scored
BELOW the constant-prompt control). A genuinely NEW VIEW of the same
species is a different thing entirely, and it is worth +0.071 holdout.
**Augmentation beat repetition, measured.**

### TIER 1 — validated, zero acquisition cost, ship it

**`mindat/geomaterials.jsonl`** — 65,044 named records, 148 fields.
Matches **94.8%** of our 1,952 spectra species by name, and among matched:
csystem 98.2%, cell length a 97.6%, Strunz class 98.4%, space group 81.6%,
density 88.3%, hardness 88.1%.

Encoded as 26 features (`augmented_mlp.py`: crystal-system one-hot,
Strunz one-hot, log a, c/a, b/a, log density, hardness, space-group
number, missing-indicator) and measured over 5 paired seeds:

| arm | holdout | % of trivial→ceiling gap |
|---|---|---|
| retrieval (no model) | 0.4990 | 9.1% |
| composition MLP | 0.5295 | 16.3% |
| OLMo formula encoder | 0.5538 | 22.0% |
| **structure only, 26 features, NO LLM** | **0.5685** | **25.4%** |
| **composition + structure, NO LLM** | **0.6009** | **33.0%** |

Paired on holdout: comp+struct minus comp-only **+0.0714 ± 0.0213,
t = 7.50**; comp+struct minus the OLMo formula encoder **+0.0471 ± 0.0192,
t = 5.48**. **Twenty-six numbers from a JSON dump beat a 1B-parameter
language model**, and adding them to composition doubles the gap closed.

### The caveat that stops this being oversold

Structure does NOT solve the polymorphs, which is what motivated the
search. Splitting the holdout over 3 seeds:

| holdout subset | comp+struct | retrieval | delta |
|---|---|---|---|
| polymorph (same-formula twin) | 0.5049 | 0.5207 | **−0.0159** |
| rest of holdout | 0.5166 | 0.4563 | **+0.0603** |

mindat clearly separates the pairs — Anatase a=3.78 c=9.51 against Rutile
a=4.59 c=2.96 — so the information is present and the model is not using
it. The likely reason is that cell geometry → vibrational mode is
PHYSICS, not a lookup, and 1,750 training species is nowhere near enough
to learn it. Crystal system and Strunz act as broad class priors that
help everywhere; the cell parameters are being wasted.

**So the broad lift and the polymorph gap need different sources.** Do not
report the +0.071 as a polymorph fix.

### TIER 2 — aimed at the polymorph gap; both need work

**`wurm/`** — 461 minerals of AB-INITIO computed Raman modes: per-mode
relative intensities (total/parallel/perpendicular) AND symmetry labels
(irreducible representations). This is literally the structure→spectrum
physics the model failed to learn, as labelled data.

**BLOCKED, and the inventory note was wrong about why.** The note says the
data is "embedded in page JS — needs the JS freq-array parser". The
parser is not the blocker: `no_itot_rel` (264 values) and `symlabel` (264)
ARE inline, but **`freq` has 0 assignments in all 461 pages** — the
frequencies load from `xmls/w{id}.xml`, and **0 XML files were mirrored**.
Intensities without wavenumbers cannot place a band. Fix is 461 small
fetches against the existing `wurm_ids.txt`. Cheapest high-value item here.

**`amcsd/`** — 10,719 CIFs, 2,349 distinct mineral names, 2,285 with a
space group; **46.4%** exact-name coverage of our species. Lower coverage
than mindat but categorically different content: **atomic positions**,
from which coordination numbers, bond lengths and a crystal graph follow.
That is the AtomGPT input format, and the thing a model could actually
learn mode physics from. Verified to separate every polymorph pair it
contains (Rutile `P4₂/mnm` vs Brookite `Pbca`; Pyrite `Pa-3` vs Marcasite
`Pmnn`; Adamite `Pnnm` vs Paradamite `P-1`).

**`cod/`** — 111 GB, >200k CIFs, the superset AMCSD was drawn from. Same
content, higher coverage, needs a one-time name→file index build. Do this
only if AMCSD's 46.4% proves to be the binding constraint.

### TIER 3 — bridge and auxiliary

**`materials_project/mp_summary.jsonl`** — 154,377 docs, 104,734 formulas,
**20,942 of them with multiple structures**, i.e. polymorph-capable by
construction. Confirmed to carry 46 TiO2 structures including anatase's
`I4_1/amd`, and both `Pa-3`/`Pnnm` for FeS2. Keyed by formula, not mineral
name, so it needs the bridge **mindat name → (formula, space group) → MP**.
Adds computed density, volume, band gap, formation energy, stability.
Atomic positions are NOT in the summary dump and would need a separate API
pull (key at `~/.mp_key`).

**`ecostress/`** — 3,104 mineral entries, TIR + VSWIR. Not structural; a
different MODALITY. Worth noting because the same species measured by a
second technique is another genuinely new view, and cross-modal was
under-used in run 1 (only reaching species that also had Raman).

### Recommended order

1. **mindat structural block into the training form** — validated, free,
   +0.071 holdout, doubles the gap closed. No reason to wait.
2. **fetch the 461 WURM XMLs** — smallest job on this list, and the only
   source that is directly about the unsolved problem.
3. **AMCSD CIF → coordination/bond-length features** for the 46.4%, as the
   test of whether real structure (not metadata) moves polymorphs.
4. COD name index and the MP bridge only if 3 shows the polymorph gap
   actually responds.

---

## 15. Structural integration — what was built

Points 1–3 of the §14 earmark, built and measured.

### 1. mindat structural block

`reference_layer.load_mindat_structure()` → 65,044 named records, 94.8%
name coverage of our species. Kept fields: crystal system, cell lengths
and angles, Strunz class, calculated density, Mohs hardness.

**A field that had to be renamed before it could be used.** mindat's
`spacegroup` column is NOT the International Tables number. Verified
against AMCSD H-M symbols: Quartz is ITA 152 and mindat says 89; Pyrite
is 205 and mindat says 204; Marcasite is 58 and mindat says 73 — no
offset fits, so it is a mindat-internal id. Emitting "Quartz, space group
89" would have been a fabricated fact in grounded-looking prose, the
exact failure this corpus cannot absorb. It is now
`mindat_spacegroup_id`, used only as an opaque categorical feature, and
`test_mindat_spacegroup_is_never_exposed_as_an_ita_number` asserts no
view prints it. **True space groups come from the CIFs.**

### 2. WURM — the mirror completed

`_acquire/wurm_xmls.py` fetches the 461 crystal XMLs the original mirror
missed, politely (one at a time, 2 s apart, identifying User-Agent,
resume by skipping what is on disk, `.part`-then-rename so a truncated
file is never mistaken for a complete one).

**It died at file 288 of 461 and the reason is worth recording.**
`http.client.IncompleteRead` inherits from `HTTPException`/`ValueError`
and from NEITHER `URLError` NOR `OSError`, so the narrow `except` let it
escape and kill the run. A truncated read is also precisely the failure
most worth retrying. Now caught with 4 retries and backoff.

`wurm_layer.py` joins the two halves — intensities and symmetry labels
from the HTML, frequencies from the XML, on mode index — and **skips any
mineral whose two halves disagree on mode count**, because a silent
off-by-one would shift every band onto its neighbour's intensity: wrong
in a way that looks entirely plausible.

Validated against literature: Zircon's computed 1012 cm⁻¹ (B1g),
438 (A1g), 364 (Eg) are the known bands. And it supplies computed
polymorph pairs outright — Zircon `I4_1/amd` against Reidite `I4_1/a`,
both ZrSiO4, unrelated spectra.

### 3. AMCSD CIF geometry

`cif_features.py` → 2,152 minerals from 8,461 usable CIFs (876
unparseable). Per mineral: coordination number per cation, bond lengths
(mean/min/spread) per pair, shortest bond, volume per atom, density, and
the true space group.

**Validated before use, which is the only reason to trust it.** A missed
symmetry operation yields too few atoms and understates every
coordination number *with no error raised*. Checked against textbook
values: Diopside Si 4 / Mg 6 / Ca 8, Quartz Si 4 (Si–O 1.605 Å), Pyrite
Fe 6 (Fe–S 2.264 Å), Rutile Ti 6, Forsterite Si 4 / Mg 6, Calcite Ca 6 /
C 3 (C–O 1.286 Å) — **6 of 6 exact**. Operator ruling: use a library
(pymatgen) rather than hand-roll the symmetry expansion.

### The three new views

`structure` (species → its structural identity), `polymorph` (the
contrastive view with the REASON attached), `computed` (ab-initio modes
with irreps, explicitly flagged as offset from measured positions so the
corroboration views never conflate theory with measurement).

On a 120-species smoke run: 270 structure, 20 polymorph, 30 computed
views alongside the existing 420/420/46/252/20.

### What the geometry features bought, measured

| features | holdout | polymorph subset |
|---|---|---|
| composition only | 0.5295 | — |
| + mindat structure | 0.6009 | 0.5049 |
| + AMCSD geometry | 0.5888 | **0.5182** |

Overall the CIF block is flat-to-slightly-down (within noise, and only
44% of species have an AMCSD entry). But **on polymorphs it is +0.0134
against +0.0010 for everything else** — a 13× concentration exactly where
the mechanism predicts, which is the signature you want even at this
magnitude.

**Honest reading: directionally right, not yet the fix.** Polymorphs move
from 0.5049 to 0.5182 and remain far below the rest of the holdout. The
likely limit is the ENCODING rather than the data — a whole crystal
structure collapsed to eight summary numbers discards the geometry that
distinguishes the pair. A graph representation over the atomic positions
(which AMCSD supplies and which we now parse) is the next thing to try,
and WURM's symmetry-labelled modes are the supervision for it.

---

## 16. LoRA run 2 — pre-registration

Written BEFORE the run, because run 1's most expensive error was a
metric that changed mid-experiment (§11): the eval set was fixed
mid-flight and every comparison crossing that change became meaningless,
including the baseline and the checkpoint bookkeeping.

### The cross-run comparison is INVALID and must not be made

Run 1's headline was eval loss 1.8499 → 1.6692 (−9.77%). **Run 2's loss
cannot be compared to those numbers.** The corpus gained `structure`,
`polymorph`, `computed`, `libs_predicted` and `libs_temperature` views,
and held-out species receive them too — so the eval SET is different and
the loss is on a different scale. The base-model baseline is therefore
recomputed on the v3 eval set, and run 2 is compared only against that.

### What changed in the corpus

- structural views from mindat (94.8% species coverage) + AMCSD geometry
- coordination numbers recomputed with CrystalNN after the covalent-radius
  cutoff was found to undercount large cations (Leadhillite Pb 1.375 →
  6.375; zero implausible values remain)
- WURM ab-initio modes with symmetry labels, 459 minerals, 34,837 modes
- LIBS records gained RELATIVE INTENSITIES from the NIST Boltzmann
  derivation, plus a temperature-pair view; the shipped ASD loader was
  returning nothing for 42 of 92 elements including Ca

### Predictions, in falsifiable order

1. **Eval loss improves by roughly the same 8–12% as run 1.** Format is
   still the largest single thing the model can learn, and that has not
   changed. A much larger improvement would more likely indicate an eval
   artifact than real learning, and should be investigated as one.

2. **The decisive test is NOT loss.** Run 1 posted a respectable loss
   while emitting IDENTICAL band lists for Quartz and Hematite. The test
   that matters is `probe_digits.py`: does the model now produce
   DIFFERENT output for Anatase and Rutile? Run 1 could not — both are
   TiO2 and it had only composition to go on. The corpus now states their
   structures explicitly and contrasts them in a `polymorph` view.
   **If that still fails, the structural views did not transfer, and no
   loss number redeems it.**

3. **Weaker but checkable:** for held-out species the model should be
   able to state a crystal system. It has never seen these species, but
   it has seen ~3,300 structure views, so the FORM is learnable even
   where the fact is not. Getting the form right and the fact wrong is
   the expected outcome and is worth measuring separately — that
   distinction is what §11's correction was about.

4. **Expected NOT to work:** exact band positions. Nothing in this round
   addresses exposure consistency, which §11 identified as the reason
   positions failed. Predicting this in advance is the point — if
   positions do improve, the diagnosis in §11 is wrong.

### Configuration

Unchanged from run 1 for comparability: OLMo 2 1B, LoRA r32 on attention
+ MLP, batch 2 × accum 16 (the 100,352-vocab logits tensor is the
binding constraint), lr 1e-4, seq 2048. Run 1 peaked at epoch 2.50 and
was stopped at 3.75 on two consecutive eval rises; run 2 gets the same
early-stopping rule.

The 3090 is freed by stopping the mission and LLMVP, exactly as run 1
did — one LLMVP process holds 22.9 GB on the 3090 and 8.0 GB on the 3060.
Both are restarted afterwards.

---

## 17. LoRA run 2 result — the pre-registered test PASSED, the facts did not

All four §16 predictions resolved. Two of them the way I wanted, two not,
and one unanticipated cost.

### Loss (prediction 1: CONFIRMED)

Baseline **1.8158** on the v3 eval set → best **1.6315** at epoch 2.99
(step 2238). **−10.15%**, inside the pre-registered 8–12% band.

| epoch | 0.5 | 1.0 | 1.5 | 2.0 | 2.5 | **3.0** | 3.5 | 4.0 |
|---|---|---|---|---|---|---|---|---|
| eval | 1.661 | 1.645 | 1.640 | 1.634 | 1.634 | **1.631** | 1.633 | 1.633 |

Do NOT compare this to run 1's 1.6692: different eval set, different scale.

### The decisive test (prediction 2: PASSED)

`probe_polymorph.py`, seven polymorph pairs, generation similarity between
the two members. Lower = the model distinguishes them.

| prompt | BASE | TUNED |
|---|---|---|
| "…shows Raman bands at" | 0.799 | **0.568** |
| Anatase vs Rutile specifically | **1.00 IDENTICAL** | **0.54** |
| pairs emitting identical text | 4 of 7 | **0 of 7** |

**Anatase and Rutile went from byte-identical to clearly different.** That
was the pre-registered success criterion and it is met. The base model
also emits implausible values (2.1, 2.3, 2.6 cm⁻¹); the tuned model emits
the right RANGE (109.6, 125.8, 155.4 …). Format and range were learned.

### The facts are still wrong (predictions 3 and 4: CONFIRMED)

- Anatase → "trigonal (space group P3_221)". That is QUARTZ's space
  group. Anatase is tetragonal I4_1/amd.
- Rutile → "monoclinic C2/c". It is tetragonal P4_2/mnm.
- Marcasite and Pyrite → both "monoclinic C2/c, a = 5.34 Å", 0.98 similar
  and both wrong (Pnnm orthorhombic / Pa-3 cubic).
- Anatase's dominant Raman band is 144 cm⁻¹; the model offers 109.6.

Prediction 4 said band positions would NOT improve because nothing this
round addressed exposure consistency (§11). That held. The diagnosis in
§11 survives its test.

### The unanticipated cost — structure views HOMOGENISED the output

| prompt | BASE | TUNED |
|---|---|---|
| "…is" (structure) | **0.476** | **0.784** |

On the structure prompt the tuned model is markedly LESS differentiated
than the base. The base distinguishes minerals through vague encyclopedic
recall — "a common photocatalyst used in water treatment" for Anatase
against "a common mineral in the Earth's crust" for Rutile. The tuned
model replaced that with a rigid template — "*is* {system} (space group
{sg}) with unit cell a = …" — and then filled the slots with whatever is
most frequent rather than what is true.

**3,318 structure views taught the FORM of a structural statement and
overwrote weak-but-real knowledge with confident-and-wrong knowledge.**
That is worse than a null result and it was not predicted. It is the same
mechanism as run 1's template acquisition, one level up: give a 1B model
a uniform frame and it learns the frame.

### What this says about the direction

The structural data is right — it doubled the regression head's holdout
score (§14) with no language model involved. The problem is the DELIVERY:
3,318 near-identically-phrased records are a template, and a 1B model
learns templates far faster than it learns 3,318 distinct facts.

Two candidate responses, neither yet tested:
1. **Vary the phrasing hard.** The interconnect design already says
   oversampling must be distinct framings rather than copies; the
   structure view has exactly one framing and violates its own rule.
2. **Stop asking a 1B LM to hold the facts at all.** §14 showed 26
   structural numbers beat the whole 1B model on the actual prediction
   task. The LM may be the wrong home for this data — a retrieval or
   regression path may simply be correct, with the LM as the interface.

---

## 18. Does the run-2 adapter improve the BACKBONE? No — it damages it

A separate question from §17. That asked whether the tuned model can
GENERATE the answer. This asks whether 14.2M tokens of mineral text
improved its internal REPRESENTATION, by using the merged adapter as the
frozen feature extractor for the cosine head from §13 and comparing
against the plain base backbone. Three prompt modes, five paired seeds,
identical splits.

| mode | base | adapted | paired delta | |
|---|---|---|---|---|
| formula | 0.5532 ± 0.006 | 0.5536 ± 0.007 | +0.0003, t=0.09 | no effect |
| both | 0.5707 ± 0.012 | 0.5585 ± 0.021 | −0.0122, t=−1.36 | ns |
| **name** | **0.4208 ± 0.013** | **0.3880 ± 0.015** | **−0.0327, t=−5.01** | **SIGNIFICANT** |

(trivial 0.4600; constant-prompt control 0.4599; unrelated-species floor
0.2316; noise ceiling 0.8874)

**The adapter buys nothing on formula and significantly DAMAGES the name
pathway.** Name-only was already below the constant-prompt control on the
base backbone (0.4208 vs 0.4599 — worse than useless). After training on
14M tokens of mineral text it drops to 0.3880, moving AWAY from trivial
and toward the unrelated-species floor.

### This is the same effect §17 found, measured a second way

§17 (generation): on structure prompts, pairwise similarity between
polymorph members rose 0.476 → 0.784. The base distinguished minerals by
vague encyclopedic recall; the tuned model replaced that with one
template and homogenised them.

§18 (representation): the name-conditioned embedding became LESS
predictive of the spectrum.

Two independent instruments, same conclusion: **3,318
near-identically-phrased structure records taught the model that a
mineral name is a slot-filler in a frame, and that is strictly less
information than the diffuse pretrained association it overwrote.** The
generation probe could be dismissed as a decoding artifact; the
representation result cannot — the head never decodes anything.

### Consequence for the design

The corpus-side rule stands and is now load-bearing: oversampling must be
DISTINCT FRAMINGS, not copies. A view with one phrasing repeated 3,318
times is a template, and a 1B model will learn the template and pay for
it with representation quality.

It also sharpens §14's conclusion. Twenty-six structural numbers beat the
whole 1B model on this task; feeding those same facts through the LM as
uniform prose makes the LM worse. On current evidence the language model
is the wrong container for these facts — the regression/retrieval path
holds them better, and the LM's role should be the interface, not the
store.
