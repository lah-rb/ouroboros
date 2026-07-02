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
