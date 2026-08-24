"""Curator gate helpers: grounding, registry, envelope, curator doc.

The deterministic core of stage 3 — every model output is a CLAIM and
these are the checks that admit it. Also pins the fig_review sidecar's
pure helpers (caption_context/numeric_overlap), imported from the tool
file directly (its module-level imports are stdlib-only by contract).
"""

from __future__ import annotations

import importlib.util
import os

from agent.actions.curation_actions import (
    build_curator_doc,
    format_key_registry,
    grounding_check,
    near_duplicate_keys,
    registry_check,
    required_fields_check,
    update_key_registry,
)

_FIXTURE_MD = """# Strong CoCrFeMnNi at cryogenic temperatures

We measure a yield strength of 759 MPa at 77 K with elongation of 71%.
Grain size after annealing was 4.4 um.

<div><img src="../figures/doi_10.1000_x/fig_00.png" alt="Image"></div>

Figure 1 shows stress-strain curves at 77, 193 and 293 K.

Tables report hardness 458 HV after cold rolling.
"""

_FIGTEXT = {
    "paper_key": "doi_10.1000_x",
    "model": "qwen3-vl-8b",
    "figs": [
        {
            "fig": "fig_00.png",
            "caption": "Figure 1 stress-strain",
            "figtext": "Stress-strain curves; ultimate strength reaches 1280 MPa at 77 K.",
            "numeric_overlap_rate": 0.5,
        },
        {
            "fig": "fig_orphan.png",
            "caption": "",
            "figtext": "SEM micrograph of dendrites.",
        },
    ],
}


# ── curator doc ───────────────────────────────────────────────────────


def test_build_curator_doc_inlines_after_reference_and_appends_orphans():
    doc = build_curator_doc(_FIXTURE_MD, _FIGTEXT)
    ref_pos = doc.index("fig_00.png")
    mark_pos = doc.index("[FIGURE fig_00.png — VLM reading]")
    assert mark_pos > ref_pos
    # The reading lands with its paragraph, before the next prose block.
    assert mark_pos < doc.index("Tables report hardness")
    assert "Unanchored figures" in doc
    assert "SEM micrograph" in doc


def test_build_curator_doc_no_figtext_is_identity():
    assert build_curator_doc(_FIXTURE_MD, None) == _FIXTURE_MD
    assert build_curator_doc(_FIXTURE_MD, {"figs": []}) == _FIXTURE_MD


# ── grounding ─────────────────────────────────────────────────────────


def test_grounding_passes_on_stated_values_including_figtext():
    doc = build_curator_doc(_FIXTURE_MD, _FIGTEXT)
    data = {
        "yield_strength_mpa": 759,
        "test_conditions": "77 K",
        "hardness_hv": 458,
        # From the inlined VLM reading — groundable via the combined doc.
        "ultimate_strength_mpa": 1280,
        "grain_size_um": 4.4,
    }
    result = grounding_check(data, doc)
    assert result["passed"] is True
    assert result["grounding_rate"] == 1.0
    assert result["numeric_leaves"] == 5


def test_grounding_fails_on_fabricated_and_converted_values():
    data = {
        "yield_strength_gpa": 0.759,  # unit conversion the paper never states
        "creep_rate": 3.1e-7,  # fabricated
        "yield_strength_mpa": 759,  # grounded
    }
    result = grounding_check(data, _FIXTURE_MD)
    assert result["passed"] is False
    bad_tokens = {u["token"] for u in result["ungrounded"]}
    assert "0.759" in bad_tokens
    assert not any(u["token"] == "759" for u in result["ungrounded"])


def test_grounding_walks_nested_lists_and_strings():
    data = {"tensile_tests": [{"temperature_k": 77, "note": "elongation 71%"}]}
    result = grounding_check(data, _FIXTURE_MD)
    assert result["passed"] is True and result["numeric_leaves"] == 2


def test_grounding_empty_data_passes():
    assert grounding_check({}, _FIXTURE_MD)["passed"] is True


# ── envelope ──────────────────────────────────────────────────────────


def _envelope(**over):
    env = {
        "paper_key": "doi_10.1000_x",
        "title": "Strong CoCrFeMnNi",
        "doi": "10.1000/x",
        "license": "cc-by",
        "review": {"status": "accepted", "summary": "solid tensile data"},
        "data": {"yield_strength_mpa": 759},
    }
    env.update(over)
    return env


