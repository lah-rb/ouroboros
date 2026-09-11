"""The template gate: every problem class the synthesis bank must never hold."""

from __future__ import annotations

from agent.actions.cloud_limits import is_provider_limit
from agent.actions.synth_gate import (
    bank_row,
    literal_entities,
    normalise_slots,
    numbers_survive,
    render_check,
    signature,
    slots_in,
    template_id,
    skeleton_jaccard,
    validate_template,
)

SPEC = {
    "slots": {
        "species": {},
        "sp_f": {},
        "formula": {},
        "bands": {},
        "peak_list": {},
        "laser": {},
        "how": {},
        "tol": {},
        "instrument_class": {},
    },
    "subject_slots": ["species", "sp_f"],
    "answer_slots": {
        "forward": ["bands", "peak_list"],
        "backward": ["species", "sp_f"],
    },
    "sample_fills": [
        {
            "species": "Quartz",
            "sp_f": "Quartz (SiO2)",
            "formula": "SiO2",
            "bands": "128, 206.3, 464.8 cm-1",
            "peak_list": "128.0 (0.31), 206.3 (0.55), 464.8 (1.00)",
            "laser": " at 532 nm excitation",
            "how": "peak-picked from the RRUFF spectrum",
            "tol": "±2 cm-1",
            "instrument_class": "portable",
        }
    ],
}


def _fwd(
    prompt="Field note{laser}: the sample logged as {species} gave peaks at",
    completion=" {peak_list}; positions {how}.",
    **kw,
):
    return {
        "prompt": prompt,
        "completion": completion,
        "direction": "forward",
        "form": "statement",
        **kw,
    }


def _bwd(
    prompt="A {instrument_class} unit{laser} picked peaks at {peak_list}. The phase is",
    completion=" {species}.",
    **kw,
):
    return {
        "prompt": prompt,
        "completion": completion,
        "direction": "backward",
        "form": "statement",
        **kw,
    }


def test_good_templates_pass_both_directions():
    assert validate_template(_fwd(), SPEC) == []
    assert validate_template(_bwd(), SPEC) == []


def test_hash_slot_spelling_is_normalised():
    t = _fwd(prompt="Peaks for #{species}:", completion=" #{peak_list}.")
    assert validate_template(t, SPEC) == []
    assert normalise_slots(t["prompt"]) == "Peaks for {species}:"
    assert slots_in(t["prompt"] + t["completion"]) == ["species", "peak_list"]


def test_digits_and_unicode_digits_rejected():
    assert "digit in template" in validate_template(
        _fwd(completion=" {peak_list}, calibrated on the 520 cm-1 silicon line."), SPEC
    )
    assert "digit in template" in validate_template(
        _fwd(prompt="SiO₂-type {species} shows"), SPEC
    )


def test_unknown_and_malformed_slots():
    assert any(
        p.startswith("unknown slot laser_nm")
        for p in validate_template(_fwd(prompt="{species}{laser_nm}:"), SPEC)
    )
    # a capitalised typo is an UNKNOWN slot (the spec decides names); only a
    # brace group that cannot be a name is malformed
    assert any(
        p.startswith("unknown slot Species")
        for p in validate_template(_fwd(prompt="{Species} shows"), SPEC)
    )
    assert "malformed placeholder" in validate_template(
        _fwd(prompt="{ } shows {species}"), SPEC
    )
    assert "malformed placeholder" in validate_template(
        _fwd(prompt="{0} shows {species}"), SPEC
    )


def test_direction_rules():
    # forward without a subject slot in the prompt
    assert "no subject slot in prompt (forward)" in validate_template(
        _fwd(prompt="The picked peaks were", completion=" {peak_list} for {species}."),
        SPEC,
    )
    # forward without an answer slot in the completion
    assert "no answer slot in completion (forward)" in validate_template(
        _fwd(prompt="{species} was measured{laser}.", completion=" Positions {how}."),
        SPEC,
    )
    # backward that still names the species in its prompt
    probs = validate_template(
        _bwd(prompt="{species}: peaks at {peak_list}; the phase is"), SPEC
    )
    assert "species slot in prompt (backward)" in probs
    # backward without a value slot
    assert "no value slot in prompt (backward)" in validate_template(
        _bwd(prompt="A {instrument_class} unit was used. The phase is"), SPEC
    )
    # backward whose completion names nothing
    assert "no species slot in completion (backward)" in validate_template(
        _bwd(completion=" {bands}."), SPEC
    )


