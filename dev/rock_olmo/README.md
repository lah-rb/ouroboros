# rock_olmo — training-form serializer for the spectral corpus

A **project result**, not part of Ouroboros. Ouroboros *produced* the
corpus; this reads it, joins it to the mineral reference layer, and
emits training records for the OLMo 2 1B passes. It lives under `dev/`
because nothing in the agent runtime imports it and its lifetime is the
project's, not the framework's.

## Inputs

| input | what it supplies |
|---|---|
| `~/corpora/ouroboros-spectra/databank/dataset/*.json` | curator-accepted papers, packed and grounded |
| `~/corpora/mineral-refs/rruff/` | Raman/XRD spectra + chemistry, cell, locality (2,135 species) |
| `~/corpora/mineral-refs/ecostress/` | thermal-IR and VSWIR reflectance (159 mineral species) |
| `~/corpora/mineral-refs/mindat/minerals_ima.jsonl` | IMA names, formulas, symbols (6,239 species) |
| `~/corpora/mineral-refs/nist_asd/` | atomic emission lines, 92 elements |
| `~/corpora/mineral-refs/sshade/` | molecular-ice band lists (separate domain) |

## Modules

- **`training_form.py`** — canonical field schema. Units normalised,
  `_min`/`_max` folded into range objects, `*_as_packed` retaining the
  verbatim value. Runs at serialisation, never on the packs: rewriting
  a value in an artifact would break the grounding invariant that
  `grounding_check` enforces.
- **`holdout.py`** — held-out species, stratified on recurrence AND
  compositional complexity (15 simple / 15 complex over 5 bands).
  Quartz protected. Held out by SPECIES because holding out papers
  leaks through the reference side.
- **`reference_layer.py`** — readers for RRUFF / ECOSTRESS / NIST ASD.
  Keeps FACT (catalogued) apart from DERIVED (our peak-pick of a RRUFF
  spectrum), because RRUFF publishes spectra, not peak lists.
- **`interconnect.py`** — the five views: forward, inverse, cross-modal,
  contrastive, corroboration. Oversampling is delivered as distinct
  framings, not repeated copies.

## Running the tests

They are NOT in the root `pytest tests/` run — this is a project folder.

```bash
cd dev/rock_olmo && ../../.venv/bin/python -m pytest tests/ -q
```

`conftest.py` puts this directory on `sys.path` so the modules import by
bare name.

## Design decisions worth not relitigating

- **Species, not papers, are the holdout unit.** The same fact appears
  in a paper, a RRUFF spectrum, an ECOSTRESS signature, a mindat
  formula and NIST lines derived from that formula. Only removing a
  species closes every view.
- **The reference grounds; it does not adjudicate.** A paper reporting
  371 against a reference 375.2 is not wrong — the gap may be precision
  or accuracy. Views say so and name both, and there are tests
  asserting the reference is never called authoritative.
- **Provenance is never smoothed.** A peak-picked position says
  "peak-picked from the RRUFF spectrum"; catalogued data says
  "catalogued by". A reader can always separate the two.
- **The long tail is not collapsed.** 93% of pack keys are used once and
  carry the domain's real specificity; they serialize as prose rather
  than being forced into a schema.
