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
| `~/corpora/mineral-refs/mindat/geomaterials.jsonl` | crystal system, cell, Strunz class, density, hardness (94.8% of our species) |
| `~/corpora/mineral-refs/amcsd/` | 10,719 CIFs → coordination numbers and bond lengths (2,152 minerals) |
| `~/corpora/mineral-refs/wurm/` | ab-initio Raman modes with symmetry labels (461 minerals; HTML **and** XML) |

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
- **`interconnect.py`** — the eight views: forward, inverse, cross-modal,
  contrastive, corroboration, **structure, polymorph, computed**.
  Oversampling is delivered as distinct framings, not repeated copies.
- **`cif_features.py`** — AMCSD CIF → coordination numbers, bond lengths,
  bond-length spread, true space group. Uses pymatgen for symmetry
  expansion; `--validate` checks coordination against textbook values for
  six minerals before the extraction is trusted, because a missed
  symmetry operation yields too few atoms and understates every
  coordination number with no error raised.
- **`wurm_layer.py`** — joins the two halves of the WURM mirror: the HTML
  carries per-mode intensities and symmetry labels, the XML carries the
  frequencies. Joined on mode index, and only when the mode COUNTS agree.
- **`augmented_mlp.py` / `composition_mlp.py` / `retrieval_baseline.py`** —
  the no-LLM controls. Any claim about what the language model
  contributes is measured against these, not asserted.

### Why structure is in the corpus at all

Run 2 measured it. Everything the model learned it read off the chemical
FORMULA; the mineral name was worse than useless (name-only scored below
a constant-prompt control); and on polymorphs — same formula, different
structure — every predictor sat at the unrelated-minerals floor. LoRA on
the backbone changed nothing, which is the signature of a MISSING INPUT
rather than missing capacity. Composition cannot encode bonding geometry
and Raman frequency is bonding geometry. See PROCEDURE.md §13–§15.

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
