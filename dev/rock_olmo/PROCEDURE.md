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

## 9. Emitter — NEXT

Not built. Turns these records plus ~17M tokens of paper text into the
training file:

- 4x weighting for the priority datasets (RRUFF, ECOSTRESS, SSHADE,
  NIST ASD) delivered as views, not copies
- holdout **physically separated**, verified absent from every view
- SSHADE ices on their own join key (`species_inchikey`), a separate
  domain from the minerals
- shuffle and shard

**Open question worth answering before training rather than after:**
162k interconnect tokens against ~17M of paper text is 0.8% of the
corpus. Even at 4x it is under 4%. If the interconnects are meant to
shape reasoning rather than add facts, that ratio may be too thin — the
lever is more views per species (per-sample records, more techniques,
more sibling groups), not more copies of these.
