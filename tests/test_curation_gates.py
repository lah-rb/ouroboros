"""Curator gate helpers: grounding, registry, envelope, curator doc.

The deterministic core of stage 3 — every model output is a CLAIM and
these are the checks that admit it. Also pins the fig_review sidecar's
pure helpers (caption_context/numeric_overlap), imported from the tool
file directly (its module-level imports are stdlib-only by contract).
"""

from __future__ import annotations

import importlib.util
import os

import pytest

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
        "identifier": "10.1000/x",
        "identifier_kind": "doi",
        "license": "cc-by",
        "review": {"status": "accepted", "summary": "solid tensile data"},
        "data": {"yield_strength_mpa": 759},
    }
    env.update(over)
    return env


def test_required_fields_pass_and_failures():
    assert required_fields_check(_envelope()) == []
    problems = required_fields_check(
        _envelope(
            title="",
            doi="",
            identifier="",
            identifier_kind="",
            review={"status": "maybe"},
            data={},
        )
    )
    assert "missing title" in problems
    assert "missing identifier_kind (expected a tier or 'none')" in problems
    assert "review.status not in accepted|denied" in problems
    assert "missing review.summary" in problems
    assert "data empty or not an object" in problems


def test_any_resolved_identifier_tier_satisfies_the_envelope():
    for ident, kind in (
        ("2401.01234", "arxiv"),
        ("W2049875903", "openalex"),
        ("10662/12345", "hdl"),
        ("2013PA112254", "theses.fr"),
        ("30695665", "core"),
    ):
        assert (
            required_fields_check(
                _envelope(doi="", identifier=ident, identifier_kind=kind)
            )
            == []
        ), kind


def test_an_explicit_none_is_accepted_but_a_silent_gap_is_not():
    """Operator ruling 2026-09-03: resolve what we can, emit the rest as
    identifier: none. 'none' is a RECORDED state and passes; a missing
    identity block is an unanswered question and does not."""
    assert (
        required_fields_check(_envelope(doi="", identifier="", identifier_kind="none"))
        == []
    )
    assert (
        "missing identifier_kind (expected a tier or 'none')"
        in required_fields_check(_envelope(doi="", identifier="", identifier_kind=""))
    )
    # a tier that claims an identifier must carry one
    assert "identifier_kind 'hdl' with no identifier" in required_fields_check(
        _envelope(doi="", identifier="", identifier_kind="hdl")
    )


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
    assert "e.g. xxx" in block, "the exemplar's shape, its value masked (2026-10-02)"
    assert "759" not in block
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


def test_a_registry_entry_typed_bare_list_accepts_any_list():
    """CAUGHT LIVE (2026-09-03): a window grounding 547 values at 0.998 was
    thrown away because xps_peak_binding_energy_ev is registered `list` (its
    first paper's value was a mixed list) and the pack sent `list[object]`.
    An entry that says "a list of things" cannot refuse a list of one kind."""
    from agent.actions.curation_actions import _types_compatible

    assert _types_compatible("list", "list[object]")
    assert _types_compatible("list", "list[number]")
    assert _types_compatible("list[object]", "list")
    # real drift is still refused
    assert not _types_compatible("number", "string")
    assert not _types_compatible("object", "number")


# ── coinage guard (operator ruling 2026-09-04) ────────────────────────────


def _mature_registry(n_shared: int = 250, n_single: int = 100) -> dict:
    reg: dict = {}
    for i in range(n_shared):
        reg[f"shared_key_{i}"] = {"type": "number", "count": 3, "tier": "core"}
    for i in range(n_single):
        reg[f"single_key_{i}"] = {"type": "number", "count": 1, "tier": "bespoke-pool"}
    return reg


