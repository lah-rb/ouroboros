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

---

## 19. Corpus v4 / full-parameter two-stage run — pre-registration

Written 2026-09-07 BEFORE any v4 corpus is built or any weight is trained,
under the §16 rule: the instrument is fixed before the experiment. Numbers
marked *measured* are filled in by the smoke and probe steps and may not be
edited after stage 1 starts.

### Why this run exists

§11, §17 and §18 established that the LoRA passes learned output shapes and
not facts, and this session measured why:

1. **Truncation.** `emit.MARKDOWN_CHUNK_CHARS = 12_000` was sized for the
   4,096-token window; `train_lora.py` cut at 2,048. Run 1 trained on
   11.78M of 16.93M tokens. On the 2026-09-07 corpus 87.7 % of markdown
   chunks exceed 2,048 tokens (59.6 % of tokens kept) against 11.5 % at 4,096
   (91.4 % kept).
2. **LoRA ≠ continued pretraining.** 24M of 1.48B parameters at lr 1e-4, no
   replay; §18 measured damage to the name pathway (0.4208 → 0.3880,
   t = −5.01) — a forgetting signature.
3. **Templating.** `assemble.PHRASINGS` is stored and never rendered; 400
   inverse records carry 11 distinct openings; 3,318 identical structure
   views homogenised generation (§17) and representation (§18).
4. **Competing targets.** species → formula (one string) was learned;
   species → bands (several lists per prompt) was not.

### Design (operator decisions 2026-09-07)

- **Stage 1**: continued pretraining, FULL weights, fp32 master + bf16
  autocast, seq 4,096, boundary-aware packing, both GPUs via an explicit
  device map. Prose corpus: accepted papers with FIGTEXT INLINED at the
  figure anchor (`build_curator_doc`), `.en.md` preferred, binder ×4, pack
  prose, reference prose from every dataset with a reader plus ROD, HOM,
  webmineral and mindat prose (licence-tagged), and ~17 % replay from OLMo's
  own midtraining mix biased toward scientific text (pes2o ≫ wiki > dclm).
  lr 4e-5 constant after a 5 % warmup. **Two epochs committed** (~172M
  tokens).
- **Stage 2**: the anneal — prompt → completion shapes from a TEMPLATE
  LIBRARY (≥10 statement frames + ≥4 question frames per view; probe frames
  never trained), single-valued targets (one canonical band list per
  species + modality, measurement-keyed prompts otherwise), loss on
  completions only, 70 % shapes / 22 % carried stage-1 prose / 8 % replay,
  linear decay 4e-5 → 0, two epochs of ~12M tokens.
- **No species holdout.** Everything trains. The "obscure systems" probe =
  the ~100 REFERENCE-ONLY RRUFF species (in no paper, ≥2 spectra) with the
  LOWEST exposure in the rendered stage-1 text. **They are not withheld** —
  the first design excluded them from every view, but with webmineral and
  HOM in the mix every IMA species is named somewhere (all 100 candidates
  and all 1,014 reserves were), so the set is an exposure-ranked evaluation
  list: recall at the low end of exposure versus recall on well-exposed
  species.
- **Parity cap lifted** (`reference_only_budget=None`).

### Token targets

Operator addition (2026-09-07, after approval): **the reference datasets bear
repetition in stage 1 alongside the binder papers.** Templated reference
facts (RRUFF, ROD, ECOSTRESS, mindat structure, AMCSD, WURM, NIST, SSHADE)
are presented 4x as FOUR DISTINCT FRAMES (never copies — §17/§18); the
encyclopaedic sheets (HOM, webmineral, mindat prose) 3x as copies, like the
binder shelf. Papers and replay stay at 1x.

| stage | source | repeats | approx. weighted tokens |
|---|---|---|---|
| 1 | papers markdown (2,057) + inlined figtext | 1 | ~60M |
| 1 | binder shelf | 4 | ~5M |
| 1 | pack prose | 1 | ~1M |
| 1 | templated reference facts (RRUFF/ROD/ECOSTRESS/mindat-struct/AMCSD/WURM/NIST/SSHADE) | 4 (distinct frames) | ~6M |
| 1 | HOM + webmineral + mindat prose | 3 (copies) | ~39M |
| 1 | replay (pes2o ≫ wiki > dclm) | 1 | ~20M (~15 % of stage) |
| 2 | shapes / carried prose / replay | — | 70 / 22 / 8 |

Stage 1 ≈ 130M weighted tokens per epoch, two epochs ≈ 260M; at the
planning pace of 3–5M tok/h that is 52–87 h. The smoke test replaces the
pace figure before stage 1 starts.

Manifest reports realised shares; ±2 points of target is a pass.

### Predictions, falsifiable

1. **Papers val loss falls ≥ 10 % from base after stage 1** (run 1/2 moved
   ~10 % on their own eval sets; a much larger move is an artefact to
   investigate).
2. **Replay val loss stays within +3 % of base at both stages.** This is the
   forgetting bound; §18's damage would show here first.
3. **Structure-prompt polymorph similarity stays ≤ base (0.476).** Run 2 rose
   to 0.784; the template library and carried prose exist to prevent that.
4. **Seen-species recall at stage 2**: formula ≥ 80 %, crystal system ≥ 70 %,
   ≥ 2 of 3 strongest bands within ±10 cm⁻¹ for ≥ 50 %, inverse top-1
   ≥ 40 %; trained-frame minus probe-frame gap ≤ 15 points.
5. **Reference-only probe species**: bands near floor (never seen), but
   formula / crystal-system accuracy ≥ base — a drop is §18-style damage.
6. **§18 instrument**: name-mode paired delta ≥ 0 (run 2: −0.0327).
7. **Expected NOT to work**: bands for species with a single noisy spectrum,
   and anything about the 14 binder papers still without a PDF.

### Licence gate

HOM (© Mineral Data Publishing 2001), webmineral (scraped) and mindat prose
(NC API terms) are INCLUDED for this private research run by operator
decision; every record and the manifest carry `license: restricted-<source>`
so they can be excluded from any published artefact or a later run.

### Measured before stage 1 (frozen 2026-09-07)

**Device map.** `device_map.py --probe`, fp32 params + fused AdamW + bf16
autocast + gradient checkpointing at 1x4096:

| 3060 layers | peak 3090 | peak 3060 | min headroom | tok/h |
|---|---|---|---|---|
| 2 | 22.4 | 7.1 | 1.2 | 9.29M |
| 3 | 21.8 | 8.2 | 1.8 | 9.51M |
| **4** | **21.2** | **9.3** | **2.3** | **9.64M** |
| 5 | 20.1 | 10.4 | 1.2 | 9.14M |

Chosen: **embeddings + 4 decoder layers on the 3060**, the rest and `lm_head`
on the 3090.

**Trainer smoke** (`train_full.py --smoke`, 20 steps, 200 blocks, eval every
10 steps over all 8 val sets — far heavier eval than the real cadence):
peak 21.25 GiB / 8.18 GiB, **6.1M tok/h end-to-end, 1,850 tok/s (6.7M tok/h)
while training**. Loss 1.831 -> 1.539 over 20 steps. Resume from
`checkpoint-10` reproduced step 20's loss exactly, so the optimizer state
round-trips. A resumable checkpoint is 17 GB (fp32 weights 5.9 + AdamW 11.9);
`save_total_limit 2` + a 2.8 GB bf16 endpoint = ~37 GB of the 79 GB free.

**Wall-clock, revised from measurement:** stage 1 is 34,261 blocks =
2,141 optimizer steps per epoch; two epochs = 4,282 steps / 280.7M tokens
≈ **42 h** at the measured rate. Stage 2 (3,521 blocks, 220 steps/epoch,
two epochs) ≈ 4 h.

**Base-model numbers** (the floor every prediction is measured against).
Recall probe, greedy decoding, trained-frame vs probe-frame:

*Seen species (n=200):*

| task | trained frame | probe frame | gap |
|---|---|---|---|
| formula | 0.010 | 0.010 | +0.000 |
| crystal_system | 0.086 | 0.000 | +0.086 |
| bands | 0.000 | 0.000 | +0.000 |
| inverse | 0.000 | 0.000 | +0.000 |

*Low-exposure probe species (n=97):*

| task | trained frame | probe frame | gap |
|---|---|---|---|
| formula | 0.000 | 0.000 | +0.000 |
| crystal_system | 0.103 | 0.021 | +0.082 |
| bands | 0.000 | 0.000 | +0.000 |
| inverse | 0.000 | 0.000 | +0.000 |

Polymorph probe: band prompts **0.799** mean pairwise similarity,
4/7 identical; structure prompts **0.476**,
0/7 identical. The structure figure is the homogenisation
guard: run 2 pushed it to 0.784 and that must not recur.

Per-source validation loss of the base model on the v4 stage-1 val sets
(step 0 of the smoke): binder 1.852, reference 1.826, replay 2.013,
paper_markdown 2.098, pack_prose 2.264, webmineral 2.274, hom 2.486,
mindat_prose 2.728.

### Corpus v4 as packaged (2026-09-07, `package.py`, seq 4,096)

Stage 1: **34,261 blocks = 140,333,056 tokens per epoch** (pad 6.85%, 666 hard cuts of over-long paragraphs), validation 249 blocks across 8 sources. Replay share 15.1 %. Two epochs ≈ 281M tokens.

| source | docs | unique tokens | repeats | weighted | share |
|---|---|---|---|---|---|
| paper_markdown | 2,022 | 57,861,116 | 1 | 57,861,116 | 44.3 % |
| webmineral | 4,651 | 7,077,946 | 3 | 21,233,838 | 16.2 % |
| reference | 115,773 | 12,207,143 | 1 | 12,207,143 | 9.3 % |
| replay/pes2o | 2,549 | 11,874,564 | 1 | 11,874,564 | 9.1 % |
| hom | 3,831 | 3,511,680 | 3 | 10,535,040 | 8.1 % |
| replay/wiki | 5,877 | 4,951,936 | 1 | 4,951,936 | 3.8 % |
| binder_markdown | 22 | 1,198,235 | 4 | 4,792,940 | 3.7 % |
| mindat_prose | 9,083 | 1,062,086 | 3 | 3,186,258 | 2.4 % |
| replay/dclm | 2,681 | 2,974,673 | 1 | 2,974,673 | 2.3 % |
| pack_prose | 2,050 | 1,102,902 | 1 | 1,102,902 | 0.8 % |

Licence shares (weighted tokens): unknown 29.8M, restricted-webmineral 21.2M, ODC-BY 19.8M, cc-by 19.1M, reference-mixed 12.2M, restricted-hom 10.5M, cc-by-nc-nd 6.9M, other-oa 3.7M, restricted-mindat 3.2M, cc-by-nc 2.5M, cc-by-nc-sa 0.9M, public-domain 0.4M, cc-by-sa 0.3M, cc-by-nd 0.1M.

Stage 2: **3,521 blocks = 14,422,016 tokens per epoch**; shapes 71 % / carried prose 23 % / replay 7 %; 107,411 examples, 0 dropped as over-long; validation 23 blocks. Stage-2 kinds: raman_bands 26,130, pack_fact 19,591, formula 18,468, structure 18,276, ir_troughs 5,511, libs_temperature 5,484, libs_lines 5,445, inverse 3,862, corroboration 2,262, computed 1,344, cross_modal 513, contrastive 201, polymorph 186, ice_bandlist 138.

### Stage 1, first result: prediction 2 FALSIFIED — the forgetting bound fired

Launched 2026-09-07 18:54 at lr 4e-5. **Replay validation loss crossed the
pre-registered +3 % bound at epoch 0.23 and reached +3.4 % by epoch 0.33**,
so the run took the response the pre-registration names: stopped, resumed
from `checkpoint-600` (epoch 0.28) at **lr 2e-5** (`--override-lr`).

| epoch | replay | vs base | per-eval delta |
|---|---|---|---|
| 0.00 | 2.1180 | — | — |
| 0.05 | 2.1310 | +0.6 % | +0.013 |
| 0.09 | 2.1510 | +1.6 % | +0.020 |
| 0.14 | 2.1650 | +2.2 % | +0.014 |
| 0.19 | 2.1760 | +2.7 % | +0.011 |
| 0.23 | 2.1810 | +3.0 % | +0.005 |
| 0.28 | 2.1860 | +3.2 % | +0.005 |
| 0.33 | 2.1900 | +3.4 % | +0.004 |

**Read it honestly both ways.** The bound is crossed, which is what the
prediction said would not happen — recorded as a falsification, not
explained away. But the per-eval delta decays 0.020 → 0.004 across the same
span, so the curve is flattening rather than running away, and the domain
losses were moving hard in the right direction at the same time
(paper_markdown 1.626 → 1.391, −14.4 %; HOM 2.485 → 1.199; webmineral
2.357 → 0.525; reference 1.719 → 0.613). Halving the rate is the
pre-registered response and it was taken on the number, not on the
interpretation; whether the flattening or the crossing was the better guide
is decided later by the §18 representation instrument, which is the
measurement replay loss is only a proxy for.

**A resume does not honour `--lr`.** Trainer builds the optimizer and
scheduler, then `_load_optimizer_and_scheduler` restores both from the
checkpoint, and `on_train_begin` fires BEFORE that load — so the new rate is
silently discarded. `train_full.OverrideLR` patches the param groups and
`base_lrs` on the first `on_step_begin`, the first hook after the load, and
logs `[LR OVERRIDE] … base_lrs -> 2e-05` so the change is visible in the run
log rather than assumed.

**Throughput, measured over 6.5 h:** 7.03M tok/h while training, 5.94M
end-to-end including eval over eight sets every 100 steps and 17 GB
checkpoint writes. Two epochs ≈ 40 h of training time.

### The response worked: lr 2e-5 reversed the forgetting and learns faster

Four hours after the resume, at the SAME epoch, the halved rate is better on
**both** axes — so 4e-5 was overshooting, not buying speed:

| | replay (bound 2.1815) | paper_markdown |
|---|---|---|
| lr 4e-5 @ e0.33 | 2.1900 (+3.4 %) | 1.3910 |
| **lr 2e-5 @ e0.33** | **2.1770 (+2.8 %)** | **1.3760** |

Replay reversed rather than merely slowing — 2.1860 → 2.1770 → 2.1750 →
2.1730 → 2.1730 — and is back **inside** the +3 % bound, flat for the last
two evaluations. Domain learning did not pay for it: paper_markdown
1.3960 → 1.3610 (−16.3 % from the 1.626 base, against a ≥10 % acceptance
bar), reference 0.6338 → 0.5737, binder 1.8000 → 1.7930 over the same span.

So the pre-registered bound did its job twice over: it caught a real
regression, and the prescribed response turned out to be a straight
improvement rather than a trade. The remaining question is unchanged — loss
is a proxy, and §18's representation instrument decides at the end whether
the mineral-name pathway survived.

### Stage 1 result (2026-09-09): the corpus was learned, the bound was not held, facts pending

**Run.** 4,282 steps / 280.58M tokens at 7.02M tok/h while training; endpoint
`~/models/olmo2-1b-spectra-full/stage1/final` (bf16) with fp32 weights in
`checkpoint-4282`. Three interruptions, all recorded: the pre-registered halving
at epoch 0.28 (above); a **second bound crossing at epoch 1.03 that was NOT
acted on** — the rule changes the rate, not the epoch count, and I mistook it
for a change to the operator's two-epoch commitment; and a **false hang
diagnosis at 11:36** (stdout is block-buffered, so a 38-minute log silence and a
"missing" eighth eval line meant nothing; GPU1 100 % / GPU0 3 % is the healthy
pipeline pattern) that killed a stepping run and cost ~1.6 h on a resume from
`checkpoint-4000`. Positive evidence — a checkpoint interval passing with no
checkpoint, or a `py-spy dump` — is now required before any kill.

**Validation, final vs the run's own step-0 evaluation.** (The smoke's step-0
table above is NOT comparable: `--smoke` evaluates truncated val sets, which is
why its papers/reference/replay/webmineral figures differ; the sets it did not
truncate — binder, pack_prose, hom, mindat — agree to the third decimal.)

| source | base | final | change | minimum |
|---|---|---|---|---|
| paper_markdown | 1.626 | 1.3181 | −18.9 % | final (still falling) |
| reference | 1.719 | 0.4035 | −76.5 % | final |
| webmineral | 2.357 | 0.4207 | −82.2 % | 0.4148 @ e1.73 |
| pack_prose | 2.264 | 1.4379 | −36.5 % | 1.4363 @ e1.96 |
| hom (×3 copies) | 2.485 | 1.1323 | −54.4 % | 1.0890 @ e0.98 |
| mindat_prose (×3) | 2.728 | 1.8835 | −31.0 % | 1.7150 @ e0.98 |
| binder_markdown (×4) | 1.852 | 2.5266 | **+36.4 %** | 1.7900 @ e0.65 |
| replay | 2.118 | 2.2117 | **+4.4 %** | 2.1730 @ e0.42 |

**Prediction 1 (papers ≥ 10 %): PASSED, with a decomposition that shrinks it.**
`val_split_figtext.py` split the papers val set by paragraph class at
checkpoint-3600: VLM figure readings are 27.7 % of the tokens and fell
1.616 → 1.043 (−35.4 %); the papers' own text fell 1.584 → 1.407 (**−11.2 %**).
55 % of the headline drop is the house style of the figure prose. Every one of
the 11 held-out papers improved on its own text (−5.9 % to −15.6 %). A second,
smaller artefact: 12 of 13 val papers have their pack summary in TRAIN (val was
drawn per source; packs are per-paper derivatives) — 1.4 % of val tokens, so a
couple of points at most. Next corpus: coordinate val across paper-derived
sources.

**Prediction 2 (replay ≤ +3 %): FALSIFIED, twice.** The halving reversed the
first crossing; the curve re-crossed at epoch 1.03 and rose ~0.15 % per tenth
of an epoch to +4.4 %. Whether that is damage is the §18 instrument's call
(pending below), not the loss's.

**Prediction 3 (structure-prompt similarity ≤ base 0.476): FAILED at both
checkpoints; the Raman criterion passed.** `probe_polymorph.py`:

| model | Raman prompts | identical | structure prompts |
|---|---|---|---|
| base | 0.799 | 4/7 | 0.476 |
| epoch 0.93 | 0.579 | 0/7 | 0.671 |
| final | 0.447 | 0/7 | 0.616 |

Far below run 2's 0.784, so the frame library helped, but structure answers
still come in one template (Andalusite vs Mullite 0.98).

