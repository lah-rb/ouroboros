# Key-registry compaction: 27 Opus agents, one mechanical gate

**Operator ruling (2026-09-04):** "have an opus agent look over the sets for
equivalents and compact the unique keys as far as possible. Then look for any
new keys that warrant promotion to the main set."

## Why the vocabulary had drifted

The registry is the shared vocabulary that makes packs comparable across
papers. It held **6,690 keys** plus **2,264 more held in the coinage
quarantine** (mostly from the Haiku repack boost, which coins ~2.5 new keys
for every one it reuses). Most were used once. Twelve aliases existed, all
from the 2026-08-22 reform, and none had ever been applied to data on disk.

## How the work was sliced

Keys were sorted by their **stemmed content-token signature** — units, digits
and plurals normalised away, tokens sorted — because every one of the twelve
historical aliases is an exact signature collision under that normalisation
(`xrf_mass_composition_percent` / `xrf_composition_percent_mass`). That put
variants of one quantity adjacent and yielded **328 collision groups over 763
keys**, handed to the agents as the highest-yield candidates. 27 jobs of ≤341
keys, no signature group split across jobs.

## The gate, which is the load-bearing part

Agent judgement is advisory; `dev/apply_key_compaction.py` re-derives every
merge mechanically and refuses eight classes: missing key, self-merge, alias
chain or cycle, registry type mismatch, unit mismatch, digit-parameterised
key (the digit carries a condition or a second instance), min/max/avg
mismatch, and aliasing a registry key onto a quarantined one.

Three false-positive classes were found and fixed by running it against real
proposals, each one a case where the rule refused a provably identical pair:

| bug | refused wrongly | fix |
|---|---|---|
| "range" read as an aggregate | `xrd_measurement_range_2theta_max_deg` → `xrd_2theta_max_deg` | filler word, dropped from the aggregate set; avg ≡ average |
| any letters+digits read as parameterised | `tio2_concentrations_mg_per_l`, `calibration_curve_r2` | chemical formulas and statistic names carry digits; only `_<n>` suffixes and `ph<n>` are parameterised now |
| "ms" in `gc_ms` read as milliseconds; grating g/mm not a unit | `gcms_column` → `gc_ms_column`, `..._g_per_mm` → `..._grooves_per_mm` | technique abbreviations normalised; g/l/lines/grooves are one groove density only beside `mm` |

Four same-unit spellings were then whitelisted on the agents' exemplar
evidence: cm3 ≡ mL, mass% ≡ weight% ≡ wt%, grating g/mm ≡ lines/mm ≡
grooves/mm, min⁻¹ ≡ rpm (stirring), plus microseconds ≡ us, degrees ≡ deg.
**cm and cm-1 were NOT whitelisted** — a length is not a wavenumber — and
that ruling cost three agents work they had already done under an earlier,
looser wording of the policy.

## Result

| | before | after |
|---|---|---|
| registry keys | 6,690 | 8,307 |
| quarantined keys | 2,264 | 114 |
| aliases | 12 | 259 |
| dataset files rewritten | — | 161 |

285 merges proposed, **247 accepted, 38 refused** (30 unit, 6 type, 2 digit).
**1,726 quarantined keys promoted, 303 dropped as furniture** (author names,
contract numbers, campaign dates, bibliometrics, section titles). The registry
GROWS because the quarantine drains into it; the *duplicate* spellings fell by
247. Keys used by two or more papers: 645, of which 77 by ten or more.

Biggest single win: the Raman peak family — `raman_peaks_cm-1`,
`raman_peak_cm-1`, `raman_peak_positions_cm-1` and a quarantined fourth
spelling — folded into one key with 198 uses.

## Verification

The 33 key collisions the merge produced were checked value by value: in every
sampled case the alias and canonical held the SAME value
(`crater_age_gy` = `crater_age_ga` = 4), so the leaf-count drop is
deduplication, not loss. Two PRE-EXISTING defects surfaced and were fixed in
the same pass: 26 keys sat in both the registry and the quarantine, and six
dataset files still carried spellings from the never-retro-applied 2026-08-22
alias map. Final state: zero aliased spellings left in data, zero keys in both
files, zero alias chains, every canonical present in the registry.

## What the agents would not decide