def test_coinage_verdict_holds_only_past_both_thresholds_on_a_mature_registry():
    from agent.actions.curation_actions import (
        COINAGE_MAX_NEW,
        coinage_verdict,
    )

    reg = _mature_registry()
    coining = {f"bespoke_{i}": i for i in range(COINAGE_MAX_NEW + 1)}
    # 41 new, 0 reused: both thresholds -> held.
    v = coinage_verdict(reg, coining)
    assert (
        v["coinage_quarantined"] == COINAGE_MAX_NEW + 1
        and "mature" in v["coinage_rule"]
    )
    # 41 new but 30 reused: the ratio saves it -- a rich paper speaking the
    # registry's language may still coin.
    rich = {**coining, **{f"shared_key_{i}": 1 for i in range(30)}}
    assert coinage_verdict(reg, rich)["coinage_quarantined"] == 0
    # Exactly the cap, 0 reused: not past the count threshold.
    at_cap = {f"bespoke_{i}": i for i in range(COINAGE_MAX_NEW)}
    assert coinage_verdict(reg, at_cap)["coinage_quarantined"] == 0


def test_coinage_verdict_never_holds_on_a_green_registry():
    """The first papers into a corpus coin nearly everything; a green
    registry (few keys seen in two or more papers) only logs."""
    from agent.actions.curation_actions import (
        COINAGE_MATURE_SHARED_KEYS,
        coinage_verdict,
    )

    green = _mature_registry(n_shared=COINAGE_MATURE_SHARED_KEYS - 1, n_single=500)
    v = coinage_verdict(green, {f"bespoke_{i}": i for i in range(300)})
    assert v["coinage_quarantined"] == 0
    assert "green" in v["coinage_rule"]
    assert v["registry_shared_keys"] == COINAGE_MATURE_SHARED_KEYS - 1
    assert coinage_verdict({}, {"a": 1, "b": 2})["coinage_quarantined"] == 0


@pytest.mark.asyncio
async def test_fold_holds_new_keys_in_quarantine_and_still_counts_reused():
    import json

    from agent.actions.curation_actions import (
        KEY_QUARANTINE_PATH,
        KEY_REGISTRY_PATH,
        fold_pack_into_registry,
    )
    from agent.effects.mock import MockEffects

    reg = _mature_registry()
    fx = MockEffects(files={KEY_REGISTRY_PATH: json.dumps(reg)})
    data = {f"bespoke_{i}": i for i in range(50)}
    data["shared_key_0"] = 7
    v = await fold_pack_into_registry(fx, reg, data, "p_newsletter")
    assert v["coinage_quarantined"] == 50
    assert "bespoke_0" not in reg, "held keys never enter the registry"
    assert reg["shared_key_0"]["count"] == 4, "reused keys still count"
    q = json.loads(fx._files[KEY_QUARANTINE_PATH])
    assert set(q) == {f"bespoke_{i}" for i in range(50)}
    assert (
        q["bespoke_1"]["first_paper"] == "p_newsletter" and q["bespoke_1"]["count"] == 1
    )
    saved = json.loads(fx._files[KEY_REGISTRY_PATH])
    assert "bespoke_0" not in saved and saved["shared_key_0"]["count"] == 4

    # A second paper coining the same key is evidence of vocabulary: the
    # quarantine count grows (the promote tool acts on that), still unfolded.
    v2 = await fold_pack_into_registry(
        fx, reg, {f"bespoke_{i}": i for i in range(45)}, "p_second"
    )
    assert v2["coinage_quarantined"] == 45
    q = json.loads(fx._files[KEY_QUARANTINE_PATH])
    assert q["bespoke_1"]["count"] == 2 and q["bespoke_1"]["papers"] == [
        "p_newsletter",
        "p_second",
    ]
    assert q["bespoke_47"]["count"] == 1

    # A modest pack folds normally and leaves the quarantine file alone.
    v3 = await fold_pack_into_registry(
        fx, reg, {"new_modest_key": 1, "shared_key_1": 2}, "p3"
    )
    assert v3["coinage_quarantined"] == 0 and "new_modest_key" in reg