**Digit probe (stage-2 criterion, recorded for the trajectory).** Base: digit
soup. Epoch 0.93: wrong ascending lists for Quartz and Calcite; inverse prompts
collapse to one continuation. **Final: Quartz → 182.6, 205.4, 265.9, 356.7,
465.1** — four of five within 2 cm⁻¹ of RRUFF's 206/265/355/464, in the
measurement-frame order; Calcite wrong (180.6, 212.8, 1054.4 vs 1085/712/282);
**inverse still collapsed** — 1085/712/282 and 464/206/128 both → "Tsumebite,
whose composition is Pb2+2Cu2+(PO4)(S6+O4)" at p≈1.0, gypsum's bands →
"Metatorbernite". Confident-and-wrong in the inverse direction is run 2's
failure mode reappearing; the forward direction has started to carry facts.

**The binder rise is memorisation of a small repeated set, not size and not
era.** CPU probe, base → checkpoint-4200: the two held-out binder papers
(Barkla 1911, Moseley 1913) 1.772 → 2.623 (**+48 %**); five TRAINING binder
documents of every decade 1.993 → 0.837 (**−58 %**, near recitation); three
modern held-out papers −15.5 %. Twenty-two documents × 4 copies × 2 epochs =
8 exposures with nothing to dilute them; mindat prose (6 exposures) also rose;
papers (2) did not. The reference layer, repeated as four DISTINCT frames,
kept improving — [[structure-beats-the-llm-for-spectra]]'s rule ("copies are a
template, not N facts") replicated inside one run. The binder val set is two
documents / 20 k tokens; mindat's is 3 blocks. **Next corpus: ≤ 2 copies, more
distinct foundational documents rather than more copies, and a validation
floor (≥ 20 blocks) per source.** Stage 2's carried prose holds one binder
document once (15 of 3,521 blocks), so no change there.

**Recall probe (`probe_recall.py`, greedy, ±10 cm⁻¹ band tolerance): facts
appeared in stage 1, before any shape training.** Scores are fractions; each
question asked through the fact's first-ranked question frame ("trained" —
a misnomer at stage 1, since question frames are only trained in stage 2) and
through the never-trained PROBE frame.

*Seen species (n=200):*

| task | base | epoch 0.93 trained / probe | **final** trained / probe |
|---|---|---|---|
| formula | 0.010 / 0.010 | 0.170 / 0.260 | **0.210 / 0.380** |
| bands (≥2 of 3 within ±10) | 0.000 / 0.000 | 0.225 / 0.465 | **0.340 / 0.505** |
| crystal_system | 0.086 / 0.000 | 0.137 / 0.193 | 0.168 / 0.198 |
| inverse (bands → species) | 0.000 | 0.000 | **0.000** |

*Low-exposure probe species (n=97; in the reference layer, absent from papers):*

| task | base | epoch 0.93 | **final** |
|---|---|---|---|
| formula | 0.000 / 0.000 | 0.103 / 0.186 | **0.144 / 0.361** |
| bands | 0.000 / 0.000 | 0.186 / 0.381 | **0.278 / 0.443** |
| crystal_system | 0.103 / 0.021 | 0.206 / 0.227 | 0.216 / 0.227 |
| inverse | 0.000 | 0.000 | 0.000 |

Four readings. (1) **The endpoint beats epoch 0.93 on every recall task**, on
both species sets — the second epoch bought facts, not just loss, and this
decides the stage-2 branch point in the endpoint's favour. (2) **Bands are at
50.5 % under a frame the model never saw**, which is the stage-2 acceptance bar
(≥ 50 %) reached in stage 1; formula (38 %) and crystal system (20 %) are far
from their bars (80 % / 70 %), and **inverse is exactly zero** — the digit probe
shows why: every band list is answered with a memorised favourite species and
its formula at p≈1.0. (3) **Low-exposure species recall nearly as well as seen
species** (bands 0.443 vs 0.505, formula 0.361 vs 0.380): the recall is coming
from the four-frame reference prose, not from the papers. Prediction 5's
"bands near floor" premise no longer applies because the probe set is
low-exposure rather than withheld; its guard half holds — formula and crystal
system ≥ base. (4) **Crystal system barely moved** and the polymorph probe shows
the mechanism: structure prompts return a fluent template with wrong systems
(Anatase "hexagonal", Pyrite "monoclinic", Marcasite "trigonal") — run 2's
confident-and-wrong, now confined to structure while bands and formulas carry
real content. The trained-minus-probe gap is **negative** (−17 points on formula)
because at stage 1 neither wording was trained as Q→A; the collapse metric
becomes meaningful only after stage 2.

**Prediction 6 (§18 head instrument, name-mode paired delta ≥ 0): PASSED — the
name pathway was repaired, not damaged.** Frozen backbone → cosine head on
1,952 species / 9,655 spectra, probe-species holdout, 5 paired seeds, last-10-
epoch means (the §18 convention); `val` = seen-species split, `holdout` = the
97 probe species:

| prompt | metric | base | epoch 0.93 | final | Δ final−base (paired t) | Δ e0.93−base |
|---|---|---|---|---|---|---|
| name | val | 0.4233 ± 0.033 | 0.4806 ± 0.018 | 0.4858 ± 0.029 | **+0.0625**, t=6.4 | +0.0573, t=5.1 |
| name | holdout | 0.4074 ± 0.006 | 0.4496 ± 0.007 | 0.4452 ± 0.010 | **+0.0378**, t=8.8 | +0.0421, t=9.3 |
| formula | val | 0.6005 ± 0.015 | 0.6202 ± 0.015 | 0.6222 ± 0.014 | **+0.0217**, t=3.8 | +0.0197, t=4.4 |
| formula | holdout | 0.5901 ± 0.007 | 0.6104 ± 0.005 | 0.6163 ± 0.008 | **+0.0262**, t=7.7 | +0.0203, t=4.7 |
| both | val | 0.5798 ± 0.024 | 0.6113 ± 0.031 | 0.6135 ± 0.023 | **+0.0337**, t=9.9 | +0.0314, t=6.5 |
| both | holdout | 0.5860 ± 0.004 | 0.5860 ± 0.002 | 0.5897 ± 0.005 | **+0.0036**, t=1.0 | +0.0000, t=0.0 |

Run 2 had moved name-mode from 0.4208 to 0.3880 (t = −5). Stage 1 moves it
**+0.06 on val and +0.04 on holdout**, taking the mineral NAME from below the
constant-prompt control (0.46) to slightly above it — the first time the name
has carried spectrum-relevant information in this project. Formula gains
+0.02, both +0.03. The endpoint and epoch 0.93 are indistinguishable here
(differences ≤ 0.006, inside seed noise), so the instrument does not separate
the branch points; recall did. Replay's +4.4 % is therefore NOT the §18-style
damage the bound was written to catch — the representation improved on every
prompt mode while general-text loss rose.