def test_literal_entities_and_script():
    probs = validate_template(
        _fwd(prompt="Like quartz, {species} shows peaks at"),
        SPEC,
        known_entities={"quartz", "calcite"},
    )
    assert any(p.startswith("literal entity quartz") for p in probs)
    assert literal_entities("Iron-bearing {species}", {"quartz"}) == []
    assert "non-Latin script" in validate_template(_fwd(prompt="Пики {species}:"), SPEC)


def test_duplicate_near_duplicate_and_forbidden():
    a = _fwd()
    sig = signature(a["prompt"], a["completion"])
    assert "duplicate signature" in validate_template(a, SPEC, banked_sigs={sig})
    assert "reproduces a v4/probe frame" in validate_template(
        a, SPEC, forbidden_sigs={sig}
    )
    # a synonym swap keeps the skeleton -> near-duplicate
    b = _fwd(prompt="Field note{laser}: the specimen logged as {species} gave peaks at")
    probs = validate_template(
        b, SPEC, banked_texts=[a["prompt"] + " " + a["completion"]]
    )
    assert any(p.startswith("near-duplicate") for p in probs)
    # a genuinely different structure passes beside it
    c = _fwd(
        prompt="Which peaks did {species} give{laser}? The picked list was",
        completion=" {peak_list}.",
    )
    assert (
        validate_template(c, SPEC, banked_texts=[a["prompt"] + " " + a["completion"]])
        == []
    )
    assert skeleton_jaccard(a["prompt"], c["prompt"]) < 0.3


def test_render_check_and_numbers_survive():
    assert render_check("{species} {nope}", " x", SPEC["sample_fills"]).startswith(
        "render failed: KeyError"
    )
    fill = SPEC["sample_fills"][0]
    text = "Quartz gave 128.0 (0.31), 206.3 (0.55), 464.8 (1.00) at 532 nm"
    assert numbers_survive(
        text, {"peak_list": fill["peak_list"], "laser": fill["laser"]}
    )
    assert not numbers_survive(
        "Quartz gave 128.0 (0.31)", {"peak_list": fill["peak_list"]}
    )


def test_bank_row_shape_and_stable_id():
    row = bank_row(
        _fwd(structure="field_note"),
        "raman_bands",
        "measurement_report",
        {"model": "muse"},
    )
    assert row["template_id"] == template_id(
        "raman_bands", "measurement_report", "forward", row["prompt"], row["completion"]
    )
    assert row["slots"] == ["laser", "species", "peak_list", "how"]
    assert row["signature"].endswith("|laser,species,peak_list,how")
    assert row["structure"] == "field_note" and row["provenance"] == {"model": "muse"}


def test_provider_limit_regex():
    assert is_provider_limit("You've hit your usage limit until 3pm")
    assert is_provider_limit("HTTP 429 Too Many Requests")
    assert not is_provider_limit("limited-range prompt")
    assert not is_provider_limit(None)


def test_slot_names_with_digits_or_case_are_not_digits_or_malformed():
    spec = dict(SPEC)
    spec["slots"] = {
        **SPEC["slots"],
        "top3": {},
        "libs_top3": {},
        "libs_T": {},
        "T": {},
    }
    spec["answer_slots"] = {
        "forward": ["bands", "peak_list", "top3", "libs_top3"],
        "backward": ["species", "sp_f"],
    }
    spec["sample_fills"] = [
        {
            **SPEC["sample_fills"][0],
            "top3": "128, 206.3, 464.8 cm-1",
            "libs_top3": "288.16, 251.61, 777.19 nm",
            "libs_T": "10,000",
            "T": "10,000",
        }
    ]
    t = _fwd(
        prompt="At {libs_T} K ({T}), {species} shows its three strongest bands at",
        completion=" {top3}; lines {libs_top3}.",
    )
    assert validate_template(t, spec) == []
    assert slots_in(t["prompt"] + t["completion"]) == [
        "libs_T",
        "T",
        "species",
        "top3",
        "libs_top3",
    ]