def test_review_state_denies_a_non_paper_form_that_the_model_accepted():
    from agent.actions.curation_actions import NON_PAPER_FORMS, review_state_from

    assert "newsletter" in NON_PAPER_FORMS
    st = review_state_from(
        {
            "verdict": "accept",
            "document_form": "Newsletter",
            "summary": "one LIBS figure",
            "issues": ["fig 67 blurry"],
        }
    )
    assert st["status"] == "denied" and st["deny_category"] == "corpus_fit"
    assert st["document_form"] == "newsletter"
    assert (
        any("composition rule" in i for i in st["issues"])
        and "fig 67 blurry" in st["issues"]
    )
    # A real article is untouched; a model denial keeps its own category.
    ok = review_state_from(
        {"verdict": "accept", "document_form": "article", "summary": "s"}
    )
    assert ok["status"] == "accepted" and ok["deny_category"] == ""
    dn = review_state_from(
        {
            "verdict": "deny",
            "deny_category": "data_not_in_text",
            "document_form": "article",
        }
    )
    assert dn["status"] == "denied" and dn["deny_category"] == "data_not_in_text"
    # No form given: nothing changes -- the rule only acts on a named non-paper form.
    assert review_state_from({"verdict": "accept"})["status"] == "accepted"


def test_the_registry_widens_a_scalar_slot_instead_of_drifting():
    """T -> list[T] when a paper honestly reports several values.

    _types_compatible already lets the pack THROUGH, but before 2026-09-04
    the entry kept saying T forever, so the registry stopped describing its
    own data and the prompt kept showing a scalar -- which pushed models to
    coin the plural spelling as a separate key.
    """
    reg = {"spectrometer_model": {"type": "string", "count": 4}}
    update_key_registry(reg, {"spectrometer_model": ["A", "B"]}, "p9")
    e = reg["spectrometer_model"]
    assert e["type"] == "list[string]" and e["count"] == 5
    assert e["widened_at"], "the widening is stamped, not silent"
    # A widened slot still accepts the scalar form from the next paper.
    assert registry_check({"spectrometer_model": "C"}, reg)["type_mismatches"] == []
    # Real drift never widens.
    drift = {"k": {"type": "number", "count": 1}}
    update_key_registry(drift, {"k": "text"}, "p1")
    assert drift["k"]["type"] == "number"
    # Nor does a deeper container silently replace a shallow one.
    deep = {"k2": {"type": "list[string]", "count": 1}}
    update_key_registry(deep, {"k2": [{"a": 1}]}, "p2")
    assert deep["k2"]["type"] == "list[string]"


def test_an_accepted_lingual_paper_waits_for_translation_before_packing():
    """Review may run on the original; the PACK must be English (the
    pre-training targets are too small for multilingual packs). An accepted
    paper still flagged extract_lingual and untranslated is not pack-eligible;
    once translation lands (extraction_status extracted, translated=True) it
    is; an unreviewed lingual paper is still review-eligible."""
    from agent.actions.curation_actions import _curation_pending

    base = {"extraction_status": "extract_lingual", "figure_count": 0}
    unreviewed = dict(base)
    assert (
        _curation_pending(unreviewed) is True
    ), "the review still runs on the original"
    accepted_untranslated = dict(base, review_status="accepted")
    assert _curation_pending(accepted_untranslated) is False, "must wait for translate"
    translated = dict(
        base, review_status="accepted", extraction_status="extracted", translated=True
    )
    assert _curation_pending(translated) is True
    already_packed = dict(translated, pack_status="packed")
    assert _curation_pending(already_packed) is False


# ── raised-dot decimals (OCR of pre-1950s typography) ─────────────────
_CDOT_MD = (
    "<table><tr><td>Cu</td><td>63\\cdot 57</td><td>477</td></tr>"
    "<tr><td>Zn</td><td>65\\cdot 37</td><td>39\\cdot 4</td></tr></table>\n"
    "Silver 107·88. The constant k = 2\\cdot 10^{-3} throughout.\n"
)