def test_required_fields_pass_and_failures():
    assert required_fields_check(_envelope()) == []
    problems = required_fields_check(
        _envelope(title="", doi="", review={"status": "maybe"}, data={})
    )
    assert "missing title" in problems
    assert "missing doi|arxiv_id" in problems
    assert "review.status not in accepted|denied" in problems
    assert "missing review.summary" in problems
    assert "data empty or not an object" in problems


def test_arxiv_id_satisfies_identifier_requirement():
    assert required_fields_check(_envelope(doi="", arxiv_id="2401.01234")) == []


# ── registry ──────────────────────────────────────────────────────────


def test_registry_check_types_and_new_keys():
    registry = {}
    update_key_registry(registry, {"yield_strength_mpa": 759, "phases": ["fcc"]}, "p1")
    assert registry["yield_strength_mpa"]["type"] == "number"
    assert registry["phases"]["type"] == "list[string]"

    result = registry_check(
        {"yield_strength_mpa": "759 MPa", "phases": ["fcc", "bcc"], "hardness_hv": 458},
        registry,
    )
    assert result["type_mismatches"] == [
        {"key": "yield_strength_mpa", "expected": "number", "actual": "string"}
    ]
    assert result["new_keys"] == ["hardness_hv"]
    assert result["reused_keys"] == ["yield_strength_mpa", "phases"]


def test_near_duplicate_flags_synonym_not_distinct():
    registry = {}
    update_key_registry(registry, {"yield_strength": 759}, "p1")
    data = {"yield_strengths": 800, "elongation_pct": 71}
    flags = near_duplicate_keys(["yield_strengths", "elongation_pct"], registry, data)
    assert len(flags) == 1
    assert flags[0]["new_key"] == "yield_strengths"
    assert flags[0]["existing"] == "yield_strength"


def test_near_duplicate_requires_type_compatibility():
    registry = {}
    update_key_registry(registry, {"yield_strength": 759}, "p1")
    # Same-ish name but different value type -> not a synonym flag.
    flags = near_duplicate_keys(
        ["yield_strengths"], registry, {"yield_strengths": ["a", "b"]}
    )
    assert flags == []


def test_registry_update_counts_and_format_block():
    registry = {}
    update_key_registry(registry, {"yield_strength_mpa": 759}, "p1")
    update_key_registry(registry, {"yield_strength_mpa": 800, "hardness_hv": 458}, "p2")
    assert registry["yield_strength_mpa"]["count"] == 2
    assert registry["yield_strength_mpa"]["first_paper"] == "p1"

    block = format_key_registry(registry)
    # Most-used first, exemplar shown.
    assert block.index("yield_strength_mpa") < block.index("hardness_hv")
    assert "e.g. 759" in block
    assert "coin clear" in format_key_registry({})  # empty-registry guidance


# ── fig_review pure helpers (tool file, stdlib-only contract) ─────────