**Stage-1 scorecard against the §19 predictions:** 1 papers ≥ 10 % PASSED
(−18.9 %; −11.2 % on the papers' own text); 2 replay ≤ +3 % FAILED (+4.4 %);
3 structure similarity ≤ 0.476 FAILED (0.616), Raman ≤ 0.60 PASSED (0.447);
4 recall bars are stage-2 bars — bands already 51 %, formula 38 %, crystal 20 %,
inverse 0 %; 5 probe species: formula / crystal ≥ base PASSED, "bands near
floor" premise void (low-exposure, not withheld; 44 %); 6 §18 name-mode ≥ 0
PASSED (+0.06); 7 as expected.

### Stage 2 launched (2026-09-09 15:40) — branch, rate, and what changed from the plan

**Branch: the epoch-2.0 endpoint**, chosen by the probes (recall higher on every
task and both species sets; Raman-prompt polymorph similarity 0.447 vs 0.579;
Quartz bands recited) over the epoch-0.93 checkpoint that sat inside the replay
bound. Init is `stage1/final_fp32` — checkpoint-4282's fp32 weights with the
tokenizer copied beside them — so stage 2 does not start from the bf16-rounded
`final/`. The §18 head instrument was still running at launch; it measures
backbone representation for the regression task and could annotate this choice
but not reverse it against direct recall evidence, so the launch did not wait.

**Rate: 1e-5, linear to 0, 20-step warmup, fresh AdamW** — half the plan's
"2e-5 if stage-1 shows forgetting", because forgetting continued at 2e-5.

**Evaluation: five sets.** `package.py stage2` wrote only `val-shapes`; the
pre-registered stage-2 criteria need papers (rise ≤ 2 %) and replay (≤ base
+3 %), so stage 1's `val-paper_markdown`, `val-replay`, `val-reference` and
`val-binder_markdown` are symlinked into the stage-2 dir (the trainer globs
`val-*.bin`). Eval every 40 steps (11 + start), saves at 200/400, `python -u`
so the log is live.

**Smoke (20 steps, same init and rate):** masked-loss path trains — shapes val
0.6215 → 0.4796 during warmup alone, train loss 0.76 → 0.59, truncated papers /
replay flat (1.832 → 1.849, 2.107 → 2.125), peak 21.25 / 8.18 GiB, 1,865–1,889
tok/s. Step-0 binder loss 2.527 = the stage-1 final 2.5266, confirming the fp32
init carries the endpoint's weights.

**Expected:** 440 steps ≈ 4.2 h + ~20 min of evals → done ≈ 20:25. Stage-2
acceptance (§5): shapes val falls; papers val rises ≤ 2 %; replay ≤ 2.1815
(already failed at stage 1, so the honest bar is "no further rise"); recall
formula ≥ 80 %, crystal system ≥ 70 %, bands ≥ 50 %, inverse top-1 ≥ 40 %,
trained-minus-probe gap ≤ 15 points; polymorph structure ≤ 0.476 and crystal
system right for ≥ 5/7 pairs; digits Quartz/Calcite first band top-1 with
p ≥ 0.5; §18 name-mode delta ≥ 0.

### Stage 2 result (2026-09-09 20:02): shapes learned in 40 steps, the second pass cost prose

440 steps / 28.8M tokens in 4 h 22 min (6.58M tok/h end-to-end with five eval
sets every 40 steps); endpoint `stage2/final` (bf16), fp32 in `checkpoint-440`;
`stage2_epoch1` preserves the step-200 fp32 weights (epoch 0.91).

| step | shapes | paper_markdown | replay | reference | binder |
|---|---|---|---|---|---|
| 0 (stage-1 endpoint) | 0.6493 | 1.3180 | 2.2120 | 0.4035 | 2.527 |
| 40 | 0.4319 | 1.3260 | 2.2260 | 0.3871 | 2.426 |
| 120 (shapes minimum) | **0.4267** | 1.3270 | 2.2330 | 0.3505 | 2.351 |
| 200 (epoch 0.91) | 0.4359 | 1.3260 | 2.2340 | 0.3263 | 2.462 |
| 239 (first eval of pass 2) | 0.4338 | **1.3400** | **2.2530** | 0.3190 | 2.583 |
| 440 (final) | 0.4315 | 1.3420 | 2.2580 | 0.2977 | 2.639 |
| final vs stage-1 endpoint | −33.5 % | **+1.82 %** | +2.08 % | −26.2 % | +4.4 % |

**Loss criteria.** Shapes val fell: PASSED, but all of it in the first 40 steps
(warmup, lr ≤ 1e-5) and flat from step 120 — 320 further steps bought nothing
measurable on shapes. Papers ≤ +2 %: PASSED at +1.82 %, with the whole rise
arriving at the epoch boundary (1.326 → 1.340 between steps 200 and 239) — the
stage-1 second-pass pattern again, now on 108k Q→A examples. Replay rose a
further +2.08 % (2.258 = +6.6 % over base; the +3 % bound was already gone).
Reference prose kept improving to −26 %: training the question frames
sharpened the statement frames too. Binder drifted +4.4 % (2 documents).

**Reading.** One epoch at 1e-5 would have delivered the same shapes loss at a
lower prose cost — `stage2_epoch1` (step 200: shapes 0.436, papers +0.6 %,
replay +1.0 %) is the comparison point if the probes show the final's
recall no better than the epoch-1 model's. Whether the second pass bought
FACTS (inverse recall, crystal system) rather than loss is what the probes
decide next; loss cannot see it.

**Stage-2 polymorph probe: structure collapse got worse; Raman held.** Same
run for all three models (greedy bf16 on the 3060; the base's structure figure
reads 0.517 here vs 0.476 on the 3090 this morning — greedy flips on kernel
differences, so compare within a run):

| model | Raman prompts | structure prompts | crystal system right (of 13 minerals) | pairs both right (of 7) |
|---|---|---|---|---|
| base | 0.799, 4/7 identical | 0.517 | — | — |
| stage-1 endpoint | 0.484 | 0.612 | 5 of 12 stated | — |
| **stage-2 endpoint** | 0.488 | **0.723** | **6 of 13** | **1 of 7** |

The stage-2 model answers every "X (Y) is" prompt in one frame — "<system>
(space group Pnma) with unit cell a = …" — and fills the system slot at about
the base rate of orthorhombic/monoclinic: Brookite "hexagonal", Pyrite
"orthorhombic", Andalusite "monoclinic", Atacamite/Botallackite swapped. The
criterion (≥ 5/7 pairs) FAILS at 1/7, and 0.723 is within reach of run 2's
0.784. Bands were learned; structure was not — the mindat structural sentence
(one frame per species) is exactly the "N copies of one phrasing" template the
frame rule was written against, and the structure facts need the same
treatment the Raman facts got: several distinct frames per fact plus
measurement-keyed anchors (the AMCSD cell/space-group records) rather than a
single mindat sentence.


### Stage 2 probes (2026-09-09 21:25): the shapes unlocked formulas and crystal systems, the inverse is still dead

**Recall (`probe_recall.py`, greedy, trained frame / probe frame):**

| task | bar | base | stage-1 endpoint | **stage-2 endpoint** | probe species (97), stage 2 |
|---|---|---|---|---|---|
| formula | ≥ 0.80 | 0.01 | 0.21 / 0.38 | **0.615 / 0.565** | 0.598 / 0.474 |
| bands (≥2 of 3 within ±10) | ≥ 0.50 | 0.00 | 0.34 / 0.505 | **0.625 / 0.570 PASS** | 0.474 / 0.567 |
| crystal_system | ≥ 0.70 | 0.09 | 0.17 / 0.20 | **0.467 / 0.452** | 0.443 / 0.454 |
| inverse (bands → species) | ≥ 0.40 | 0.00 | 0.00 | **0.000 FAIL** | 0.000 |
| trained − probe gap | ≤ 0.15 | — | negative | **+0.05 / +0.055 / +0.015 PASS** | formula +0.12 |

Formula +40 points and crystal system +30 points from a stage that did not add
knowledge: the Q→A frames unlocked what stage-1 prose had deposited. Bands
clear their bar under both wordings. Formula (61.5 %) and crystal system
(46.7 %) miss theirs, and the shapes loss was flat from step 120, so more
shape training will not close that gap — the missing part is knowledge the
prose never carried in a recoverable form (crystal systems entered as one
mindat sentence per species). **Inverse is exactly zero at every checkpoint**
and the digit probe shows the collapse in its current form: all three band
lists → "Pyrope, whose composition is Mg3Al2(SiO4)3." (stage 1: Tsumebite).
3,862 inverse examples against 26,130 forward ones taught a single favourite
answer; the inverse needs its own design (every measurement-keyed spectrum as
an inverse example, contrastive negatives, candidate-list answers) before it
can be scored.

**Digits (stage-2 endpoint):** Calcite → 203.8, 279.6, 667.1, 1087.1 (1087 and
280 within 2 cm⁻¹ of 1085 / 282; 667 and 204 wrong); Quartz → 165.3, 178.6,
291.3, 403.1, 431.5 (none within 10 — stage 1 had four of five). Single-prompt
greedy output is noisy; the 200-species recall is the measure. First-token
probabilities are far below the p ≥ 0.5 bar (see log) — FAIL, as expected for a
model that ranks rather than knows the first band.

**Polymorph:** Raman 0.488 PASS (≤ 0.60); structure 0.723 FAIL (base 0.517 in
the same run); crystal system right for 6 of 13 minerals, 1 of 7 pairs (bar
5/7) FAIL — see the stage-2 polymorph section above.

**§18 head instrument, stage-2 arm (5 paired seeds, last-10 means):**

| prompt | metric | base | stage 1 | stage 2 | Δ s2−base (t) | Δ s2−s1 (t) |
|---|---|---|---|---|---|---|
| name | val | 0.4233 | 0.4858 | 0.4949 | +0.0716 (6.9) | +0.0091 (2.0) |
| name | holdout | 0.4074 | 0.4452 | 0.4662 | +0.0588 (11.1) | +0.0210 (4.3) |
| formula | val | 0.6005 | 0.6222 | 0.6281 | +0.0275 (6.7) | +0.0058 (1.5) |
| formula | holdout | 0.5901 | 0.6163 | 0.6139 | +0.0238 (4.4) | -0.0024 (-0.6) |
| both | val | 0.5798 | 0.6135 | 0.6159 | +0.0361 (6.9) | +0.0024 (0.8) |
| both | holdout | 0.5860 | 0.5897 | 0.5894 | +0.0034 (1.7) | -0.0002 (-0.1) |

No damage from stage 2; name-mode holdout improved a further +0.02 over stage 1
(t ≈ 5). Prediction 6 holds at both stages.

**Stage-2 scorecard:** loss — shapes fell PASS, papers +1.82 % PASS (≤ 2 %),
replay +6.6 % over base FAIL (bound gone since stage 1); polymorph — Raman
PASS, structure FAIL, systems FAIL; digits FAIL; recall — bands PASS, gap PASS,
formula / crystal / inverse FAIL; probe species — formula/crystal ≥ base PASS;
§18 PASS. **Seven of fifteen criteria pass.** The failures are one mechanism
(single-template structure facts) plus one missing design (the inverse), not a
training defect: everything that was given several frames was learned.

**Was the second shape epoch worth its prose cost? Roughly a wash.** Recall on
`stage2_epoch1` (step 200, papers +0.6 %, replay +1.0 %) against the final
(step 440, papers +1.82 %, replay +2.1 %), trained / probe frame:

| task | epoch 1 | final | difference |
|---|---|---|---|
| formula, seen | 0.610 / 0.555 | 0.615 / 0.565 | none |
| bands, seen | 0.550 / 0.560 | 0.625 / 0.570 | **+7.5 / +1** |
| crystal_system, seen | 0.457 / 0.492 | 0.467 / 0.452 | +1 / **−4** |
| bands, probe species | 0.495 / 0.433 | 0.474 / 0.567 | −2 / **+13** |
| crystal_system, probe species | 0.443 / 0.495 | 0.443 / 0.454 | 0 / −4 |
| inverse, both sets | 0.000 | 0.000 | none |

The second pass bought bands (the oracle's primary target) and cost a little
crystal-system generalisation and 1.2 points of papers loss; formula and the
inverse did not move. **Decision: the final stays the stage-2 endpoint**, both
artefacts are kept (`stage2_epoch1` fp32; `stage2/final` bf16 + `checkpoint-440`
fp32), and "one shape epoch" enters the next-corpus levers as a near-free
saving rather than a correction.

### Why the inverse direction is zero: the key is ill-posed before the model ever sees it (2026-09-10)

`templates.fields()` builds the inverse prompt from `top4 = bands[:4]`, and `bands` is the
position-sorted peak-pick — so "top4" is the **four LOWEST-wavenumber peaks**, the lattice-mode
region every mineral shares, never the diagnostic bands (Abellaite's key is 118.6, 128, 138.5,
201.7; its 1058 carbonate band is absent). Measured over the 1,914 RRUFF species with ≥ 4 picked
peaks, using the field's own ±10 cm⁻¹ tolerance:

| key | median band | share < 250 cm⁻¹ | another species matches all 4 (±5 / ±10 / ±15) | ≥3 of 4 within ±10 |
|---|---|---|---|---|
| **4 lowest (as trained)** | 238 cm⁻¹ | 55 % | 21 % / **56 %** / 76 % | **93 %** |
| 4 strongest (by relative intensity, available from `pick_peaks`) | 503 cm⁻¹ | 19 % | 4 % / **11 %** / 19 % | 64 % |

A perfect lookup table over the trained keys would be ambiguous for 56 % of species. The model
cannot learn a mapping the data does not contain, so it learned the only regularity available —
the answer FORMAT ("{species}, whose composition is {formula}") — and fills it with a prior
("Tsumebite" after stage 1, "Pyrope" after stage 2). Everything downstream compounds it:
- **Exposure:** 2 stage-1 statements + 2 stage-2 Q/A per species (~6 exposures over both stages)
  against ~25+ forward exposures; only the canonical fact renders an inverse — the 5,787
  measurement-keyed spectra render forward only, so the inverse gets none of the natural variation
  that made the forward direction learnable.
- **Reversal curse:** forward exposure ("Quartz shows bands at …") does not transfer to the
  backward mapping in autoregressive LMs (Berglund et al. 2023); the operator's stage-2 hypothesis
  that pairing directions would lift the inverse ran into this — only direct backward exposure
  counts, and there was almost none.
- **Numeric keys** tokenize into digit pieces ('108','5'); a 1B model has to hash 8–12 such
  tokens into a name, whereas the forward key is a name with pretrained associations.
- **No negatives:** 201 contrastive + 186 polymorph examples; nothing says what a band set is NOT.

**Levers for v5, in order of expected effect:** (1) fix the key — strongest-N by relative
intensity, present strongest-first, and state the diagnostic band explicitly; (2) render an
inverse example from EVERY measurement-keyed spectrum (≈ 3× exposure with real variation), plus
candidate-list answers ("… is most consistent with X; also Y, Z") for the confusable 11 %;
(3) contrastive negatives from nearest-neighbour keys; (4) keep the direction ratio near 1:1 in
the shape stage. Probe: recompute the inverse recall on the same 200 species after (1) alone.

## 20. Inverse-identification experiment — pre-registration (2026-09-10)

**Question.** Does the inverse direction (peak list → species) learn at all once the
prompt is the seeker's own output over real spectra, with the variation coming from
data rather than wording? v4 scored 0.000 with a key that was ambiguous for 56 % of
species (§19 root cause).

**Design** (`corpus_inverse.py`, `package_inverse.py`, `probe_inverse.py`):
- Prompt = `identify_prompt(peaks, laser)`: the project's `pick_peaks` output, 12
  strongest peaks, position-sorted, relative intensities attached, one fixed schema.
  Completion = `species (formula)`, plus a deterministic "also consistent with" tail
  for the 196 species whose four strongest bands collide with another's at ±10 cm⁻¹.
- Data: every RRUFF spectrum in eight archives (excellent/fair/poor/unrated,
  oriented and unoriented, LR-Raman) = 15,600 train spectra over 2,481 species;
  4 seeker re-runs per spectrum over perturbed copies (calibration shift, gain
  envelope, fluorescence baseline, noise, window truncation, smoothing, threshold
  jitter); the same peak lists rendered in BOTH directions (identify / predict), 1:1.
- Held out: one excellent_unoriented spectrum per species with ≥ 2 (n = 1,388,
  in-distribution instrument) and ALL of ROD (n = 1,043, unseen instrument family).
- Training: from `stage1/final_fp32` (fact-trained, not shape-trained, so the
  comparison with v4's stage 2 is clean), lr 1e-5 linear→0, **one epoch** (v4's
  second shape pass bought loss only and cost prose), eval every 20 steps on
  identify_heldout, identify_rod, paper_markdown, replay, reference, shapes_v4.
- Control: nearest-neighbour peak matching over the train library (`probe_inverse.py
  --models ""`), same items, same tolerance.

**Predictions, falsifiable.**
1. Held-out top-1 ≥ 0.30 and top-3 ≥ 0.50 (v4: 0.000). Below 0.10 = the format
   thesis is wrong and the blocker is elsewhere.
2. Distinct first answers over the 1,388 held-out items ≥ 200 (v4: one answer for
   everything). This is the collapse test and it is pass/fail before accuracy matters.
3. The model stays BELOW the kNN control on held-out library spectra (a lookup over
   the same library should win there); the interesting gap is on ROD, where I expect
   the model within 10 points of kNN or above it if the augmentation taught invariance.
4. ROD top-1 ≥ 0.10 for species seen in training (unseen instrument transfer).
5. Papers val rises ≤ 2 %; shapes_v4 val WILL rise (old format untrained) and that
   is not a failure; the forward `predict` direction is not scored this run.
6. Expected not to work: species with a single training spectrum and no ROD entry.

**As built and launched (2026-09-10 12:33).** Archives yielded 15,600 train spectra
(excellent_unoriented 4,399 after the 1,388 hold-out; oriented 1,798 + 13; fair 1,481;
poor 808; unrated 190; LR-Raman 3,352), 155,620 examples (77,810 per direction), 190 of
62,400 perturbed re-picks discarded for < 3 peaks. **24.8M tokens/epoch** — numbers
tokenise at ~156 tokens per example, 1.8× the character estimate — 6,059 blocks, pad
1.97 %, 378 steps, eval every 40. One render fix before packaging: the picker's
`relative_intensity` is relative to the whole spectrum's range (baseline included), so
the strongest listed peak read 0.68 while the schema promised 1.00; `pick()` now
renormalises the listed peaks. kNN control library = 15,600 train peak lists; 816 of
the 1,043 ROD spectra name a species present in training. Log
`~/tmp/train_inverse_exp.log`, out `inverse_exp/`, init `stage1/final_fp32`, lr 1e-5
linear→0, warmup 20. Expected ≈ 3.6 h + ~25 min of evaluations.

**kNN control, measured before any result (12:34):** held-out RRUFF top-1 **0.488** /
top-3 0.654 (n = 1,388); ROD top-1 **0.576** / top-3 0.638 (n = 1,043, of which 816
name a species present in training — so 0.78 is the ceiling for any method on ROD).
These are the bars the model's identify scores are read against; prediction 3 says the
model lands below 0.488 on held-out RRUFF, and the ROD comparison is the one that
matters.

**Early signal (steps 40 / 80 of 378, 13:24).** identify_heldout loss 1.397 → 0.536 →
0.451 (−68 %): the schema and at least some of the mapping are being learned fast.
identify_rod 2.999 → 3.082 → 3.355 (+12 %) is **an artefact of the completion, not the
identification**: 350 of 400 ROD completions carry CIF-style spaced formulas ("Fe3 O4",
"F4 Li Y") and some ROD "species" are formula-like names, while RRUFF formulas are
unspaced with RRUFF markup ("Sn^2+^21O6(OH)14Cl16", "[box]Ca2(…)"); as the model commits
to the RRUFF style its loss on the CIF spelling rises. `probe_inverse.py` scores the
species NAME (first phase, and anywhere in the text) and is immune to this — read ROD
from the probe, never from this loss curve. **Owed for the next render:** a formula
normaliser (strip CIF spaces, RRUFF `^…^` and `[box]`) so both sources spell formulas
one way. Prose cost is running higher than v4's stage 2: papers +2.6 % at step 80 (bar
2 %, lr still near peak), reference frames +88 %, shapes_v4 +46 % — the expected price of
a pure-schema stage with no carried prose; the recipe run must carry prose, this
experiment deliberately does not.

**Run complete (16:27, 234 min, 378 steps, 24.8M tokens, 6.34M tok/h end-to-end).**
Endpoint `inverse_exp/final` (bf16), fp32 in `checkpoint-378`. Final losses vs the
stage-1 endpoint: identify_heldout 1.397 → **0.359** (−74 %, still falling at the last
eval); identify_rod 3.00 → 4.05 (the CIF-formula artefact, not read); paper_markdown
1.318 → 1.371 (**+4.0 %, FAILS the ≤ 2 % bar**); replay +1.4 %; reference frames 0.40 →
0.90 (+124 %); shapes_v4 0.65 → 1.22 (+89 %). The prose/fact cost of a pure-schema
stage with no carried prose is now measured, and it is large. Probes launched 16:28:
`probe_inverse.py` (s1 vs inv, kNN bars 0.488 / 0.576) on the 3090 and `probe_recall.py`
(seen species, s1 vs inv) on the 3060 to quantify what the reference-loss rise cost in
formula / band recall.

### §20 result: the format thesis is FALSIFIED — the peak list is not read at all (2026-09-10 17:20)

**Probe (`probe_inverse.py`, greedy, species-name scoring), kNN control beside it:**

| model | split | top-1 | top-3 | distinct first answers | most common answer |
|---|---|---|---|---|---|
| kNN over the train library | heldout | **0.488** | 0.654 | — | — |
| kNN | ROD | **0.576** | 0.638 | — | — |
| stage-1 endpoint (untrained format) | heldout | 0.000 | 0.000 | 3 | "$\alpha$-Fe$_2$O$_3$" (paper prose) |
| **inverse endpoint** | heldout | **0.001** | 0.001 | **9** | Beryl 897/1,388 |
| inverse endpoint | ROD | 0.006 | 0.006 | 8 | Beryl 571/1,043 |

Predictions 1 (top-1 ≥ 0.30) and 2 (≥ 200 distinct answers) are falsified outright;
3 holds trivially; 4 fails. The −74 % identify loss was the completion's format and
formula tokens — predictable once a species is chosen — not the decision tokens.

**Two diagnostics say the model never used the peak list.** `rank_inverse.py`: scoring
the true species by likelihood against the 63 most frequent training species on 200
held-out items puts the true species in the BOTTOM eighth of 64 for 92.5 % of items
(top half 4 %) — likelihood tracks species frequency, nothing else. Prompt sensitivity:
log P(true | own peaks) − log P(true | another spectrum's peaks) = **+0.08 nats, own
higher for 52 % of items** (chance 50 %; stage-1 endpoint 46 %). The mapping is not
buried under a prior; it is absent. The collapse targets (Beryl, Fluorapatite, Epidote,
Diopside) are hub species — 3rd–5th most frequent in training; median species has 15
identify examples, 543 species ≤ 5.

**Cost to the forward facts** (`probe_recall.py`, seen species, probe frame): formula
0.405 → 0.185, bands 0.490 → 0.410, crystal system 0.198 → 0.320; papers +4.0 %,
reference frames +124 %. A pure-schema stage with no carried prose halves formula recall.

**Reading.** At 1.5B, one epoch, ~30 seeker-schema exposures per species, a decimal peak
list (~60 digit tokens) does not become a key the model can look up: reading a dozen
numbers and matching them against memory is a retrieval operation, and the generation
readout learned P(species) plus the format. The forward direction worked because the
NAME is the key (one or two pretrained tokens) and the numbers are the output. Fixing
the key (§19) was necessary — the v4 key was ill-posed — but it was not the blocker
this experiment isolated.

**Where this points, in order of cost:**
1. **Retrieval in the loop** (the spectroscopist's workflow): seeker output → kNN
   candidates from the library (already 0.49 / 0.58 top-1) → the LM chooses and explains
   among K candidates whose reference peak lists are IN the prompt. A comparison task
   over 5 options, not a hash over 2,481; trainable from the same data; the LM's value
   is the reasoning (shifts, missing bands, mixtures) plus its prose knowledge. This is
   the product path.
2. **Cheap diagnostic before anything else:** a linear/MLP head over the FROZEN
   endpoint's representation of the identify prompt → species (the §18 instrument
   reversed). If a head on frozen features scores > 0.2 top-1, the information is in the
   representation and generation is the wrong readout; if ~0, the encoding itself is
   opaque to this model.
3. **Symbolic spectral tokens** (10 cm⁻¹ bins with intensity levels as single tokens)
   so a species becomes a bag of ~12 "words" like a name — the research direction if the
   LM itself must internalise spectra.
4. More epochs or the 7B on the Mac are gambles against a zero-sensitivity result and
   should follow (2), not precede it.

Artefacts: `inverse_exp/final` (+ fp32 `checkpoint-378`, `checkpoint-200`);
probes `~/tmp/analysis/inverse_exp/`; kNN control `inverse_knn_control.json`.
The formula normaliser (`formula_norm.py`, 8 tests) is unaffected and stands for v5.

### §20b. Frozen-representation head over the identify prompt — pre-registration (17:30)

**Question.** Is species information present in the model's representation of the peak
list at all, with generation ruled out as the readout? `head_inverse.py`: encode every
training identify prompt (77,810, all augmentations) with a FROZEN model, pool the final
layer (last token; mean) and a middle layer (mean), train a linear softmax head and a
2048→1024→2481 MLP head over species, evaluate top-1/3/10 on the 1,388 held-out RRUFF
spectra and on ROD (816 items whose species exist in training). Models: inverse
endpoint, stage-1 endpoint, base. Controls: kNN (0.488 / 0.576) and the same heads over a
10 cm⁻¹ intensity-binned peak vector (130 dims) — what a representation-free classifier
extracts from the identical inputs.

**Predictions.** (1) Binned-vector heads land near kNN (0.35–0.55 held-out top-1). (2)
Base-model features score low (< 0.05): pretraining does not encode digit lists as
spectra. (3) The decision: if the inverse endpoint's features reach > 0.20 top-1 the
information is in the representation and generation was the wrong readout; if they sit
below the binned vector by a wide margin the encoding is opaque to this model and the
retrieval-in-the-loop path is the only one left at 1.5B. (4) Expected: inverse endpoint >
stage-1 endpoint > base, all below the binned vector.

**§20b result (18:16, 31 min).** Head top-1 on the 1,388 held-out RRUFF spectra / the 816
ROD items whose species exist in training; chance 0.0004; kNN 0.488 / 0.576.

| features | linear, held-out / ROD | MLP, held-out / ROD |
|---|---|---|
| **binned peak vector, 130 dims (control)** | 0.486 / 0.605 | **0.624 / 0.725** |
| inverse endpoint, last token ("Phase:") | 0.090 / 0.320 | 0.101 / 0.392 |
| inverse endpoint, final-layer mean | 0.326 / 0.665 | 0.329 / 0.668 |
| inverse endpoint, layer-8 mean | 0.324 / 0.659 | 0.318 / 0.685 |
| stage-1 endpoint, last token | 0.258 / 0.656 | 0.256 / 0.657 |
| stage-1 endpoint, final-layer mean | 0.313 / 0.673 | 0.349 / 0.694 |
| stage-1 endpoint, layer-8 mean | 0.318 / 0.681 | **0.361 / 0.702** |
| base, last token | 0.192 / 0.586 | 0.183 / 0.632 |
| base, layer-8 mean | 0.294 / 0.670 | 0.336 / 0.686 |

Four findings, each pre-registered as a possibility:
1. **The information is in the representation** (prediction 3, upper branch): a linear
   probe on the mean-pooled hidden state reads the species at 0.31–0.36 top-1, far above
   the 0.20 line, while generation from the same model scored 0.001 with zero prompt
   sensitivity. Generation was the wrong readout.
2. **Training did not add it.** Base ≈ stage-1 ≈ inverse endpoint on pooled features
   (0.34 / 0.36 / 0.32). Whatever a probe can read is what pretraining already encodes
   about digit strings; 24.8M tokens of peak lists moved nothing readable. Prediction 4
   (inverse > stage-1 > base) is falsified.
3. **The generation objective destroyed the decision position.** At the last token,
   where generation reads, the stage-1 endpoint carries 0.26 and the inverse endpoint
   0.09 — one epoch of identify training collapsed that state toward the frequent-species
   answer. The "Beryl" behaviour is now mechanistic, not descriptive.
4. **Pooling beats the last token everywhere, and a 130-dim bin histogram beats every
   LM feature** (0.62 vs ≤ 0.36 held-out). The species signal is spread across the number
   tokens and never integrated at the decision point; the LM's encoding of decimals keeps
   about half of what a trivial binning keeps. On ROD the gap nearly closes (0.70 vs
   0.725): LM features generalise across instruments about as well as bins do.

**Reading for direction.** For identification itself the LM adds nothing over a 130-dim
histogram and a small MLP (0.62 / 0.72, above kNN). The LM's value is downstream —
reasoning about shifts, missing bands and mixtures, and the prose knowledge — which
argues for the classifier or kNN proposing candidates from the seeker output and the LM
choosing and explaining among candidates it can SEE (copying a visible name is easy; recall
from a numeric key is what failed). If the LM itself must carry spectra, finding 4 points at
the input: bins as symbolic tokens make a spectrum a bag of ~12 words, the representation
the MLP already exploits, and the one the generation readout could plausibly learn.

## 21. Synthetic-corpus pilot (Raman vs LIBS) — pre-registration (2026-09-11)

**Question.** Does massively diverse synthetic restatement of structured facts —
≥ 20 structurally distinct framings per fact, both directions, 60–120 spaced
exposures, a source tag on every document, mixed pretraining-style — raise a
1.5B OLMo-2's recall of those facts, measured on framings it never saw? And
does the effect differ between an instrument the paper corpus supports richly
(Raman: 577 accepted papers, 32k mentions) and one it barely mentions (LIBS:
307 papers, 22k mentions)?

**Why now.** §19–§20b: v4 exposed each fact through ~10 framings, forward-heavy;
recall on unseen framings sat at formula 0.38 / bands 0.51 / crystal 0.20 after
stage 1 (0.615 / 0.625 / 0.467 after the anneal), bands→species 0.000, and the
seeker-schema run FALSIFIED the format thesis while the head probe showed the
species signal present in pooled states (0.33) but not at the decision token.
Literature: Allen-Zhu & Li 3.1 (1 wording 9.7 % → 5 wordings + permutations
96.6 %; augmentation must be in pretraining, Q/A mixed early), 3.3 (source
token recovers junk-mixing losses), EntiGraph (entity-relation texts, log-linear;
paraphrase-only saturates), Ovadia (~10 paraphrases saturate — where v4 sat),
Chang et al. 2024 (paraphrase decays slower than duplication; smaller batches
lower the learnability threshold), reverse training (mirrored frames).

**Instrument.** The `synth` flow set (flows/synth; agent/actions/synth_*.py):
muse (local) and Haiku (LLMVP `claude_cli` route, capped) write prompt/completion
TEMPLATES whose every value is a `{slot}`; a deterministic gate rejects digits,
literal minerals, unknown slots, wrong-direction layouts, duplicates and the v4
/ probe frames; `dev/rock_olmo/synth_render.py` fills templates from the facts
layer with sampled instrument variance (`synth_variance.py`) and fact-graph
relations; `package_synth.py` packs; `probe_recall.py --target-species` scores.

**Design, frozen before the final render.**

| Item | Value |
|---|---|
| Species groups (`synth_species.json`, sha256 724a276ed9b5…) | eligible 1,705 (formula + canonical Raman + crystal system; all LIBS-eligible); T_RL 100, T_L 200, T_R 200, C 500; stratified by tertiles of prose mentions (medians 13 / 12.5 / 14 / 14); 100 probe species untouched |
| Synthesised per group | all targets: formula, structure; T_R∪T_RL: raman_bands (+ contrastive/polymorph/ir where present); T_L∪T_RL: libs_lines; T_RL adds cross_instrument; C: nothing |
| Families (14) → cells | definition, structure_card, measurement_report, identification (seeker schema), catalogue_entry, variance_note, contrast, qa, tabular, group_membership, polymorph_family, band_neighbourhood, cross_instrument, provenance; ~50 (kind, family, direction, form) cells; ≥ 20/15/12 accepted templates per cell by family (≈ 760 at target) |
| Exposures | 12 templates per fact per direction per variant, stratified over families; 3 variants (re-picked templates, re-drawn instrument) → ~72 framings per fact; 5 relational docs per direction per variant |
| Held-out framings | template_id hash % 10 == 0 never trained; rendered once per fact per direction → `val-synth_holdout` |
| Instrument variance | Raman: class mix lab .50 / portable .35 / handheld .15; σ 1.2 / 3.0 / 3.5 cm-1 plus a 0.25-share constant offset U(4–9) on non-lab units (calibrated to RRUFF within-species |Δ| median 1.3, p75 3, p90 5–6, p95 8–9); excitation 532/785/633/488/830 at .45/.35/.10/.05/.05; low cut-offs and bandwidth-dependent weak-band loss; strongest-band flip p = 0.29 when top two within 0.15. LIBS: positions ±0.02–0.10 nm, T 8–12 kK, per-stage intensity scaling, self-absorption 35 %, windows 200–500 / 350–900 / 190–1040 nm |
| Presentation | full peak lists with intensities (strongest listed = 1.00), never strongest-N alone |
| Mix | 60 % synthetic / 20 % carried v4 prose (ALL reference docs of the 1,000 species — targets AND controls — then random stage-1 prose) / 20 % replay; `[source: …]` tag line on every document; `formula_norm` applied to carried prose at packing |
| Training | FROM BASE `~/models/OLMo-2-0425-1B` (operator ruling: measure, then stage 1 in a follow-up); `train_full.py --stage 2` (linear→0, 20 warmup), lr 4e-5 (the only rate measured to move recall here), `--accum 8` = 32k tokens/step, one pass over the 3-variant stream (~17M tokens ≈ 530 steps), eval every 40 steps |
| Val sets | synth_holdout, reference, paper_markdown, replay, shapes_v4 |
| Probes | `probe_recall.py --target-species synth_species.json --group {T_R,T_L,T_RL,C}` on base and the endpoint: formula, crystal_system, bands, inverse, identification (perturbed seeker list), libs_lines (±0.2 nm), libs_inverse, cross_modal; trained-frame vs probe-frame |

**Predictions (probe frames unless stated; scored against the base model on the
same items).**

| # | Prediction | Falsified if |
|---|---|---|
| P1 | Targets formula ≥ 0.50 and crystal system ≥ 0.40; targets − controls ≥ 25 points on both (controls saw the same carried reference docs and no synthetic) | either bar missed |
| P2 | Seeker-schema identification on T_R canonical spectra top-1 ≥ 0.10 (v4: 0.000; chance ≈ 0.002) | < 0.05 — structural diversity + mirrored frames do not fix the readout at this scale |
| P3 | T_R bands ≥ 0.40 and T_L libs_lines ≥ 0.40; T_R − T_L ≥ 10 points if natural-frequency support matters; T_L ≥ T_R means synthetic exposure substitutes for it | both below 0.40 |
| P4 | T_RL cross_modal top-1 ≥ max(T_R identification, T_L libs_inverse) + 5 points; T_R species asked LIBS questions ≈ base (no unseen-instrument transfer) | cross-modal below the better single instrument |
| P5 | Trained-frame − probe-frame gap ≤ 15 points on every task; synth_holdout loss falls monotonically | gap > 15 (template collapse) |
| P6 | Forgetting: paper_markdown val ≤ base + 2 %, replay ≤ base + 3 % | either exceeded |
| P7 | ≥ 90 % of correct formula answers use the plain-textbook spelling | < 90 % |

Expected NOT to work: LIBS lines→species where the line set is dominated by
shared major elements (Fe/Ca/Si); species with a single noisy spectrum; anything
the 100 untouched probe species are asked (they get no synthetic docs and are
reported as the third population, not scored against P1–P7).

**Not tuned post hoc.** lr, exposures, mix and the gate thresholds are fixed
here; a change means a new section, not an edit. To be filled at freeze: bank
size and cell coverage from `docs_manifest.json`, packed shares from
`manifest.json`, base-model baselines on every val set and probe.

**Freeze (2026-09-11 19:13 local).** Bank `bank/templates.jsonl` frozen at
882 rows (sha256 ba113a9a7522ec00…): muse 545, Haiku
337; 16 rounds; 50/50 cells populated, 12 cells short by 1–3 (all
≥ 9); 31 pre-fix backward contrast rows carry the member names in `{lines}`
and are excluded by the filler's re-gate, leaving 851 usable templates
(150 unused because no fact of the group could fill them), 83 held out.
Gate fixes during the bank (all bug fixes, none a threshold change): slot names
may carry case/digits; unit spellings are not digits; `geometry` is a structure
answer; backward contrast templates use anonymised lists. Render
(`docs_manifest.json`): 121,449 synthetic docs (identity
62,294, raman 23,545, libs 18,871, relational
16,739); groups T_RL 31,327 / T_L 40,408 / T_R 49,714;
exposures per fact min 33 / median 71 / max 240 over 1,639 facts;
2,959 held-out-framing docs; 0 documents lost a number; realised Raman |Δ|
quantiles {'0.5': 1.2, '0.75': 2.3, '0.9': 4.7, '0.95': 6.6}. The base-model probe baseline
runs AFTER training on the same items (base and endpoint in one call): the
base model cannot be influenced by training, and 16,000 single-prompt
generations would have idled both GPUs for two hours before the run.

**Pack (`manifest.json`, seed 20260911).** 17,166,813 tokens in 4,370 blocks
(pad 4.1%): synthetic 60.0% (10,300,100), carried reference
19.9% (3,410,299; 60% of the 1,000 species' 41,133
reference docs, subsampled by a seeded shuffle uniformly over species so targets
and controls keep the same natural exposure), carried prose 0.1%, replay
20.0% (3,433,352). DEVIATION from the design table, recorded before training:
"ALL reference docs of the 1,000 species" would have been 33 % of the stream
because the character estimate undercounts numeric text by almost half; the
60/20/20 mix and the target/control symmetry were kept and the reference set
subsampled instead. Val: synth_holdout (2,959 docs, 63 blocks), reference,
paper_markdown, replay, shapes_v4. One pass = 546 steps at 32k tokens/step.

### 21b. Results — from-base pilot (run 2026-09-12 08:50–11:54)

`train_full.py --stage 2 --init OLMo-2-0425-1B --epochs 1 --lr 4e-5 --accum 8
--eval-steps 40`: 546 steps, 17.89M tokens, 5.83M tok/h, peak 21.2 / 8.2 GB;
endpoint `~/models/olmo2-1b-spectra-full/synth_pilot/final` (checkpoints 400,
546 kept for a resume). Eval on start = the base model on the same val sets.

| val set | base | endpoint | Δ | min over run | P6 bound |
|---|---|---|---|---|---|
| synth_holdout (unseen framings) | 2.062 | 1.114 | −46.0 % | 1.048 (step 40) | — |
| reference (v4 frames) | 1.719 | 0.998 | −41.9 % | 0.937 | — |
| shapes_v4 (stage-2 shapes) | 1.036 | 0.719 | −30.6 % | 0.619 | — |
| replay | 2.118 | 2.165 | +2.2 % | 2.118 | ≤ +3 % ✓ |
| paper_markdown | 1.626 | 1.707 | +5.0 % | 1.626 | ≤ +2 % ✗ |

Reading: the held-out FRAMING loss falls to its floor within 40 steps (1.05)
and then drifts up slightly as the wording is saturated — the framings are
learned almost at once; whether the FACTS transferred is the probes' question
(below). P6 splits: replay holds within bound, paper markdown does not (+5 %),
so the pilot's stream is prose-poor for the papers' register (carried prose
was 0.1 % because the reference docs consumed the carry budget). A follow-up
should carry paper prose explicitly inside the 20 % share.

**Probes** (`probe_recall.py --target-species synth_species.json --group all
--models base,pilot`, batched greedy, 60 new tokens; 1,000 species × 8 tasks ×
2 frames; the untouched 100 probe species in a second call). PROBE-frame
accuracy, base → endpoint; C = controls (same carried reference docs, no
synthetic); U = the 97 untouched probe species (no synthetic, no carried docs).

| task | T_R | T_L | T_RL | C | U |
|---|---|---|---|---|---|
| formula (exact, normalised) | 0.000→**0.875** | 0.005→**0.870** | 0.010→**0.890** | 0.002→0.208 | 0→0.000 |
| crystal_system | 0.015→0.530 | 0.005→0.595 | 0.000→0.510 | 0.008→0.290 | 0.01→0.351 |
| bands (≥2/3 within ±10) | 0.000→0.550 | 0.000→0.335 | 0.000→**0.700** | 0.000→0.388 | 0→0.351 |
| libs_lines (≥2/3 within ±0.2 nm) | 0→0.905 | 0→0.910 | 0→0.950 | 0→0.916 | 0→0.856 |
| inverse (4 bands → species) | 0→0.010 | 0→0.000 | 0→0.000 | 0→0.000 | 0→0 |
| identification (seeker list → species) | 0→0.000 | 0→0.000 | 0→0.000 | 0→0.000 | 0→0 |
| libs_inverse / cross_modal | 0→0.005 / 0.000 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

Trained-frame (v4 question frames, seen only through the carried reference
docs) vs probe-frame for targets: formula 0.62–0.65 vs 0.87–0.89, bands
0.28–0.47 vs 0.34–0.70 — the NEVER-trained probe frame scores higher than the
v4 frame; gaps are negative (−0.06 to −0.28), so no template collapse
(P5 ✓ in spirit; the pre-registered bound was written for the other sign).

**Chance floors the untouched species expose (post hoc, reported, not used
to move a bar).** crystal_system has a ~0.3 floor (a frequent-system answer);
bands has a ~0.35 floor (a long plausible list hits 2 of 3 within ±10 by
chance); libs_lines is element→line knowledge because the frame shows the
formula (0.86 on species the run never saw). formula (exact string) has no
floor and is the clean readout.

**Verdicts.**
- P1 ✓ formula: targets 0.87–0.89, controls 0.21 (Δ +67 points; bar ≥ 0.50 and
  Δ ≥ 25 both met). crystal system: targets 0.51–0.60 ≥ 0.40 ✓; Δ vs controls
  +22 / +31 / +22 — met for T_L only; against the ~0.3 floor the controls sit at
  chance, the targets do not.
- P2 ✗ FALSIFIED: identification 0.000 on every group (chance 0.002; bound
  < 0.05). The endpoint answers the same handful of frequent names (Cebaite-(Ce),
  Tantalite-(Fe), Eulytine, Cervantite, Sphalerite) whatever the list — the §20
  "Beryl" collapse reproduced after ~24 backward framings per fact, both
  seeker-schema and short-question forms. Structural diversity + mirrored frames
  do NOT fix the numeric-key readout at 1.5B.
- P3 ◐ T_R bands 0.55 ≥ 0.40 ✓; T_L's LIBS bar is met (0.91) but the task
  measures element knowledge, so the Raman-vs-LIBS natural-frequency contrast is
  INCONCLUSIVE from this probe. On identity facts the two instrument arms are
  indistinguishable (formula 0.875 vs 0.870; crystal 0.53 vs 0.60).
- P4 ✗ cross_modal 0.000 (backward). Forward: T_RL bands 0.70 vs T_R 0.55 —
  the cross-instrument documents lifted forward Raman recall by 15 points.
- P5 ✓ (no collapse; probe frame ≥ trained frame). P6 ◐ replay +2.2 % ✓,
  paper markdown +5.0 % ✗. P7 ✓ by construction (exact normalised match).

**Reading for direction.** The forward, identity-style facts respond exactly
as the literature predicts: ~24 framings × 3 variants took formula recall
from 0.21 (reference frames only) to 0.88 on unseen framings, with the 100
untouched species confirming it is species memory, not format. The backward
numeric-key direction did not move at all, in agreement with §20b's head
probe: the information is present but the generation readout collapses to
frequent names. The stage-1-initialised follow-up should keep the forward
recipe and drop the backward families in favour of a retrieval-in-the-loop
design (candidates proposed from the seeker output, the LM choosing and
explaining among names it can SEE) — copying a visible name is what this
model does well.

### 21c. Follow-up from the stage-1 endpoint — pre-registration (2026-09-12)

Operator ruling: same corpus (`v5/synth_pilot`, manifest sha as §21b), same
recipe (`--stage 2 --epochs 1 --lr 4e-5 --accum 8 --eval-steps 40`), init
`stage1/final_fp32` instead of base; no mid-run checkpoints (disk), endpoint
`synth_pilot_s1/final`. Probes: stage-1 init, the base-init endpoint and this
endpoint on identical items (all groups + the untouched probe species).

Predictions, falsifiable:
1. Controls start higher (stage 1 already carries v4 exposure: formula 0.38,
   bands 0.51, crystal 0.20 on probe frames) and the synthetic INCREMENT on
   targets persists: formula targets ≥ 0.85 and targets − controls ≥ 25 points.
2. Paper-markdown forgetting shrinks: Δ vs the stage-1 init ≤ +2 % (stage 1
   was trained on the papers; the base-init run lost 5 %).
3. Backward tasks stay at zero (the readout, not the exposure, is the limit).
4. Held-out framing loss reaches a lower floor than the base-init run (1.05).

### 21c. Results — stage-1-initialised follow-up (run 2026-09-12 12:50–16:35)

546 steps, 17.89M tokens, 5.84M tok/h, endpoint `synth_pilot_s1/final`.

| val set | stage-1 init | endpoint | Δ | min |
|---|---|---|---|---|
| synth_holdout | 2.060 | 1.056 | −48.7 % | 1.020 |
| reference (v4 frames) | 0.404 | 0.615 | +52.5 % | 0.404 |
| shapes_v4 | 0.649 | 0.764 | +17.7 % | 0.649 |
| replay | 2.212 | 2.264 | +2.4 % | 2.212 |
| paper_markdown | 1.318 | 1.401 | +6.3 % | 1.318 |

Probe-frame accuracy, stage-1 init → endpoint [base-init endpoint from §21b]:

| task | T_R | T_L | T_RL | C | U (97 untouched) |
|---|---|---|---|---|---|
| formula | 0.240→**1.000** [0.875] | 0.235→**1.000** [0.870] | 0.220→**1.000** [0.890] | 0.236→0.418 [0.208] | 0.206→0.134 |
| crystal_system | 0.165→0.885 [0.530] | 0.155→0.890 [0.595] | 0.150→0.890 [0.510] | 0.146→0.618 [0.290] | 0.227→0.505 |
| bands | 0.490→**0.900** [0.550] | 0.505→0.520 [0.335] | 0.550→**0.920** [0.700] | 0.484→0.522 [0.388] | 0.464→0.402 |
| inverse | 0→0.055 | 0→0 | 0→0.030 | 0→0 | 0→0 |
| identification | 0→0.030 | 0→0 | 0→0.010 | 0→0 | 0→0 |
| libs_lines (element knowledge) | 1.0→1.0 | 1.0→1.0 | 0.98→1.0 | 0.99→0.99 | 0.99→0.98 |
| libs_inverse / cross_modal | ≤ 0.010 | ≤ 0.005 | 0 | 0 | 0 |

Trained-frame (v4 question frames) for targets: formula 0.80–0.87, crystal
0.83–0.87, bands 0.66–0.71 — again below the never-trained probe frame (gaps
−0.02 to −0.26): no template collapse.

**Verdicts (§21c predictions).**
1. ✓ Controls start higher (formula 0.24 vs 0.00 from base) and the increment
   persists and grows: targets formula 1.000, controls 0.418 (Δ +58 points).
   Crystal system 0.89 vs 0.62 (Δ +27). Raman bands 0.90–0.92 for the Raman
   arms vs 0.52 for controls AND for T_L (no Raman synthetic docs) — the
   contrast is instrument-specific, as designed.
2. ✗ Paper-markdown forgetting did not shrink (+6.3 % vs +5.0 % from base):
   a property of the stream (no paper prose in the carry share), not of the
   initialisation.
3. ✓ (as bounded) Backward stays near zero: identification 0.030 and inverse
   0.055 on T_R (chance 0.002) — the first non-zero backward signal, still far
   below §21's 0.10 bar; the generations remain frequent-name guesses.
4. ✓ marginally: held-out framing floor 1.020 vs 1.048.

**Comparison with the base-init run.** Same corpus, same recipe: initialising
from stage 1 adds +11 to +13 points on formula (to saturation), +30 to +38 on
crystal system, +22 to +35 on Raman bands for the Raman arms. Stage 1's prior
exposure to these species and the synthetic framings compound. Controls also
gain (+21 formula, +33 crystal) from the shared framings and the carried
reference docs, and the untouched species lose a little formula recall
(0.21→0.13) while gaining the crystal-system format — the celebrity/format
effects the pre-registration warned about, visible and bounded.

**Standing conclusions after two runs.** (i) Forward identity and Raman facts
are learnable at 1.5B with ~24 framings × 3 variants per fact, to ceiling when
stacked on stage 1. (ii) The numeric-key backward readout does not respond to
framing diversity, mirrored frames or initialisation (≤ 0.055 everywhere).
(iii) The LIBS-line probe measures element knowledge; a species-specific LIBS
test needs a frame without the formula. (iv) Carry paper prose explicitly in
the next stream. Next direction per §20b and §21b: retrieval-in-the-loop for
identification (candidates the model can SEE), forward recipe unchanged.

## 22. v3: FIM primer → pile → XML fill anneal — pre-registration (2026-09-19)

**Question.** (i) Can a 1B OLMo-2 acquire the fill-in-the-middle (FIM)
sentinels its tokenizer reserves — `<|fim_prefix|>` 100258, `<|fim_middle|>`
100259, `<|fim_suffix|>` 100260, embeddings untrained (row norm 0.88 against
10.8 for common tokens) — at fine-tune scale, as CONDITIONING on the suffix
and not merely the grammar? (ii) Does a FIM blank over an intensity-ranked XML
record lift the numeric-key backward direction (bands → species) above the
≤ 0.055 ceiling of §19–§21c, while the symbolic slots (formula ↔ species) hold
≥ 0.90?

**Why now.** §21c standing conclusions (i)–(iv). The single-key backward probe
(`probe_backward_identity.py`, 2026-09-19): after the synth pilot's mirrored
framings, formula → species reaches 0.97–0.99 on synth targets (controls 0.01,
untouched 0.00) while structure → species (cell parameters, a numeric key)
stays ≤ 0.011 and bands → species ≤ 0.055 — the wall is the approximate
numeric key, not direction. GLM (Du 2022) avoids the reversal curse with blank
infilling in pretraining; Bavarian 2022 shows FIM is a data transformation a
next-token model learns from rearranged documents; OLMo 3 applies FIM in
mid-training only. Operator decisions (2026-09-19): scraper paused for this
pass; init from BASE; stage 0 = clean dolmino-mix-1124 (pes2o-heavy; OLMo 3's
midtraining mix carries 5 % real science PDFs and ~50 % synthetic and is not
used); stage 1 = v2's recipe with more tokens, copies ≤ 2 and a ~5 % FIM share
on replay documents only; stage 2 = confined XML records (top-4 Raman bands and
top-4 LIBS lines by intensity, species / formula / system attributes) with ONE
FIM blank each, middle-only loss, numeric-key mitigations DEFERRED; stage-0
budget gated on the running smoke.

**Measured before the design (2026-09-19).** The FIM smoke (28.8 M tokens from
`stage1/final`, lr 2e-5, accum 8, 877 steps; §22 smoke) shares its source
shards with `replay.py`: 3,196 of its 24,383 documents are replay documents,
40 of the 117 `val-replay` documents among them (text-prefix match) — the smoke
endpoint is never used for a replay-bound claim, and the stage-0 driver excludes
every replay document by dolmino id and first-2000-char hash. Under the
paragraph packing rule ~26 % of the smoke's FIM documents straddled a block, so
their middles trained without prefix or suffix (`Packer.add_document(atomic=)`
fixes this; the smoke UNDER-states achievable recovery). The old recovery items
(154 wiki sentences, world-knowledge blanks) are replaced by class-stratified
items over all val documents: `copy_suffix` (the answer also occurs verbatim in
the suffix only — the conditioning test), `copy_prefix` (control), `free`.

**Design, frozen before the stage-0 launch.**

| Stage | Item | Value |
|---|---|---|
| 0 | source | dolmino-mix-1124 shards pes2o-0025 + pes2o-0024, wiki-0001, dclm-0001 (ODC-BY); `fim_transform.py --clip 8000 --fim-rate 0.9 --exclude-jsonl v4/replay/replay.jsonl` |
| 0 | mix | 55 / 25 / 20 pes2o / wiki / dclm by est. tokens; middles: short span 20–200 chars ½, uniform split ½; PSM ½ / SPM ½; `fim/*` documents packed atomically |
| 0 | val | 120 val documents per subset written twice (plain + rearranged twin) → `val-plain` / `val-fim` on the same documents; remaining 1 % val documents held out of training and used only for recovery items; `val-paper_markdown` borrowed from v4 as the domain guard |
| 0 | budgets | S ≈ 150 M tokens (~22 h) or L ≈ 300 M (~44 h), both packed in advance; GATE below |
| 0 | training | `train_full.py --stage 0` (= stage-1 schedule: constant after 5 % warmup), lr 2e-5 (the smoke's rate), accum 8 = 32k tokens/step, one epoch, eval every 400 steps, `--save-final-fp32` |
| 0 | gate | on `fim_smoke/final` with the NEW items (`copy_suffix` exact, PSM): ≥ 0.50 → S; 0.20–0.50 → L; < 0.20 → hold and report (options then: L at 3e-5, or no stage 0 with the grammar learned in stage 2 — confounded). Written before the number lands; the only branch in this section |
| 1 | pile | `corpus_stage1.py --root v6`: accepted papers with markdown (`.en.md` preferred) incl. supplements, binder ×2 (v4 ×4), packs (val with their paper), reference facts ×4 distinct frames + 2 inverse (unchanged), hom / webmineral / mindat ×2 (v4 ×3); val fractions hom 3 % / webmineral 2 % / mindat 8 % / binder 10 % (nested in the 1 % split) |
| 1 | replay | v4 `replay.jsonl` (20 M tokens, ≈ 15 %) with ⅓ of its TRAIN documents rearranged with the sentinels (`fim/<subset>`; planned ≈ 5 % of the stage, realised 2.8 % — the rearranged copies are clipped to 8,000 chars); replay val rows never rearranged; `val-fim` borrowed from the stage-0 S pack (dolmino documents disjoint from replay) so the sentinel loss is read through stages 1 and 2 |
| 1 | training | from `v3_stage0/final_fp32`; `--stage 1`, lr 4e-5, accum 16 = 65k tokens/step, 2 epochs; §19 halving rule: `val-replay` > 1.03 × its step-0 value at any eval → resume from the last checkpoint at 2e-5 (`--override-lr`), a second crossing recorded, not acted on |
| 2 | records | one canonical record per species (`corpus_xml.py`): `<mineral species formula system>` + `<raman laser_nm><top>…</top><next>…</next>×3</raman>` (integers, strongest 4 by `rel`) + `<libs><line>…</line>×4</libs>` (2 dp, `strongest_lines`); attribute-order × block-order permutations ×4; variants `full` and `raman_only`; 1,689 trained species; the 96 `probe_species.json` species with facts (U) never rendered; held-out probe permutation `formula system species` never rendered |
| 2 | blanks | one per record, `fim_wrap` PSM and SPM: species, identity (`species="…" formula="…"`), formula, raman (inner), libs (inner), top, system → 96 FIM examples + 6 plain appearances (3 seeded 8-record bundles × 2 variants, `[source: reference/xml]`) per species ≈ 19.7 M XML tokens |
| 2 | mix | XML 65 % / carried paper prose 20 % / carried reference + other prose 7 % / replay 8 % ≈ 30 M tokens; FIM rows loss on middle + EOS only (`Packer.add_example`, verbatim tokenisation), plain bundles and carried prose full loss |
| 2 | training | from `v3_stage1/final_fp32`; `--stage 2` (linear → 0, 20 warmup), lr 2e-5, accum 8, one epoch (818 steps over the packed 6,545 blocks), eval every 40, mid checkpoint at the half-epoch step 410 |
| all | probes | `probe_fim_recovery.py` (by class), `probe_recall.py` (seen 200 + probe set), `probe_backward_identity.py --max-new 60`, `probe_xml_fill.py` (kinds × variants × orders × groups; trained vs held-out permutation), base + every stage endpoint on identical items |

**Predictions.**

| # | Prediction | Falsified if |
|---|---|---|
| S0-1 | stage-0 endpoint `copy_suffix` exact ≥ 0.70 (S) / ≥ 0.60 (L); `free` ≥ 0.10; base and v2 stage 1 read 0.000 on every class | `copy_suffix` below the bar |
| S0-2 | `val-plain` ≤ base + 1 %; `val-fim − val-plain` shrinks from step 0 and is non-increasing over the last 5 evals | either violated |
| S0-3 | borrowed `val-paper_markdown` ≤ 1.626 × 1.02 (no domain change from clean text) | exceeded |
| S0-4 | sentinel embedding row norms ≥ 3.0 (from 0.88) | any row < 3.0 |
| S1-1 | `val-paper_markdown` ≤ 0.90 × its step-0 value (v2: −18.9 %) | > 0.90 × |
| S1-2 | `val-replay` ≤ 1.03 × step 0, or the halving fired and was logged | exceeded without the rule firing |
| S1-3 | `val-binder_markdown` ≤ step 0 + 10 % (v2: +36 % at ×4) | > +10 % |
| S1-4 | recall probe frame on the seen 200: formula ≥ 0.35, bands ≥ 0.50 (v2 stage 1: 0.38 / 0.51) | either below |
| S1-5 | `copy_suffix` at the stage-1 endpoint ≥ 0.5 × the stage-0 value (the 5 % replay share keeps the skill) | below |
| P1 | `species` fill (formula visible) ≥ 0.90 lenient AND `formula` fill ≥ 0.90 (held-out permutation, trained species) | either < 0.90 |
| P2 | `raman` fill ≥ 0.625 (v2 stage-2 bands) | < 0.625 |
| P3 | `identity` fill on `full` records ≥ 0.10 lenient (chance ≈ 0.001); informal goal: any value > 0.055 | < 0.10 — FIM blanks do not lift the numeric backward direction |
| P4 | `libs` fill ≥ 0.90 | < 0.90 |
| P5 | vs the stage-1 endpoint's step-0 losses: `val-paper_markdown` ≤ +2 %, `val-replay` ≤ +3 % | either exceeded (then the step-470 checkpoint is the endpoint, recorded) |
| P6 | U species `species` / `identity` fill within ±5 points of the stage-1 endpoint on the same items; `probe_recall --probe-set` formula / bands ≥ stage-1 endpoint − 5 points | either violated |
| P7 | recall probe frame on the seen 200: formula ≥ 0.60, bands ≥ 0.60, crystal ≥ 0.45; `formula_to_species[probe]` strict ≥ 0.90 | any below |
| P8 | trained − held-out permutation gap ≤ 0.15 on every kind; `val-xml_fim` reaches its floor within the first 40 % of steps | gap > 0.15 (schema collapse) |

**P9–P11 (operator, added 2026-09-21 22:40Z — before stage 2 runs, after the
stage-1 halving; nothing about stage 2 has been measured).** Operator's
prediction: stage 2 may regain what stage 1 eroded of the sentinel skill,
since fill on the XML schema is precisely what stage 2 trains. Formalised as
three predictions, because "the FIM loss" and "the fill behaviour" come
apart under this stream:

*Why they come apart.* `val-fim` is dolmino PROSE, up to 2,000 tokens,
scored on every token, so its value is dominated by ordinary continuation of
the prefix and suffix; only a few tokens per document sit at the middle. The
stage-2 FIM rows are ~115-token XML records trained with loss on the MIDDLE
ONLY (§22 design), so they deliver almost no prose-continuation signal —
that comes from the stream's 27 % carried prose and 8 % replay. The
recovery probe, by contrast, scores exactly the behaviour at the middle.

| # | prediction (stage-2 endpoint vs the stage-1 endpoint on the same instrument) | falsified if |
|---|---|---|
| P9 | `val-fim` recovers at least 25 % of the gap the stage-1 run opened (from the stage-0 anneal endpoint's 2.248 to whatever stage 1 ends at) | it recovers < 25 %, or rises further |
| P10 | The 402 PROSE recovery items: PSM `copy_suffix` at the stage-2 endpoint ≥ its stage-1 endpoint value; the strong form, ≥ the stage-0 endpoint's 0.179, is reported separately | it falls below the stage-1 endpoint value |
| P11 | In-domain fill is not in doubt and is the control: `val-xml_fim` falls monotonically to a floor and the XML `species` fill clears P1. If P10 holds while P11's in-domain numbers are strong, narrow schema training TRANSFERS back to open-domain infilling; if P10 fails while P11 holds, the fill skill is schema-bound | — |

Either outcome of P10 is decision-relevant for a v4. Transfer would mean the
49 M-token primer bought little that 26 M tokens of in-domain blanks could
not, i.e. stage 0 is skippable and its 2–19 % drift tax avoidable.
Schema-binding would mean the identity-fill result (P3) is a statement about
this schema rather than about fill in general, and it would have to be
reported that way.

Expected NOT to work: `identity` on `raman_only` records whose strongest-4 key
is shared within ±10 cm⁻¹ by another species (~11 %, §19); `top` completion
above chance; anything about U beyond P6.

### 22e. Stage-2 result and the §22 scorecard (run 2026-09-22 16:07–20:59Z)

818 steps, 26,804,224 tokens, 4.8 h; endpoints `v3_stage2/final`,
`final_fp32`, mid checkpoint 410 kept. Losses vs the run's step 0: xml_fim
0.8067 → 0.2854 (floor 0.145 at step 120, then a monotone rise to 0.285),
xml_plain 0.9848 → 0.6001, paper 1.3180 → 1.3394 (+1.6 %), replay 2.2520 →
2.2886 (+1.6 %), prose fim 2.3610 → 2.4252 (+2.7 %).

**XML fill, trained species (T = 200 seeded, V = the 19 val species, U = the
100 untouched), lenient; base and stage 1 read ≤ 0.06 on every row.**

| blank kind | what it asks | T | V | U | trained − held-out permutation |
|---|---|---|---|---|---|
| libs | lines from the rest | **1.000** | 0.908 | 0.968 | 0.000 |
| raman | bands from the rest | **0.949** | 0.197 | 0.142 | 0.062 |
| formula | formula from species | **0.925** | 0.638 | 0.522 | 0.082 |
| top | strongest band given three | **0.923** | 0.204 | 0.124 | 0.051 |
| system | crystal system | **0.920** | 0.421 | 0.505 | 0.065 |
| species | species, FORMULA VISIBLE | **0.688** | 0.000 | 0.003 | −0.011 |
| identity | species + formula, NUMERIC KEY ONLY | **0.014** | 0.000 | 0.000 | 0.000 |

**The decisive contrast is inside one model, one schema, one record.** The
`species` blank (0.688) and the `identity` blank (0.014) differ in exactly
one respect: whether the formula stays visible beside the gap. Removing the
symbolic co-key and leaving the intensity-ranked bands, the LIBS lines and
the crystal system collapses recall by a factor of 49. §19–§21c inferred
this from separate probes; here it is a within-record manipulation. Fill is
not the lever for an approximate numeric key.

Generalisation splits as designed: for V and U the SCHEMA transfers (libs
0.91/0.97 from the formula's chemistry, formula 0.64/0.52, system 0.42/0.51)
while the species-specific numbers do not (raman 0.20/0.14, top 0.20/0.12) —
the model learned the format for every species and the facts only for the
ones it was shown.

**Other instruments.** Recall, probe frame, seen 200: formula 0.520 → 0.855
(v2 stage 2: 0.615), bands 0.675 → 0.665 (0.625), crystal 0.183 → 0.198
(0.467). Backward identity in a never-trained PROSE frame, 1,077 species:
species→formula 0.555 → 0.832; formula→species 0.006 → **0.051** (8.5×, from
the XML identity blank alone — real transfer out of the schema, but far
below §21c's 0.97 from mirrored prose framings); structure→species 0.001.
Prose recovery items: PSM copy_suffix 0.201 → 0.187, SPM 0.545 → 0.500.

**Scorecard.**

| # | prediction | result | verdict |
|---|---|---|---|
| P1 | species ≥ 0.90 AND formula ≥ 0.90 | 0.688 / 0.925 | ✗ (species) |
| P2 | raman ≥ 0.625 | 0.949 | ✓ |
| P3 | identity ≥ 0.10 (informal > 0.055) | 0.014 | ✗ |
| P4 | libs ≥ 0.90 | 1.000 | ✓ |
| P5 | paper ≤ +2 %, replay ≤ +3 % | +1.6 %, +1.6 % | ✓ |
| P6 | U within ±5 pts; probe-set recall ≥ s1 − 5 pts | U fill ✓; probe-set bands 0.640 → 0.490 | ✗ (−15 pts on untouched bands) |
| P7 | formula ≥ 0.60, bands ≥ 0.60, crystal ≥ 0.45; formula→species strict ≥ 0.90 | 0.855 ✓, 0.665 ✓, 0.198 ✗, 0.044 ✗ | ✗ |
| P8 | permutation gap ≤ 0.15; xml_fim floors in the first 40 % | max gap 0.082; floor at 15 % | ✓ (with the later rise noted) |
| P9 | val-fim recovers ≥ 25 % of the stage-1 loss | +2.7 %, moved away | ✗ |
| P10 | prose PSM copy_suffix ≥ the stage-1 value | 0.187 vs 0.201 | ✗ (within noise; held, did not recover) |
| P11 | in-domain control strong | xml_fim −64.6 %, T fill 0.92–1.00 | ✓ |
| S0-1…4 | primer bars | see §22a/§22b | ✗ ✗/✓ ✗ ✗ |
| S1-1…5 | stage-1 bars | paper 1.318 ✓, replay +3.4 % ✗, binder −13.0 % ✓, recall ✓, sentinel 82 % ✓ | 4 of 5 |

**Standing conclusions.** (i) A fill blank teaches every slot of a rigid
record to near-ceiling from the rest of the record, including the symbolic
backward slot when a symbolic co-key is visible (0.688), and fails at the
approximate numeric key (0.014) — the §21c wall reproduced within a single
record. (ii) Confining stage 2 to the schema cost the prose crystal-system
frame (0.198 against v2's 0.467, which taught it as Q→A prose) and 15 points
of band recall on untouched species, while buying +24 points of formula
recall over v2. (iii) Fill training does not transfer back to open-domain
infilling (P10) and does not restore the prose fill loss (P9); the skill is
schema-bound, so P3 is a statement about this schema. (iv) The XML identity
blank did move the symbolic backward direction in prose 8.5-fold, which is
the one transfer this stage produced.

**Mid checkpoint vs endpoint (probe 2026-09-22 21:39Z, same 15,615 prompts).**
Trained / val / untouched, lenient:

| blank | step 410 | step 818 |
|---|---|---|
| species | 0.406 / 0.000 / 0.003 | 0.688 / 0.000 / 0.003 |
| identity | 0.006 / 0 / 0 | 0.014 / 0 / 0 |
| formula | 0.844 / 0.599 / 0.460 | 0.925 / 0.638 / 0.522 |
| raman | 0.887 / 0.184 / 0.164 | 0.949 / 0.197 / 0.142 |
| libs | 0.999 / 0.882 / 0.968 | 1.000 / 0.908 / 0.968 |
| top | 0.869 / 0.191 / 0.130 | 0.923 / 0.204 / 0.124 |
| system | 0.842 / 0.395 / 0.448 | 0.920 / 0.421 / 0.505 |

(a) The val-xml_fim rise (0.145 → 0.285) was calibration, not behaviour:
greedy fill on the 19 held-out species IMPROVED from step 410 to 818 on
formula, raman, libs, top and system. The loss rose because the model grew
more confident, so its wrong answers on unseen species cost more; its argmax
did not get worse. The endpoint stands as the artefact and P8's caveat is
closed. (b) The symbolic backward slot is the last thing the schema teaches:
species fill 0.406 → 0.688 across the second half while every forward slot
was near ceiling by the midpoint, still rising at the end. A longer stage 2
could plausibly carry P1's species number toward 0.90; identity (0.006 →
0.014) gives no such sign, so more steps would not rescue the numeric key.

**Head-to-head with v2 under today's probes (2026-09-22 22:10Z; correction).**
The §22e comparisons to "v2 stage 2" quoted §19's numbers, which were scored
against v2's own gold. Since §21, facts carry the `formula_norm` spelling
(`CaSO4·2H2O`), while v2 was trained on the raw IMA spelling (`CaS6+O4.2H2O`),
so today's exact-string scorer marks v2 down for spelling alone. Re-run on the
SAME 200 seen species and 100 untouched species, probe frame:

| task | v2 stage 2 | v3 stage 1 | v3 stage 2 |
|---|---|---|---|
| formula, as scored (exact string) | 0.275 | 0.535 | 0.845 |
| formula, spelling-fair (generation passed through `normalize_formula`) | **0.725** | 0.695 | **0.925** |
| bands (first three by position, ±10) | 0.555 | 0.675 | 0.665 |
| crystal system | **0.426** | 0.183 | **0.198** |
| inverse (bands → species) | 0.000 | 0.000 | 0.000 |
| formula → species, prose probe frame | 0.009 | 0.006 | 0.051 |
| untouched: formula / bands / crystal (as scored) | 0.310 / 0.460 / 0.440 | 0.540 / 0.640 / 0.270 | 0.650 / 0.490 / 0.260 |

Spelling-fair formula recall is +20 points over v2 (0.925 vs 0.725; the
earlier "+24" compared different items against different golds), and v3's
stage 1 alone (0.695) nearly matches v2's finished two-stage model. The
exact-string scorer also fails hydrates written with "." instead of "·"
(v3 writes "." in prose and "·" in XML), which is most of v3's 0.845 → 0.925.
Crystal system is a genuine −23 points against v2. Both stage-2 methods cost
untouched-species band recall to the same level (0.46–0.49). If `copy_prefix` ≫ `copy_suffix` at
the stage-0 endpoint the primer taught the grammar only, and P3 is reported as
uninterpretable rather than as a falsification of FIM blanks.

**Not tuned post hoc.** Rates, budgets, mixes, blank kinds, val floors and every
bar are fixed here; the gate is the one branch and its rule precedes its input.
A change means a new subsection.

**Measured before freeze (2026-09-19 21:50Z; the smoke still running).**
Contamination 3,196 / 40 and straddle 5,539 of 21,663 FIM train documents
(25.6 %) as above. Stage-0 documents (`fim_manifest.json`): pes2o 79,988
rearranged + 9,040 plain (176.0 M est. tokens, two shards), wiki 122,837 +
13,756 (80.0 M), dclm 57,704 + 6,745 (64.0 M); 7,819 replay documents
excluded (2,578 / 2,527 / 2,714); 120 val documents written per subset (735 /
1,316 / 507 further val documents held out of training for the items);
recovery items 402 = 134 per class, 67 numeric each. Packs: L 69,626 blocks =
285,188,096 tokens, pad 1.56 % (shares fim/pes2o .489, fim/wiki .235,
fim/dclm .175, plain .055 / .026 / .020); S 37,191 blocks = 152,334,336
tokens, pad 1.54 % (same shares ±0.01); val-fim 108 blocks, val-plain 96,
borrowed val-paper_markdown 114. Stage-1 pile (`corpus_stage1 --root v6`):
2,256 papers (359 English translations) + 406 supplements + 23 binder, 139,887
documents; probe species re-verified against the pile, none swapped. Pack:
33,804 blocks = 138,461,184 tokens/epoch (pad 7.0 %; v2: 140.3 M); weighted
shares paper 53.3 %, webmineral 10.9 %, reference 9.4 %, replay plain 10.2 %,
replay-FIM 2.8 %, hom 5.3 %, supplement 4.4 %, mindat 1.5 %, binder 1.2 %,
pack 1.0 %; val blocks paper 137, reference 31, replay 63, binder 38, hom 29,
webmineral 34, mindat 22, supplement 6, pack 3 (the last two reported only),
fim 108 (borrowed). Two epochs = 4,224 steps at 65k tokens/step. Stage-2
render (`corpus_xml`): 1,685 trained species (19 val), 100 untouched;
161,704 FIM rows (species / identity / formula / raman / top 26,960 each,
libs 13,480, system 13,424), 1,272 bundles. Pack (`package_xml`, middle
loss): 6,545 blocks = 25,894,534 real tokens, pad 3.4 %, 0 rows dropped;
shares xml_fim 61.5 % + xml_plain 3.6 % = 65.0 %, paper 20.0 % (178 papers),
other 7.0 %, replay 8.0 %; val xml_fim 45 blocks, xml_plain 3, borrowed
paper_markdown / replay / reference / fim.

**Gate reading (2026-09-19 23:06Z).** Smoke: 877 steps, 28,737,536 tokens,
6.86 M tok/h; val-fim 2.361 → 2.238 (−5.2 %, flat over the last ~200 steps),
val-plain 2.241 → 2.209 (−1.4 %). New items (PSM), exact by class:

| model | copy_suffix | copy_prefix | free | all | numeric |
|---|---|---|---|---|---|
| base | 0.000 | 0.007 | 0.000 | 0.002 | 0.005 |
| v2 stage 1 (`stage1/final`) | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| smoke (`fim_smoke/final`) | **0.187** | 0.239 | 0.052 | 0.159 | 0.154 |

`copy_suffix` 0.187 < 0.20 → the HOLD branch (n = 134, s.e. ≈ 0.034; one
item below the L band). Base losses on the v6 stage-1 val sets (`--eval-only`,
the S1 bars' denominators are the stage-1 run's own step 0, but these are the
reference): paper_markdown 1.6019, supplement_markdown 1.6033, binder_markdown
1.5972 (a different val draw from §19's 1.852), reference 1.6924, replay
2.1195, hom 2.5022, webmineral 2.3150, mindat_prose 2.6666, pack_prose 2.3731,
fim (borrowed stage-0 val) 2.3087. Sentinel embedding rows: base 0.897 / 0.879 / 0.884,
smoke 0.900 / 0.881 / 0.887 — they did not move. Adam bounds a coordinate's
drift by lr × steps (2e-5 × 877 ≈ 0.018), and with sign-noisy gradients the
rows random-walk by ~0.03; the acquired infilling therefore lives in the
transformer layers reading the untrained-but-distinct sentinel vectors, and
S0-4 (norms ≥ 3.0) is expected to FAIL as written at any fine-tune rate. It is
kept as stated (a falsified mechanism prediction is a result). The operator
decides the branch; the branch taken is recorded in §22a below, not edited
into the design table.

### 22a. Stage-0 branch taken: L at 3e-5 with a check-in schedule (2026-09-19 23:25Z)

Operator ruling on the HOLD reading: run the L pack at lr 3e-5 (the pre-listed
HOLD option) and pre-register a CHECK-IN SCHEDULE with expected results, so a
run that gains nothing over the smoke is caught early instead of after 44 h.

Second reading, taken before the ruling: the same 402 items in SPM order
(suffix, prefix, middle — the middle follows the prefix directly) on the smoke
endpoint read `copy_suffix` **0.515**, `copy_prefix` 0.522, `free` 0.134, all
0.391 (base ≈ 0 in both orders). The suffix conditioning exists after 28.7 M
tokens; PSM — where the model must leave the suffix's end and continue the
prefix — is the harder order, and the gate stays defined on PSM.

Design as the §22 table with two changes: lr **3e-5** (constant after the 5 %
warmup = 435 steps), and `packed_L` (285,188,096 tokens, 69,626 blocks, 8,703
steps at 32k tokens/step, ≈ 42 h at the smoke's 6.86 M tok/h). Checkpoints
every 500 steps (two kept); each new checkpoint is probed on CPU beside the
training (PSM and SPM, all 402 items; `checkin_stage0.sh`, ~10 min) and the
reading appended to `~/tmp/analysis/v3/stage0_checkins/checkins.jsonl`.

| check-in (step ≈ tokens) | PSM `copy_suffix` ≥ | SPM `copy_suffix` ≥ | note |
|---|---|---|---|
| 1,000 (33 M ≈ the smoke's budget) | 0.20 | 0.50 | ≥ the smoke, on the un-straddled pack at the higher rate |
| 2,000 (66 M) | 0.30 | 0.55 | |
| 4,000 (131 M) | 0.40 | 0.60 | |
| 6,000 (197 M) | 0.50 | 0.65 | |
| 8,703 endpoint (285 M) | 0.60 (S0-1) | 0.65 | `free` ≥ 0.10 |

STOP RULE (written before the run): PSM `copy_suffix` rises by < 0.05 across
two consecutive check-ins while still < 0.50, or reads below the smoke's 0.187
at any check-in from step 1,000 on → stop the run, report, decide (the smoke's
plateau repeated at a higher rate would mean the budget is not the lever).
val-fim (every 400 steps) is expected non-increasing; a rise of > 1 % held over
three evaluations is a stop-and-look. A check-in below its bar but above the
stop rule continues and is reported as a miss against this table.

**§22a result (2026-09-20 07:39Z): STOPPED at step 1,500 by the rule.**
Check-ins (CPU, fp32 checkpoints, 402 items): PSM `copy_suffix` 0.157 / 0.231 /
0.179 at steps 500 / 1,000 / 1,500 (s.e. ≈ 0.035 — one flat line from 16 M
tokens), SPM 0.545 / 0.537 / 0.560; step 1,500 read below the smoke's 0.187.
Losses (step 0 / 400 / 800 / 1,200): fim 2.310 / 2.239 / 2.247 / 2.250; plain
2.195 / 2.215 / 2.234 / 2.239; borrowed paper 1.626 / 1.664 / 1.690 / 1.704;
fim − plain gap 0.115 → 0.011 (the format is learned; the absolute rise is the
re-warming bump of restarting an annealed model at a constant rate plus the
90 % fill tax). Re-windowing the identical blanks to 150 / 400 characters did
not rescue PSM (checkpoint-1500: 0.128 / 0.138; smoke 0.231 / 0.188) while SPM
held at 0.46–0.50 for both — the model fills from the side ADJACENT to the
generation point and has not learned the PSM switch. Two inits (base, v2
stage 1) at two rates (3e-5, 2e-5) reached one ceiling; sentinel embedding rows
unmoved (0.88). Verdicts as of the stop: S0-1 ✗, S0-2 ✗ (absolute) / ✓ (gap),
S0-3 ✗ (+4.8 %), S0-4 ✗. Checkpoints 1,000 and 1,500 kept.

**Anneal of checkpoint-1500 (run 2026-09-20 15:46–17:38Z, `v3_stage0_anneal`).**
348 steps, linear 3e-5 → 0 from a fresh optimizer. Losses step 0 → 160 → 320
(end): fim 2.252 → 2.249 → 2.248, plain 2.242 → 2.238 → 2.238, paper 1.709 →
1.708 → 1.711. The decay recovered NOTHING of the drift (−0.004 on fim and
plain, +0.002 on paper), so the "re-warming bump that re-decay repairs"
reading written above is falsified for this run: the shift is a genuine
change of what the model predicts on plain text (the 90 % fill tax and the
dolmino-only distribution), not a transient of the constant rate. The run
ended in the pre-fix trainer's tokenizer load after writing `final_fp32`
(weights + config; tokenizer copied in afterwards; no bf16 `final`, no
`run.json` — the history is the log). This endpoint is the §22b fallback
init for stage 1.

### 22b. The untried lever: sentinel embedding scale — pre-registration (2026-09-20 08:20Z)

**Observation (measured on the base weights).** Embeddings are untied. Trained
rows: norm median 10.82, p5 6.66, p95 14.18. The three sentinel rows: 0.897 /
0.879 / 0.884, random directions (cosine to the mean embedding 0.001 / 0.034 /
−0.012), identical to the 272 other never-trained ids; lm_head rows 1.784
against a median 1.813 (ordinary). A sentinel therefore enters the residual
stream at one twelfth of a normal token's amplitude, and Adam at 2e-5–3e-5
cannot grow it within a fine-tune (drift ≤ lr × steps per coordinate; observed
0.003 after 877 and 1,500 steps).

**Hypothesis.** The PSM ceiling is a signal problem, not a capacity problem:
resuming the prefix after a long suffix is a switch keyed on `<|fim_middle|>`,
and the layers cannot key a reliable switch on a near-silent token. SPM needs
no switch (the middle follows the adjacent prefix), which is why SPM reads
0.50–0.55 from the first check-in and PSM stays at 0.15–0.23 regardless of
tokens, rate or init. Bavarian et al. (2022) found FIM "for free" from 50 M to
6.9 B parameters WHEN TRAINED FROM SCRATCH, where the sentinel rows grow with
everything else — so this is a fine-tuning-from-converged limitation, not an
architecture or scale one, and the fix is to make the tokens audible.

**Intervention (`scale_sentinels.py`).** Rescale the three input rows to the
trained-row median norm (10.82), keeping their directions (mutually near-
orthogonal and orthogonal to every trained token, as random directions in
2,048 dimensions are). No new information; lm_head untouched; written to
`~/models/OLMo-2-0425-1B-sentinels`, the base left byte-identical.

**Design: a matched A/B at 16 M tokens.** Same pack (`packed_L`), same seed
(block order), same rate and schedule as §22a up to step 500 (`--epochs 0.0575
--warmup-frac 0.87` reproduces the 435-step warmup and the constant 3e-5 to
step 500), same `--accum 8`; the only change is the init. Readout: the 402
items on checkpoint-500, CPU fp32, PSM and SPM — the same instrument and
dtype as the §22a step-500 check-in (PSM 0.157 / 0.246 / 0.022, SPM 0.545 /
0.590 / 0.187 for copy_suffix / copy_prefix / free); val-fim and val-plain at
step 400 against §22a's 2.239 / 2.215. Cost ≈ 2.3 h training + 10 min probe.

| # | prediction if the hypothesis holds | falsified if |
|---|---|---|
| B1 | PSM `copy_suffix` at step 500 ≥ 0.30 (§22a: 0.157; +0.10 ≈ 3 s.e. is the adoption bar) | < 0.26 |
| B2 | SPM `copy_suffix` within ±0.05 of 0.545 (SPM never needed the switch) | a large SPM change would mean the mechanism is not the switch |
| B3 | val-fim at step 400 < 2.239; val-plain within ±0.01 of 2.215 (the fill tax and re-warming bump are unchanged by the init) | val-fim ≥ 2.239 |
| B4 | the scaled rows stay near 10.8 (they still cannot move) | — |

**Decision rule.** B1 met (≥ 0.26) → adopt the scaled init: rebuild nothing,
run stage 0 on `packed_S` at 3e-5 with the §22a check-in bars (raised: PSM ≥
0.35 at 1,000, 0.45 at 2,000, 0.55 at the S endpoint ≈ 4,650 steps) and the
same stop rule, then anneal, then stage 1. B1 not met → the ceiling is not the
embedding signal; anneal checkpoint-1500 (linear to zero over ~350 steps) and
proceed to stage 1 with the §22a verdicts standing. Either outcome is a
result: the A/B decides whether "fine-tune-scale FIM acquisition" is bounded
by the token's amplitude or by something the pass cannot reach.

**Not tuned post hoc.** The bar, the matched design and the two branches are
fixed here before the run.

**§22b result (run 2026-09-20 17:38–20:04Z, `v3_stage0_ab`, 500 steps, 16.4 M
tokens; probe on checkpoint-500, CPU fp32, 402 items).**

| | §22a step 500 (base init) | A/B (scaled sentinels) | verdict |
|---|---|---|---|
| PSM copy_suffix / copy_prefix / free | 0.157 / 0.246 / 0.022 | 0.179 / 0.284 / 0.030 | **B1 ✗** (bar 0.26; +0.022 on s.e. 0.035) |
| SPM copy_suffix / copy_prefix / free | 0.545 / 0.590 / 0.187 | 0.522 / 0.552 / 0.179 | B2 ✓ (within ±0.05) |
| step 0: fim / plain / paper | 2.310 / 2.195 / 1.626 | 2.322 / 2.195 / 1.626 | louder unknown tokens cost 0.012 at step 0 |
| step 400: fim / plain / paper | 2.239 / 2.215 / 1.664 | 2.239 / 2.214 / 1.682 | B3 ✗ (fim equal, not lower; plain within ±0.01) |
| step 500 (A/B end): fim / plain / paper | — | 2.241 / 2.223 / 1.695 | |
| sentinel row norms after training | 0.88 (unmoved) | 10.82 (unmoved) | B4 ✓ |

The surgery took (the rows are exactly as loud as trained tokens and, as
predicted, still cannot move) and made no difference to either order. The
hypothesis that the PSM ceiling is a signal-amplitude problem is FALSIFIED.
What remains standing: the ceiling is reached by ~16 M tokens, is the same
from two inits at two rates with silent or loud sentinels, and sits at PSM
≈ 0.18 / SPM ≈ 0.53 on these items. The adjacent-side reading (the model
continues whatever precedes `<|fim_middle|>` and pulls from the far block
about as well as an ordinary LM copies from earlier context) is the
description that survives; a fine-tune of this size does not install the PSM
switch, and the reason is not the token. **Branch taken per the rule:**
stage 1 inits from the anneal endpoint `v3_stage0_anneal/final_fp32`
(one-sided conditioning, format learned, plain +2.0 % / paper +5.2 % drift
carried in), with the caveat recorded in §22a: if stage 2's identity fill
does not move, the SPM-order results are the interpretable half.

### 22d. Exposure census for the rebase question (2026-09-21; side analysis, no training)

Question from the operator: how do the candidate bases differ in prior
exposure to (a) chemistry / mineralogy text and (b) fill-in-the-middle
material? FIM exposure is a recipe quantity (rate × tokens, from the papers
and mix cards); topic exposure was measured with ONE fixed classifier — a
mineralogy / vibrational-spectroscopy lexicon, a document counting at ≥ k
distinct terms within its first 20,000 characters — over 20,000-document
samples of each source of OLMo-2's pretraining mix (`allenai/olmo-mix-1124`;
local dolmino shards for pes2o / wiki / dclm, one Hub shard each for arXiv
and three StarCoder languages). Scratchpad `topic_census.py`; results
`~/tmp/analysis/v3/topic_census.json`.

| source (OLMo-2 pretraining share) | ≥ 2 terms (docs) | ≥ 4 terms (docs) |
|---|---|---|
| pes2o (1.5 %) | 17.5 % | 6.3 % |
| dclm web (95 %) | 2.1 % (4.0 % of chars) | 0.4 % |
| wiki (0.1 %) | 0.4 % | 0.1 % |
| arXiv (0.5 %; LaTeX source, one 38 MB shard — under-read) | 0.4 % | 0.0 % |
| StarCoder code (2 %): Python / C# / JavaScript | 0.2 / 0.1 / 0.0 % | 0.0 % |

Estimates (order of magnitude): OLMo-2-1B saw ≈ 25–30 B tokens of domain-heavy
text (≥ 4 terms; ~0.7 % of ~3.9 T, three quarters of it inside DCLM web) and
≈ 150 B at mention level, plus a few ×10⁸ in the dolmino midtraining's pes2o;
the v6 stage-1 pile is 138 M tokens/epoch, ≈ 1 % of that. A code corpus at
the measured rates gives StarCoder2-3B (The Stack v2, ~3.3 T tokens, 17
languages, no Wikipedia/arXiv for the 3B) at most ~10⁸ domain-heavy tokens —
two to three orders of magnitude less latent chemistry. FIM: OLMo-2 0;
this pass 49 M (primer) + 3.6 M (stage-1 replay share); OLMo-3-7B ≈ 5.2 B
(StackEdu 10.0 B tokens = 10 % of its 100 B midtraining mix, FIM on ~52 % of
documents; OLMo-3's mix card also lists 5 % olmOCR science PDFs and 5 %
STEM crawl); StarCoder2-3B ≈ 1.65 T (rate 0.5 over pretraining). A rebase to
StarCoder2 therefore trades ~300× less prior chemistry for ~30,000× more FIM
than the primer delivered, and puts the whole burden of the domain on the
pile. Calibration probes of both foreign bases on the 402 prose items are
staged for the stage-1 gap (`probes_calibration.sh`).

### 22c. Stage 1 launched from the anneal endpoint (2026-09-20 20:30Z)

`train_full.py --stage 1 --init v3_stage0_anneal/final_fp32 --epochs 2 --lr 4e-5
--accum 16` on `v6/stage1`: 33,804 blocks, 4,224 steps, warmup 211, ten val
sets. Step-0 losses (the S1 denominators) beside the base model's on the same
val sets:

| val set | base | stage-1 step 0 (primer) | primer cost |
|---|---|---|---|
| paper_markdown | 1.602 | 1.708 | +6.6 % |
| supplement_markdown | 1.603 | 1.736 | +8.3 % |
| binder_markdown | 1.597 | 1.657 | +3.8 % |
| reference | 1.692 | 2.016 | **+19.1 %** |
| replay | 2.120 | 2.177 | +2.7 % (bound for the run: 2.242) |
| hom | 2.502 | 2.634 | +5.3 % |
| webmineral | 2.315 | 2.430 | +5.0 % |
| mindat_prose | 2.667 | 2.935 | +10.1 % |
| pack_prose | 2.373 | 2.516 | +6.0 % |
| fim (borrowed) | 2.309 | 2.248 | −2.6 % |

The primer's tax is largest on the templated reference frames (+19 %) and
the short encyclopaedic registers, smallest on long prose; the one gain is
the sentinel format itself. S1-1 (paper ≤ 0.90 × step 0 = 1.537) and S1-2
(replay ≤ 1.03 × step 0 = 2.242) are judged against this table as
pre-registered; the base-relative recovery is reported beside them so the
primer's cost can be seen to be repaid or not.

**Halving rule FIRED at step 2,199 / epoch 1.041 (2026-09-21 17:45Z).**
val-replay 2.2590 > the bound 2.2423 (+3.8 % of step 0). The series rose
monotonically and slowly throughout the first epoch (2.177 → 2.205 at step
500 → 2.216 at 1,000 → 2.224 at 1,500 → 2.234 at 2,000, then the 2.259 jump
at the epoch boundary as the second pass began). Pre-registered response
executed without deviation: run stopped, resumed from `checkpoint-2000` with
`--lr 2e-5 --override-lr` (the callback logged `[LR OVERRIDE] resumed at step
2000; base_lrs -> 2e-05` and the resumed run's step-0 replay reads 2.234,
i.e. the 199 steps after the checkpoint were discarded). Logs:
`train_v3_stage1_20260920_142944.log` (4e-5, steps 0–2,199) and
`train_v3_stage1_20260921_114740.log` (2e-5, from step 2,000). A second
crossing is recorded, not acted on. **Second crossing recorded at step 3,098
/ epoch 1.467 (2026-09-22 08:4xZ): replay 2.2430 against the bound 2.2423,
over by 0.0007 — at the logged resolution (three decimals) this is AT the
bound, not past it, and the rate stays 2e-5 to the end as the rule
requires.** After the halving replay went 2.234 → 2.236 → 2.241 → 2.243 over
epochs 1.04–1.47, i.e. the climb continued at roughly a quarter of its
pre-halving slope; paper held flat at 1.326–1.329 across the same span, so
the second epoch is buying little on the papers and slowly costing replay.
Note for comparison: §19's identical rule fired at epoch 0.28; this run held
to epoch 1.04, which is the one visible benefit of starting from a primer
already shifted toward the replay distribution.

**Stage-1 result (ended 2026-09-22 15:21Z).** 4,224 steps, 276,807,680
tokens, 21.5 h wall; endpoints `v3_stage1/final` (bf16) and `final_fp32`.

| val set | step 0 | final | vs step 0 | vs base | v2 (§19) vs its base |
|---|---|---|---|---|---|
| reference | 2.016 | 0.3472 | −82.8 % | −79.5 % | −76.5 % |
| webmineral | 2.430 | 0.4072 | −83.2 % | −82.4 % | −82.2 % |
| hom | 2.634 | 1.1064 | −58.0 % | −55.8 % | −54.4 % |
| pack_prose | 2.516 | 1.5603 | −38.0 % | −34.3 % | −36.5 % |
| mindat_prose | 2.935 | 1.8600 | −36.6 % | −30.3 % | −31.0 % |
| supplement_markdown | 1.736 | 1.1979 | −31.0 % | −25.3 % | (new source) |
| paper_markdown | 1.708 | 1.3180 | −22.8 % | −17.7 % | −18.9 % (final 1.3181) |
| binder_markdown | 1.657 | 1.4412 | −13.0 % | −9.7 % | **+36.4 %** |
| replay | 2.177 | 2.2518 | **+3.4 %** | +6.3 % | +4.4 % |
| fim | 2.248 | 2.3610 | +5.0 % | +2.2 % | (none) |

**Verdicts.** S1-1 ✓ (paper 1.318 ≤ the 1.537 bar; and identical to v2's
1.3181 to four figures, so the primer's +6.6 % paper tax was fully repaid).
S1-2 ✗ (replay +3.4 % against the +3 % bound; the halving slowed the climb
from ~0.010 to ~0.003 per 500 steps but did not stop it — the same verdict
v2 recorded at +4.4 %). S1-3 ✓ decisively (binder −13.0 % against v2's
+36.4 % at four copies: the ≤ 2-copy decision removed the memorisation §19
diagnosed). S1-4 and S1-5 are probe results, reported with the gap bundle.

**What the second epoch bought (for the v4 recipe).** Comparing e0.95 with
the endpoint, counting exposures as repeats × epochs: reference (×1, 116k
distinct frame-documents) −14.8 % over the first half of epoch 2 and −29 %
across the whole of it, still falling at 2 exposures; webmineral −6 %;
papers, packs, supplements and hom each 1.6–2.3 %; mindat flat; binder
turned UP. v2's minima say the same in its own units: binder turned at 2.6
exposures, hom and mindat at 2.9, webmineral at 5.2, while papers and
reference were still falling at the end. **The turn point is a property of a
source's diversity, not of the epoch count**, so the efficient recipe is one
pass with per-source repeats set near each source's turn point and the extra
tokens spent on NEW documents (more papers; `N_REFERENCE_FRAMES` 6 instead
of 4) rather than on a second reading — the operator's "1.5× tokens, one
fewer epoch", qualified by where the extra 0.5× comes from.

At the crossing, paper stood at 1.350 (−21.0 % of step 0,
−15.7 % of base) and fim at 2.362 (+5.1 %).

**Freeze (2026-09-19 21:50Z; sha256 first 16).** `fim_recovery_items.json`
7581e2756b312012; `fim_manifest.json` 3b302930d9b401a3; `packed_L/manifest.json`
d7f08662e7c86904; `packed_S/manifest.json` 28a29b23cf8a6fdf;
`stage1/docs_manifest.json` bedd6a8d7ac6ac16; `stage1/manifest.json`
a6c6850f64ea95bd; `stage2/docs_manifest.json` c5782b8a95b33ca9;
`probe_species.json` f869a580539a0305. The probe set is re-ranked by the
standing rule (the hundred least-exposed candidates over the rendered pile,
§19): against the v6 pile 43 of §19–§21's 100 species were replaced (they are
now ordinary trained species), so U in this section is THIS file's list, not
§21's; every within-pass comparison (base, s0, s1, s2) uses the same list.
`stage2/manifest.json` b1d80755f0ce7b51. Logistics, not design: stage
0 evaluates every 400 steps and writes 120 val documents per subset because
an evaluation over all 2,543 val documents would cost minutes per pass.

### 22f. Controlled chained recall and the provenance-phrase ablation (2026-09-22 23:15Z)

Operator request: the §22 probes mix cues, so re-test showing each model ONLY named
fields, one target per turn, gold answers of earlier turns carried forward (teacher
forcing). `probe_chains.py`; report `~/tmp/analysis/v3/chain_probe/chain_report.md`
(every prompt, response, HIT/NEAR/MISS). Three minerals: Gypsum (826 paper mentions),
Celestine (7), Uranophane (53 mentions in 2 papers; a v3 validation species, so never in
v3's XML stage 2 — v2's stage 2 trained every species, so no mineral is stage-1-only for
v2). Eight chains, 18 steps per mineral, prose Q/A and XML (PSM, SPM) for both models;
v2 also re-asked with its raw IMA spelling wherever a formula is a cue.

Best-format HITs over the 54 steps: v3 25 (XML·SPM), v2 9 (prose; its XML answers are
tag soup — it never saw the schema). By direction, three minerals each:

- **name → formula:** both models 2/3 (Gypsum, Celestine); neither knows Uranophane
  from its name alone — both answer a Ca-uranyl PHOSPHATE (the autunite family).
- **name + formula → top band / first LIBS line:** v3 5/6 and 6/6, v2 0/6 and 0/6. v3's
  line answers are element knowledge (the line list is a function of the formula), and
  it recalls Uranophane's lines despite never seeing its record.
- **spectra alone → name or formula (chains 3–8, step 1):** 0 for both models on every
  mineral, every format. Answers are fluent and wrong (Quartz, Molybdite, Arsenolite).
- **formula → name:** Gypsum only. Celestine's SrSO4 gives Hexahydrite / Carnotite (v3)
  and an echo of the formula (v2); v2 names Strontianite once given bands or lines.
- **spectra as context help the forward direction:** Uranophane's formula, wrong from the
  name alone, is right in both models once bands or LIBS lines accompany the name.

**Ablation (`probe_sample_id.py`, the 200 seen species).** Band recall through the v4
probe frame with its parts removed: with the RRUFF sample ID / without the ID / without
the phrase "peak-picked from the RRUFF spectrum": v2 0.565 / 0.560 / 0.420; v3 stage 1
0.655 / 0.685 / 0.325; v3 stage 2 0.645 / 0.680 / 0.340. The sample ID carries nothing;
the provenance phrase carries a quarter of v2's band recall and half of v3's. Band lists
were always learned beside that phrase, so recall is conditioned on the learning context
rather than on the species name — which is also why a plain question ("What is its
strongest Raman band?") reads so low.

**Key ambiguity ceilings (2026-09-22, all 1,785 XML records; a key matches another
species when every value is within tolerance, bands ±10 cm-1, lines ±0.2 nm).** Share
of species uniquely identified / oracle ceiling (mean 1/candidates): top band alone
0.6 % / 0.033 (median 42 candidates); four strongest bands 90.0 % / 0.946; four LIBS
lines 31.9 % / 0.495 (Uranophane shares its lines with 15 Ca–Si–O–H species); bands +
lines 99.1 % / 0.996; formula 92.2 % / 0.958 (polymorphs); formula + bands 99.7 % /
0.998. The identity result (0.014 against a 0.946 ceiling) is therefore a learning
failure, not an ill-posed question: four bands identify a species almost as well as a
formula, but only jointly — each band alone leaves ~42 candidates — so bands→name is a
four-way conjunctive lookup over individually ambiguous numbers, while formula→name is
a single-key lookup. Lines→name is capped near 0.5 by chemistry and must be scored
against that ceiling.

### 22g. Granular stage 2: train the spectra → identity pairs directly — pre-registration (2026-09-22 19:15Z)

**Question.** §22's identity blank (species + formula from a FULL record with crystal
system and laser visible) reached 0.014 against a 0.946 ceiling after 16 exposures per
species, the same count at which the forward blanks reached ~0.92. §22f showed half the
chained probe's backward cells asked for mappings never trained, and that four bands
identify a species only jointly (one band leaves ~42 candidates). Does training each
spectra → identity pair directly, in stripped records with only the cue fields, and a
band-count progression (1, 2, 3, 4 bands → name) that makes the narrowing itself a
target, lift any of them off zero?

**Design (one variable: the blank policy).** Init `v3_stage1/final_fp32`, `--stage 2`,
lr 2e-5 linear, accum 8, one epoch, eval every 40 — identical to §22 stage 2.
`corpus_xml_granular.py` → `v6/stage2g`: every §22 row kept byte-identical (formula,
raman, top 16 / libs, system 8 / species-with-formula 16 per species; plain bundles)
EXCEPT the 26,656 identity rows (2.59 M tokens), replaced by stripped records
(`corpus_xml.stripped_record`: only the cues, one blank, no system, no laser) for 10
TRAIN_PAIRS at 8 rows per pair per species (both FIM orders; both block orders where both
blocks exist; identical rows repeated, spread by the global shuffle): formula → name;
bands → name; bands1/2/3 → name; lines → name; bands + lines → name; bands → formula;
lines → formula; bands + lines → formula. 133,280 rows = 7.45 M tokens. The other 9
probe pairs (forward, and spectra-as-context) are NOT trained — transfer for both arms.
SIZING DEVIATION from the design stated in chat, recorded before training: at the
identity rows' own budget the pairs would get ~1 exposure per species each, a test that
cannot tell "does not learn" from "never shown"; 8 per pair is the count at which §22's
libs and system blanks reached 1.00 / 0.92. The stage therefore grows from 26.8 M to
33.4 M tokens (8,434 blocks, 1,054 steps), mix held at 65 / 20 / 7 / 8 (realised xml_fim
62.2 %, xml_plain 2.8 %, paper 20.0 %, other 7.0 %, replay 8.0 %). Control = the §22
endpoint `v3_stage2/final` (same init, same forward rows, identity blank instead).

**Instrument.** `probe_pairs.py`: the 19 PAIRS × 100 species — T 60 seeded trained, V 19
(the §22 val species; never in either arm's XML training), U 21 seeded untouched —
prose, trained-order XML (PSM, SPM averaged) and held-out formula-first XML; every rate
beside its ceiling (1 / distinct answers among all 1,785 records matching the cues).
Items frozen in `items_frozen.json`. Also `probe_xml_fill.py`, `probe_recall.py`
(seen 200 + probe set), `probe_backward_identity.py`, prose bounds as P5.

**Measured before (control, 2026-09-22 19:10Z; group T, trained-order XML unless noted).**
formula → name 0.27 (held-out 0.20, prose 0.07); bands + formula → name 0.61; lines +
formula → name 0.57; bands + lines + formula → name 0.57; name → formula 0.72 (prose
0.87); name + formula → top 0.97 (prose 0.07); name + formula + top → line 0.47; the
three spectra + name → formula pairs 0.97 / 0.97 / 0.97; EVERY spectra-only pair 0.00
(bands, lines, bands + lines, bands1/2/3 → name; bands, lines, bands + lines →
formula). Ceilings (T): bands → name 0.95, lines → name 0.49, lines → formula 0.51,
bands1 / 2 / 3 → name 0.03 / 0.38 / 0.82, all other pairs ≥ 0.97. V and U: formula →
name 0.00, spectra-only pairs 0.00. v2 reads 0.00 on every XML cell (never saw the
schema) and 0.00–0.03 on every backward prose cell.

**Predictions (granular arm v3g vs control v3, same items).**

| # | prediction | falsified if |
|---|---|---|
| G1 | forward protected: `probe_xml_fill` held-out-order formula / raman / top on trained species within 5 points of control (0.884 / 0.918 / 0.898); `probe_recall` probe-frame formula / bands within 5 points (0.845 / 0.665 as scored) | any drops > 5 points |
| G2 | prose bounds as P5: paper ≤ +2 %, replay ≤ +3 % of the run's step 0 | either exceeded |
| G3 | formula → name (T, xml) ≥ control + 0.20 (≥ 0.47): single-key and well posed, now trained in isolation | < 0.47 |
| G4 | lines → name (T, xml) ≥ 0.25 (half its 0.49 ceiling): lines follow from elements the model already maps forward (libs fill 1.00) | < 0.25 |
| G5 | lines → formula (T, xml) ≥ 0.25 (ceiling 0.51) | < 0.25 |
| G6 | bands → name and bands → formula (T, xml) stay < 0.10 — the four-way conjunctive lookup is not expected to be learned at 8 exposures | ≥ 0.10 on either (a positive surprise) |
| G7 | progression: if narrowing is learned, T xml HIT rises with k (bands1 < bands2 < bands3 < bands); if not, all four sit within 0.03 of one another | — (read, not bar) |
| G8 | bands + lines → name / formula ≥ lines → name / formula (the bands add, never subtract) | either lower by > 0.05 |
| G9 | V and U: every trained backward pair ≤ 0.05 (no stage-2 exposure; stage-1 inverse frames only, position-ordered); forward name → formula within 5 points of control | backward > 0.05, or forward drop > 5 |
| G10 | held-out attribute order: formula → name held-out ≥ 0.5 × trained order (not a pure template) | < 0.5 × |
| G11 | no transfer to prose: v3g prose on the trained backward pairs ≤ control + 0.05 (P10's schema-binding) | > control + 0.05 on any |

Expected NOT to work: bands → name, bands → formula, bands1 → name (ceiling 0.03).

**Freeze (sha256, first 16).** `v6/stage2g/docs_manifest.json` 5388f2f2a33eeba3;
`v6/stage2g/manifest.json` 6e378f527a2958db; `pair_probe/items_frozen.json`
f5e7e07a35b2b61c. Not tuned post hoc; a change is a new subsection.

### 22g. Result: the backward lookup is learnable, and it is a string lookup (run 2026-09-23 01:10–07:20Z)

1,054 steps, 34,537,472 tokens, 6.2 h, endpoint `v3_stage2g/final`. Final vs step 0:
paper 1.3180 → 1.3402 (+1.7 %), replay 2.2520 → 2.2932 (+1.8 %), prose fim 2.361 →
2.431 (+3.0 %), reference 0.347 → 0.503 (control 0.493) — prose costs match the control.

**Pair probe, group T (60 trained species), trained-order XML, control v3 → granular v3g.**

| pair | ceiling (±10 / ±0.2) | control | granular |
|---|---|---|---|
| ★ formula → name | 0.97 | 0.27 | 0.39 |
| ★ bands → name | 0.95 | 0.00 | **0.47** |
| ★ bands + lines → name | 1.00 | 0.00 | **0.51** |
| ★ bands + lines → formula | 1.00 | 0.00 | **0.49** |
| ★ lines → formula | 0.51 | 0.00 | 0.33 |
| ★ lines → name | 0.49 | 0.00 | 0.08 |
| ★ bands → formula | 0.95 | 0.00 | 0.04 |
| ★ bands1 / bands2 / bands3 → name | 0.03 / 0.38 / 0.82 | 0.00 | 0.28 / 0.47 / 0.44 |
| bands + formula → name | 1.00 | 0.61 | 0.67 |
| lines + formula → name | 0.97 | 0.57 | 0.67 |
| name → formula | 1.00 | 0.72 | 0.75 |

V and U (never in either arm's XML): every trained backward pair ≤ 0.05 in both arms.
Prose: every spectral backward pair 0.00 in both arms; formula → name 0.07 → 0.12.

**The anomaly and its test (`probe_band_jitter.py`).** One band names a trained species
at 0.28, above the 0.03 ceiling that ±10 cm-1 matching allows — possible only if the
model keys on the exact integer. Every band shifted by ±δ cm-1 (random sign), same 60
species, granular arm:

| pair | δ = 0 | 1 | 2 | 3 | 5 | 10 |
|---|---|---|---|---|---|---|
| 1 band → name | 0.28 | 0.01 | 0.01 | 0.00 | 0.00 | 0.00 |
| 2 bands → name | 0.43 | 0.02 | 0.01 | 0.02 | 0.00 | 0.00 |
| 4 bands → name | 0.47 | 0.01 | 0.01 | 0.00 | 0.00 | 0.00 |
| bands + lines → name | 0.52 | 0.04 | 0.03 | 0.03 | 0.02 | 0.03 |

A 1 cm-1 shift — below RRUFF's own within-species scatter (median |Δ| 1.3) — erases it.
Exact-value ceilings (1 / species sharing the identical integers): 1 band 0.40, 2 and 4
bands 1.00; the model reaches 70 % of the one-band exact ceiling. The same model names
the species from the FULL record (bands + lines + system + laser) at only 0.077 lenient /
0.000 strict (`probe_xml_fill` identity; control 0.014 / 0.000): extra fields break the
memorised string.

**Scorecard.** G1 ✓ (held-out-order full-record fill formula 0.884 → 0.949, raman 0.918
→ 0.887, top 0.898 → 0.907; recall probe frame formula 0.845 → 0.845, bands 0.665 →
0.615, exactly at the 5-point edge). G2 ✓ (+1.7 % / +1.8 %). G3 ✗ (formula → name 0.39
< 0.47; +0.12). G4 ✗ (lines → name 0.08 < 0.25). G5 ✓ (lines → formula 0.33). G6 ✗ as
worded — bands → name 0.47 ≥ 0.10, the pre-registered positive surprise — and resolved
by the jitter test as exact-string recall, not identification; bands → formula 0.04
holds. G7 read: 0.28 / 0.47 / 0.44 / 0.47 — rises from one band to two, then flat; the
k = 1 and k = 2 values exceed their ±10 ceilings, i.e. no narrowing was learned, only
string identity. G8 ✓. G9 ✓ (backward ≤ 0.05 on V and U; name → formula within 5) —
but V's spectra + name → formula context pairs fell 12–16 points. G10 ✓ (0.24 / 0.39 =
0.62). G11 ✓ (formula → name prose +0.05, at the edge; spectral pairs 0.00).
Unpredicted: prose crystal-system probe frame 0.198 → 0.401 (probe set 0.260 → 0.400)
while the v4 question frame fell 0.279 → 0.208 — frame-dependent, unexplained.

**Standing conclusions.** (i) Direction is not the wall. Trained directly, a 1B model
learns spectra → name in 8 exposures to half its ceiling (0.47–0.51), from zero. (ii) What
it learns is the numbers as STRINGS: a 1 cm-1 shift takes 0.47 to 0.01. Numeric proximity
plays no part in the lookup, so identification of a measured spectrum — the scientific
task — is zero in both arms. (iii) The one remaining lever is training that makes
proximity matter: every backward exposure drawn with fresh instrument jitter
(`synth_variance.py` already models it), coarsened / binned values, or candidate lists in
context — the numeric-key mitigations decision 3 deferred, now the only untested path.
(iv) The skill stays schema-bound: prose 0.00 on every spectral pair.

### 22h. Resolution arm: jittered, grid-rounded bands with a resolution field — pre-registration (2026-09-23 16:00Z)

**Question.** §22g's backward lookup is an exact-string lookup (±1 cm-1 → 0.01). OLMo-2
writes every three-digit band as one token (493, 495, 490 are unrelated symbols), and a
real re-measurement of the same species shifts matched bands by median 1.0 / p90 7.0 cm-1
while keeping the same four-band set only 18 % of the time. Does training every
bands-bearing backward row on a FRESH instrument draw, rounded to that instrument's
resolution grid and labelled with a resolution field, make bands → name survive
measurement variation? Operator ruling (2026-09-23): this lifts decision 3's deferral of
numeric-key mitigations; re-ranking comes in the next arm; StarCoder2 is the alternative
if stage-2 iteration stops paying.

**Design.** Identical to §22g except the numeric presentation (`corpus_xml_resolution.py`
→ `v6/stage2r`): each row whose cues include bands draws an instrument
(`synth_variance.sample_raman`: lab / portable / handheld .50 / .35 / .15, Gaussian σ 1.2 /
3.0 / 3.5 cm-1, 25 % of non-lab units with a constant ±4–9 offset; no band loss, no
re-ranking), rounds the jittered positions to the class grid (1 / 5 / 10 cm-1) and writes
`<raman resolution_cm1="G">`. Exposures: 16 per species for bands → name and bands + lines
→ name, 8 for the other bands pairs (§22g had 8 for all — a second difference, recorded);
formula → name, lines → name, lines → formula repeated exactly as §22g; LIBS exact;
§22's forward and species rows byte-identical. 159,936 new rows = 9.87 M tokens; the
16 draws of bands → name give 15.35 distinct four-band tuples per species on average, so
whole-string memorisation is impossible by construction. Pack: 9,369 blocks = 37.08 M
tokens, 1,171 steps; mix xml_fim 62.5 / xml_plain 2.5 / paper 20.0 / other 7.0 / replay 8.0.
Init `v3_stage1/final_fp32`, `--stage 2`, lr 2e-5 linear, accum 8, one epoch.

**Instrument (`probe_resolution.py`).** bands → name and bands + lines → name on stripped
records, PSM + SPM, three field renderings (none / correct / wrong). T: the probe_pairs
seeded 60 with exact values and 4 FRESH draws per class (probe-only seed namespace). R: 100
seeded trained species with a held-out real RRUFF / ROD re-measurement
(`real_remeasurements.json`, 764 species: same strongest band 63 %, same four-band set
18 %), at native integers and rounded to 5 and 10, split by set-match. V: the val species.

**Measured before (§22g's v3g; best rendering for it is no field).** T exact 0.46
(field "1": 0.15 — the unseen attribute breaks its string); fresh lab / portable /
handheld 0.12 / 0.02 / 0.02; R real native 0.09, grid 5 0.05, grid 10 0.03; R set-match
native 0.31, grid 5 / 10 0.04; V 0.00. bands + lines → name: T exact 0.49, lab / portable /
handheld 0.15 / 0.07 / 0.05, R native 0.10.

**Predictions (v3r, bands → name, correct field unless stated).**

| # | prediction | falsified if |
|---|---|---|
| H1 | forward protected vs the §22 control: full-record held-out-order fill formula / raman / top within 5 points (0.884 / 0.918 / 0.898); recall probe frame formula / bands within 5 (0.845 / 0.665) | any drop > 5 |
| H2 | prose bounds: paper ≤ +2 %, replay ≤ +3 % of the run's step 0 | either exceeded |
| H3 | fresh synthetic draws (T): handheld ≥ 0.30 and portable ≥ 0.20 — coarse grids generalise by per-band coverage; lab (grid 1) expected < 0.20 (needs genuine proximity) | handheld < 0.30 or portable < 0.20 |
| H4 | exact canonical values, field "1": ≥ 0.20 | < 0.20 |
| H5 | the field is read: portable and handheld draws, correct field ≥ wrong field + 0.05 | < + 0.05 on both |
| H6 | real re-measurements (R): overall at grid 10 < 0.10 (the four-band set changes in 82 %); set-match subgroup ≥ 0.20 at grid 5 or 10 | set-match < 0.20 at both — then variation beyond re-ranking also defeats it |
| H7 | bands + lines → name ≥ bands → name − 0.05 in every condition | lower by > 0.05 |
| H8 | V (never in either arm's XML) ≤ 0.05 | > 0.05 |

Decision rule (operator): H3 + H6 set-match met → the re-rank arm next (train the
ranking variation that the 82 % of real re-measurements carry). H3 met, H6 not → variation
beyond ranking matters too; still re-rank next, with band loss. H3 failed → numeric
proximity is out of reach in this tokenisation; StarCoder2 (digit-level tokens) next.

**Freeze (sha256, first 16).** `stage2r/docs_manifest.json` 791fa244e6f91891; `stage2r/manifest.json`
92e8b8d59095ceb8; `real_remeasurements.json` 793fff5f671a2dfa; `resolution_items_frozen.json` 7f430318a213af2a.

### 22h. Result: jitter taught the model to ignore the bands, not to match them (run 2026-09-23 15:50–22:39Z)

1,171 steps, 38,371,328 tokens, endpoint `v3_stage2r/final`. Final vs step 0: paper +1.6 %,
replay +1.8 %, prose fim 2.361 → 2.430, reference 0.347 → 0.505.

**Resolution probe, bands → name (correct field; v3g in its native no-field rendering).**

| condition | v3g | v3r |
|---|---|---|
| T exact canonical | 0.46 | 0.05 |
| T fresh draws lab / portable / handheld | 0.12 / 0.02 / 0.02 | 0.05 / 0.04 / 0.03 |
| R real re-measurement, native / grid 5 / grid 10 | 0.09 / 0.05 / 0.03 | 0.01 / 0.03 / 0.01 |
| R set-match subgroup, grid 5 / 10 | 0.04 / 0.04 | 0.04 / 0.00 |

**bands + lines → name** (LIBS lines exact): v3r 0.39–0.44 in EVERY condition — exact,
every instrument class, every real re-measurement, and 0.36 on the "neither" subgroup
whose real four-band set shares nothing with the canonical one. `probe_band_jitter` on
v3r: bands → name 0.03 at every δ; bands + lines → name 0.43 at δ = 0 and 0.36 at δ = 10.
Pair probe (exact values): lines → name 0.08 (v3g) → 0.29 (v3r), lines → formula 0.33 →
0.41 (ceilings 0.49 / 0.51), bands → name 0.47 → 0.03. With every band draw different and
the lines always exact, the model moved its identification onto the lines — the one
invariant cue — and learned nothing from the bands, not even the exact values.

**Scorecard.** H1 ✗ (full-record held-out-order raman fill 0.918 → 0.718, system 0.887 →
0.804; recall probe-frame bands 0.665 → 0.595, −7; formula held). H2 ✓ (+1.6 % / +1.8 %).
H3 ✗ (handheld 0.03, portable 0.04 against bars of 0.30 / 0.20). H4 ✗ (exact 0.05). H5 ✗
(correct field ≈ wrong field everywhere: the field is not read). H6 ✗ (set-match 0.04 /
0.00). H7 ✓ (trivially: the lines carry it). H8 ✓ (V 0.00). Unpredicted, and continuing
the §22g trend: prose crystal-system probe frame 0.198 (control) → 0.401 (v3g) → 0.533
(v3r); bands + formula → name 0.61 → 0.72 and lines + formula → name 0.57 → 0.71.

**Reading.** Pure bands → name had no shortcut available (its rows show only bands) and
learned nothing in 16 fresh draws per species; where a shortcut existed, the model took
it. Two explanations remain and this run cannot separate them: the tokenization (every
three-digit band is one token, so 493 and 495 share nothing on the surface and proximity
must come from embedding geometry), or the budget (16 noisy examples per species for
1,666 species). Per the pre-registered rule (H3 failed), the next step is the
tokenization test. A cheaper discriminating version than the StarCoder2 rebase exists:
the same arm on OLMo-2 with band values rendered digit by digit (so 493 and 495 share
their leading tokens, as StarCoder2's tokenizer would make them) and the LIBS lines
jittered too, so no exact cue is left to route around.

### 22i. Digit-rendered bands and jittered lines: is the tokenizer the wall? — pre-registration (2026-09-23 23:25Z)

**Question.** §22h's bands-only rows — which offered no shortcut — learned nothing from 16
fresh draws per species (bands → name 0.03 everywhere). Two explanations: OLMo-2 writes
every three-digit band as ONE token (493, 495 unrelated on the surface), or the budget.
This arm tests the first at identical budget and model: the same draws, written digit by
digit so neighbouring values share their leading tokens (tokenizer check: `4 9 3` →
`4 Ġ 9 Ġ 3`, `4 9 5` → `4 Ġ 9 Ġ 5`), and the LIBS lines jittered in every row that shows
them, since §22h showed the model routes identification through any exact cue left.
Operator ruling (2026-09-23): run this cheaper discriminating test before any StarCoder2
rebase.

**Design.** `corpus_xml_resolution.py --digits --jitter-lines` → `v6/stage2d`: §22h row for
row (159,936 new rows, band draws identical — same seed namespace — so on the bands only
the rendering changes); band and line values rendered by `corpus_xml.digit_str`; every
row showing lines draws fresh positions (±U(0.02, 0.10) nm, random sign per line,
`synth_variance.LIBS_POS_JITTER`); resolution field and grids as §22h. Known second
difference: digit rendering makes the new rows 35 % longer (13.36 M vs 9.87 M tokens) at
the same row and exposure counts. Pack 10,747 blocks = 42.46 M tokens, 1,343 steps; mix
xml_fim 62.9 / xml_plain 2.2 / paper 20.0 / other 7.0 / replay 8.0. Init
`v3_stage1/final_fp32`, `--stage 2`, lr 2e-5 linear, accum 8, one epoch.

**Instrument.** `probe_resolution.py --digits --jitter-lines --tag digits`: §22h's groups,
conditions and field renderings, digit-rendered, with fresh probe-only line draws in every
non-exact condition. Also the native-rendered probe (transfer), `probe_pairs`,
`probe_xml_fill`, `probe_recall`.

**Measured before.** §22h's v3r on the digit probe: 0.00 in every cell (never saw the
rendering; jittered lines remove its shortcut). The contrast of record is v3r on its own
native probe: bands → name exact 0.05, fresh lab / portable / handheld 0.05 / 0.04 / 0.03,
real 0.01–0.03.

**Predictions (v3d, digit probe, correct field, bands → name unless stated).**

| # | prediction | falsified if |
|---|---|---|
| I1 | forward protected vs the §22 control: full-record held-out-order fill formula / raman / top within 5 points (0.884 / 0.918 / 0.898); recall probe frame formula / bands within 5 (0.845 / 0.665) | any drop > 5 |
| I2 | prose bounds: paper ≤ +2 %, replay ≤ +3 % of step 0 | either exceeded |
| I3 | THE TEST — fresh synthetic draws: portable or handheld ≥ 0.15 (v3r 0.04 / 0.03) | both < 0.10: the tokenizer is not the (only) wall |
| I4 | exact canonical values ≥ 0.15 | < 0.15 |
| I5 | real re-measurements: set-match subgroup ≥ 0.20 at grid 5 or 10; overall grid 10 < 0.15 (re-ranking still defeats it) | set-match < 0.20 at both |
| I6 | the field is read: portable and handheld, correct ≥ wrong + 0.05 | < + 0.05 on both |
| I7 | V ≤ 0.05 | > 0.05 |

Read, not barred: bands + lines → name on R:"neither" (real four-band set shares nothing
with the canonical) measures reliance on the lines — digit-rendered jittered lines keep
their integer prefix, so a coarse line key remains possible; ≤ 0.10 means the bands carry
the identification. v3d on the NATIVE probe measures transfer across renderings.

Decision rule: I3 met → the tokenizer was the wall; the re-rank arm next, in digit
rendering. 0.10 ≤ I3 < 0.15 → partial; re-rank next, with more exposures. I3 < 0.10 on
both → not the tokenizer (or not only); StarCoder2 (larger, digit-native) or a budget
increase.

**Freeze (sha256, first 16).** `stage2d/docs_manifest.json` c81288ebd3cdf47a; `stage2d/manifest.json`
e72d6f448fc6bfc9; `resolution_digits_items_frozen.json` 7f430318a213af2a.