def test_cdot_decimal_document_grounds_point_parsed_values():
    # Barkla 1911 as paddle renders it: a correctly packed 63.57 must ground.
    data = {"atomic_weight_cu": 63.57, "atomic_weight_zn": 65.37, "lambda": 39.4}
    result = grounding_check(data, _CDOT_MD)
    assert result["passed"] is True
    assert result["ungrounded"] == []


def test_unicode_middle_dot_decimal_grounds():
    result = grounding_check({"atomic_weight_ag": 107.88}, _CDOT_MD)
    assert result["passed"] is True


def test_cdot_power_of_ten_product_is_not_a_decimal():
    # "2\cdot 10^{-3}" is multiplication: 2.10 may never ground from it. Its
    # VALUE, 0.002, now does (2026-10-02): power-of-ten notation is how the
    # paper states 0.002, and the audit found 131 such correct values flagged.
    assert grounding_check({"k": 2.10}, _CDOT_MD)["passed"] is False
    assert grounding_check({"k": 0.002}, _CDOT_MD)["passed"] is True


def test_plain_document_unchanged_by_cdot_variant():
    # No raised dots anywhere: behaviour is byte-identical to before.
    assert grounding_check({"x": 63.57}, "Cu 63 and 57 separately")["passed"] is False
    assert grounding_check({"x": 4.4}, _FIXTURE_MD)["passed"] is True


def test_sub_unity_four_place_comma_decimals_ground():
    # Laue 1912: "zwischen 0,0555 und 0,0571, also etwa bei 0,0563" — single
    # leading digit, four places. Enough of them must engage the convention
    # and each must then convert.
    doc = (
        "Die Werte liegen zwischen 0,0555 und 0,0571, also etwa bei 0,0563; "
        "weiter 0,1234 und 0,9876 sowie 1,90.10^{-9} cm. Siehe [1,2] und Fig. 3,4."
    )
    result = grounding_check({"r": [0.0555, 0.0571, 0.0563, 0.1234, 0.9876, 1.90]}, doc)
    assert result["passed"] is True, result["ungrounded"]
    # Without the four-place values the same citation pairs do NOT engage the
    # convention (the pre-existing contract, test_enumeration_commas_...).
    assert grounding_check({"x": 1.2}, "Siehe [1,2] und Fig. 3,4.")["passed"] is False


def test_thousands_groups_still_not_decimals_under_wider_tail():
    doc = "0,0555 0,0571 0,0563 0,1234 0,9876 counts: 1,234 and 12,345,678"
    assert grounding_check({"n": 1.234}, doc)["passed"] is False
    assert grounding_check({"n": 12.345}, doc)["passed"] is False


# ── Whole-number grounding (2026-10-02) ──────────────────────────────
# Substring matching on the doc and on its space-and-comma-stripped copy let
# fragments and truncations ground; every case below is a production pack
# value from the audit, against the text its paper actually prints.


def _grounds(value, doc: str) -> bool:
    return grounding_check({"v": value}, doc)["passed"]


@pytest.mark.parametrize(
    "value,doc",
    [
        (0.5, "pulse energy (mJ) | 0.52 | 0.61"),  # truncated, not stated
        (0.0, "0.2363 ± 0.0008"),
        (63, "a He-Ne laser (632 nm)"),  # a space group from the laser line
        (200, "see ref. 2000"),
        (10.0, "axis ticks 0.1 0.05 0"),  # stripped copy read "0.10.050"
        (495.8, "lines at 1495.8 nm and 1506.3 nm"),
        (87, "olivine (2.87 Å)"),
        (51.2, "<td>51.25</td>"),
        (164, "dashed lines mark 1640 cm-1"),
    ],
)
def test_a_fragment_or_truncation_of_a_printed_number_does_not_ground(value, doc):
    assert not _grounds(value, doc)