def _fig_review_module():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "tools", "fig_review", "fig_review.py")
    spec = importlib.util.spec_from_file_location("fig_review", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # stdlib-only imports by contract
    return mod


def test_caption_context_finds_reference_paragraphs():
    fr = _fig_review_module()
    ctx = fr.caption_context(_FIXTURE_MD, "doi_10.1000_x", "fig_00.png")
    assert "stress-strain curves" in ctx.lower()
    assert "<div>" not in ctx  # markup stripped
    assert fr.caption_context(_FIXTURE_MD, "doi_10.1000_x", "fig_99.png") == ""


def test_numeric_overlap_advisory():
    fr = _fig_review_module()
    assert fr.numeric_overlap("values 759 and 4.4", _FIXTURE_MD) == 1.0
    assert fr.numeric_overlap("reads 999 everywhere", _FIXTURE_MD) == 0.0
    assert fr.numeric_overlap("no numbers here", _FIXTURE_MD) == 1.0


def test_grounding_matches_negative_values():
    """_norm strips '-' from the doc; the packed token must match
    unsigned — live, every negative quantity failed grounding while
    sitting verbatim in the paper's tables."""
    doc = "<table><tr><td>-448</td></tr></table> theta is negative."
    result = grounding_check({"curie_theta_k": -448}, doc)
    assert result["passed"] is True
    # A genuinely absent negative still fails.
    assert grounding_check({"curie_theta_k": -999}, doc)["passed"] is False


def test_registry_scalar_list_widening_is_compatible():
    registry = {}
    update_key_registry(registry, {"lattice_parameter_angstrom": 3.59}, "p1")
    result = registry_check({"lattice_parameter_angstrom": [3.59, 3.61]}, registry)
    assert result["type_mismatches"] == []
    # Real drift still fails.
    result = registry_check({"lattice_parameter_angstrom": "3.59 A"}, registry)
    assert len(result["type_mismatches"]) == 1


# ── decimal-comma documents (2026-08-24) ─────────────────────────────
#
# The comma-stripped compact form exists so thousands separators ground.
# On a document that writes decimals with commas it destroyed the number
# instead: "57,65 %" -> "5765", so a correctly parsed 57.65 could never
# match and the paper was rejected for being RIGHT. 46% of corpus
# markdown carries decimal-comma numbers; 10% use the convention
# predominantly (fr/es/pt/ru/de and European-published English).

_ES_DOC = (
    "Composicion quimica: SiO2 57,65 % | Al2O3 13,69 % | Fe2O3 5,77 % "
    "| MgO 1,81 %. Radiacion XPS 1486,6 eV medida a 25,4 grados. "
    "Tamano de particula 11,3 nm y 10,8 nm."
)


def test_decimal_comma_document_grounds_point_parsed_values():
    data = {"sio2_percent": 57.65, "al2o3_percent": 13.69, "xps_ev": 1486.6}
    result = grounding_check(data, _ES_DOC)
    assert result["passed"], result
    assert result["grounding_rate"] == 1.0


def test_thousands_separators_still_ground():
    doc = "The detector recorded 1,234,567 counts over 12.5 seconds."
    assert grounding_check({"counts": 1234567}, doc)["passed"]


def test_enumeration_commas_do_not_manufacture_a_decimal():
    """The false positive the convention gate exists to prevent."""
    doc = "Samples 1,2 and 3 were annealed at 12.5 K for 4.0 hours."
    result = grounding_check({"fabricated": 1.2}, doc)
    assert not result["passed"]
    assert result["grounding_rate"] == 0.0


def test_point_convention_document_is_left_alone():
    """A doc that uses points keeps the strict reading — no widening."""
    doc = "Values 3.14 and 2.72 measured; batches 1,2,3,4,5,6,7 prepared."
    assert not grounding_check({"bogus": 1.2}, doc)["passed"]
    assert grounding_check({"pi": 3.14}, doc)["passed"]


def test_sparse_comma_use_does_not_trigger_the_variant():
    """Below the frequency floor the strict reading stands."""
    doc = "One value 5,5 appears here; everything else uses 1.0 and 2.0 style."
    assert not grounding_check({"v": 5.5}, doc)["passed"]


def test_citation_brackets_do_not_vote_for_the_comma_convention():
    """Single-digit pairs are the look-alike; only measurements vote."""
    doc = (
        "As reported [1,2] and confirmed [3,4] and again [5,6] and [7,8] "
        "and [9,1] the value was 3.75 units."
    )
    assert not grounding_check({"fabricated": 1.2}, doc)["passed"]


def test_comma_convention_survives_a_point_heavy_markup_document():
    """Real corpus shape: paddle HTML + DOIs supply many point-decimals
    while the body text uses comma decimals (measured 94 vs 190)."""
    # Six+ MEASUREMENT-shaped commas (multi-digit integer part) — the
    # evidence floor deliberately ignores "5,77"-style single-digit pairs
    # because citation brackets look identical.
    body = " ".join(
        f"<td style='text-align: center; word-wrap: break-word;'>{v}</td>"
        for v in ("57,65", "13,69", "45,3", "39,8", "1486,6", "25,4", "11,3")
    )
    doc = f"doi 10.5281/zenodo.6790073 v1.0 rev 2.3 tabla 1.2 {body}"
    result = grounding_check({"sio2": 57.65, "al2o3": 13.69, "sat": 45.3}, doc)
    assert result["passed"], result
