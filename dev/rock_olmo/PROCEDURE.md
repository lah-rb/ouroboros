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

## 11. Run 1 result — FORMAT ACQUISITION, NOT SPECTROSCOPY

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