@pytest.mark.parametrize(
    "value,doc",
    [
        (0.8, "a ratio of 0.80 was used"),  # trailing zero
        (200, "heated to 200.0 K."),
        (200, "up to 200."),
        (17500, "spun at 17,500 rpm"),  # thousands separator
        (4922, "4 922 spectra"),
        (15160, "$ \\Delta_{0} = 15, 160 $ B = 726"),  # comma-space thousands
        (0.10798, "λ 0.107 98 Å"),  # ISO-grouped fraction in an OCR'd cell
        (4.19, "<td>4. 19</td>"),  # OCR's split decimal point
        (10.0, "a 10 cm lens"),  # an integral float for a printed integer
        ([282, 430, 628], "absorption peaks at 282,430,628 nm"),  # a comma list
        (1107, "bands at 632,1107 cm-1"),
    ],
)
def test_printed_forms_of_a_stated_number_still_ground(value, doc):
    assert _grounds(value, doc)


def test_a_packed_identifier_grounds_by_its_whole_run():
    doc = "https://doi.org/10.1016/j.marpolbul.2011.05.030. Andrady 2003"
    assert _grounds("10.1016/j.marpolbul.2011.05.030", doc)
    assert _grounds("Nikon 283,008", "by optical microscopy (Nikon 283,008)")
    assert not _grounds("2011.05", "a value of 2011.053 was recorded")


# ── Printed forms that are not digit strings (2026-10-02 audit) ──────


@pytest.mark.parametrize(
    "value,doc",
    [
        (700000, "max output count rate 7 × 10^5 cps"),
        (0.0004, "a ratio of 4 × 10⁻⁴"),
        (100000000.0, "A = $ 1.0 \\times 10^{{8}} $ s^-1"),  # OCR'd double braces
        (1e-05, "a step of 0.00001 s"),  # repr(1e-05) used to tokenize as "05"
        ("5.9e-05 a.u.", "absorbance 0.000059"),
        (1.2e-05, "D = 1.2E-05 cm2/s"),
        (1.7e9, "about 1.7 billion years"),
        (53, "Fifty-three samples"),
        (0.93, "Na P₀.₉₃ O₄"),
        (80, "the ⁸⁰Kr isotope"),
        ("006", "reflections (0 0 6) and (1 1 0)"),
        (1000000, "magnified 1.000.000 x"),
        (0.87, "414 (0,87), 482 (0,93)"),  # lone decimal comma
        (1250, "7. 1,250 °C"),  # list marker beside a thousands group
        (23400, "Kα 23 400(570)"),  # value with its uncertainty
    ],
)
def test_other_printed_forms_of_a_stated_number_ground(value, doc):
    assert _grounds(value, doc)


@pytest.mark.parametrize(
    "value,doc",
    [
        (30, "a 3 x 10 mm slab"),  # a dimension, not 3 × 10^1
        (105, "a ratio of 10⁵ counts"),
        (128, "30 scans were averaged"),
        (50000, "precision 50.000 ± 0.002"),  # one period group: a decimal here
        (1.2, "as shown before [1,2]"),  # a citation pair is no decimal comma
    ],
)
def test_the_new_forms_do_not_ground_what_is_not_printed(value, doc):
    assert not _grounds(value, doc)


def test_registry_examples_are_masked_and_a_copied_mask_fails_the_gate():
    from agent.actions.curation_actions import (
        _run_pack_gates,
        format_key_registry,
        mask_exemplar,
    )

    assert mask_exemplar("1.54") == "x.xx"
    assert mask_exemplar('["ICSD 01-080-0019"]') == '["ICSD xx-xxx-xxxx"]'
    assert mask_exemplar('{"peak_cm-1": 1086, "f": "Fe2O3"}') == (
        '{"peak_cm-1": xxxx, "f": "Fe2O3"}'
    ), "key names and formulas are structure, not values"
    reg = {
        "xrd_wavelength_angstrom": {"type": "number", "count": 9, "exemplar": "1.54"}
    }
    assert "e.g. x.xx" in format_key_registry(reg)
    gates = _run_pack_gates({"reference_codes": ["ICSD xx-xxx-xxxx"]}, "no codes", {})
    assert not gates["passed"] and "MASKED EXAMPLE COPIED" in gates["feedback"]