278 operator notes, kept at
`~/corpora/ouroboros-spectra/key_compaction_notes_2026-09-04.json`:

- **12 malformed keys or typos** — `bet_surface_area_g_m2` has its unit
  reversed (it is m²/g), `icp_oess_*` carries an OES typo,
  `deplorization_factor_formula`, `concentration_concentration_steps_pct`,
  and one key containing a literal space. These need a RENAME, not an alias,
  so they were left where they were.
- **8 suspect values** — `instrument_watsom_field_of_view_deg` has min > max
  on two axes and `fib_stem_resolution_nm_per_pixel` has min 26 against max
  2.2; the suffixes look swapped in the source packs.
- **14 merges blocked only by the type rule** — the same quantity typed
  `number` in one key and `list[object]` in another
  (`uv_vis_absorption_peak_nm` / `_peaks_nm`). Resolving those needs a type
  decision first.

## Follow-up: shallow lists, renames, suffix swaps (same day)

Operator: "The clean repair is probably to merge the strings into a shallow
list. Additionally, have agents rename the malformed keys, and swap the
suffixes after a sanity check."

### Why the type split kept regenerating

`_types_compatible` already lets a paper send `list[T]` where the registry
says `T`, so those packs PASS -- but `update_key_registry` only bumped the
count and left the entry saying `T`. The registry therefore stopped
describing its own data, the prompt kept showing a scalar, and models coined
the plural spelling as a separate key to escape. That is the mechanism that
manufactured the 19 duplicate pairs.

Two code fixes close it:

- `update_key_registry` **widens** `T` to `list[T]` when a paper honestly
  sends several values, and stamps `widened_at`. Real drift (number vs
  string) still never widens, and a deeper container never replaces a
  shallow one.
- `repair_shapes` wraps a bare value into a **shallow** list
  ("Renishaw inVia" -> ["Renishaw inVia"]), where before it only boxed values
  into `list[object]`. A number offered under `list[string]` is drift, not a
  shape problem, and is still left for the gate to fail.

### The migration

All 19 pairs were checked first: in **every one** the plural side really
holds multi-element values (['CaCO3','SiO4','FeTiO3','MgCO3'], [532, 638,
785]), so there was no cheap collapse -- widening was the only clean repair.
`dev/widen_scalar_keys.py` wrapped 3,079 scalars across 1,698 packs, folded
41 values from the plural twins, and asserted the corpus leaf count as an
invariant: 227,312 -> 227,308, the -4 being duplicate values merged where a
paper held both spellings.

### Renames and swaps

`dev/apply_key_renames.py` -- 100 candidates from a mechanical sweep plus the
review notes; **69 renamed, 30 deliberately left**. The agent kept every
doubled token that is real notation (`ar_ar` argon dating, `pr_pr`
interatomic distance, `w_w` weight-for-weight, `chi_chi` earthquake) and
renamed only true errors: 58 keys spelling the SHERLOC camera WATSON as
"watsom", `deplorization`->`depolarization`, four `icp_oess`->`icp_oes`,
`them is_band_center_um`->`themis_band_center_um` (a stray space),
`tme_observations`->`tem_observations`, and `bet_surface_area_g_m2` whose
unit read backwards.

`dev/apply_suffix_swaps.py` -- a corpus-wide sweep found 6 packs where a
`_min` value exceeds its `_max`. The reviewer read the papers: **4 swaps, 2
keeps**. The swaps are WATSON, where the packer keyed `_min` to the LABEL
"min. working distance" rather than to the lower number. The keeps are the
resolution trap: for nm/pixel a smaller number is finer, and both papers
order coarse-to-fine ("from 26 nm/pixel up to 2.2 nm/pixel"), so `_min`
holding the coarser value is defensible as written.

Order was load-bearing: the swaps reference the misspelled `watsom_*` keys,
so they had to run BEFORE the rename.

### Residue found and fixed

Stacking three migrations produced one self-alias (`icp_oes_instrument` ->
itself, because the typo had held the canonical slot and the rename
retargeted the alias onto its own key) and one two-step chain. Both are
collapsed, and the rename applier now drops self-aliases and flattens chains
itself. Final: 8,288 registry keys, 114 quarantined, 346 aliases, zero
self-aliases, zero chains, zero aliased spellings left in data, zero
malformed spellings left in the vocabulary.
