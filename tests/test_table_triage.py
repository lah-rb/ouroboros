"""agent/table_triage.py: parsing, the numbers outcome, the blind A/B order
and the apply gate (2026-10-02)."""

from __future__ import annotations

from agent import table_triage as tt

OCR = (
    "<table><tr><td style='text-align: center;'>Mass</td><td>Loss</td><td></td></tr>"
    "<tr><td>SSL</td><td>9334.4</td><td>12</td></tr></table>"
)
SHIFTED = (
    "<table><tr><td>Sample</td><td>Mass</td><td>Loss</td></tr>"
    "<tr><td>SSL</td><td>9334.4</td><td>12</td></tr></table>"
)


def test_parse_takes_the_last_verdict_and_its_table():
    text = (
        "VERDICT: ok\n(rechecking row 2)\n"
        "VERDICT: fixed\nPROBLEMS:\n- header lost 'Sample'; every heading one column left\n"
        "- row SSL fine\nTABLE:\n<table><tr><td>draft</td></tr></table>\n"
        "<table><tr><td>final</td></tr></table>"
    )
    d = tt.parse_triage(text)
    assert d["verdict"] == "fixed"
    assert d["problems"] == [
        "header lost 'Sample'; every heading one column left",
        "row SSL fine",
    ]
    assert d["html"] == "<table><tr><td>final</td></tr></table>"
    assert tt.parse_triage("VERDICT: **ok**")["verdict"] == "ok"
    assert tt.parse_triage("looks fine")["verdict"] == "unparsed"


def test_parse_choice():
    assert tt.parse_choice("CHOICE: B\nREASON: A shifts the header") == "B"
    assert tt.parse_choice("CHOICE: A ... on reflection CHOICE: SAME") == "SAME"
    assert tt.parse_choice("B is better") == "unparsed"


def test_numbers_outcome_kinds():
    assert tt.numbers_outcome(OCR, SHIFTED)["kind"] == "structure-only"
    fused = "<table><tr><td>123,728</td></tr></table>"
    split = "<table><tr><td>12</td></tr><tr><td>3,728</td></tr></table>"
    assert (
        tt.numbers_outcome(fused, split)["kind"] == "structure-only"
    ), "a split is structure"
    fixed_digit = SHIFTED.replace("9334.4", "933.4")
    assert tt.numbers_outcome(fixed_digit, SHIFTED)["kind"] == "from-image"
    dropped = SHIFTED.replace("<td>12</td>", "<td></td>")
    out = tt.numbers_outcome(SHIFTED, dropped)
    assert out["kind"] == "lost" and out["missing"] == ["12"]


def test_verify_order_is_seeded_and_balanced():
    keys = [f"t{i}" for i in range(400)]
    firsts = [tt.verify_order(k) for k in keys]
    assert firsts == [tt.verify_order(k) for k in keys], "same key, same question"
    assert 150 < sum(firsts) < 250
    k_a = next(k for k in keys if tt.verify_order(k))
    k_b = next(k for k in keys if not tt.verify_order(k))
    assert tt.prefers_fix(k_a, "A") is True and tt.prefers_fix(k_a, "B") is False
    assert tt.prefers_fix(k_b, "B") is True and tt.prefers_fix(k_b, "A") is False
    assert tt.prefers_fix(k_a, "SAME") is None
    p = tt.verify_prompt(k_a, OCR, SHIFTED)
    assert p.index("Sample") < p.index("<B>"), "the fix is A for this key"
    assert "style=" not in p


def test_gate():
    fixed = {"verdict": "fixed", "html": SHIFTED}
    k_a = next(k for k in (f"t{i}" for i in range(50)) if tt.verify_order(k))
    assert tt.gate({"verdict": "ok"}, OCR, None, k_a)["apply"] is False
    noop = {"verdict": "fixed", "html": OCR.replace("style='text-align: center;'", "")}
    assert tt.gate(noop, OCR, None, k_a)["reason"] == "no-op fix"
    # cells moved: only the blind read decides
    assert tt.gate(fixed, OCR, "A", k_a)["apply"] is True
    assert tt.gate(fixed, OCR, "B", k_a)["apply"] is False
    assert (
        tt.gate(fixed, OCR, "SAME", k_a)["reason"]
        == "cells moved; blind read undecided"
    )
    # numbers read from the image apply without the blind read
    from_image = {
        "verdict": "fixed",
        "html": SHIFTED.replace("<td></td>", "<td>7.1</td>"),
    }
    ocr_short = OCR.replace("9334.4", "933.4.4")
    g = tt.gate(from_image, ocr_short, None, k_a)
    assert g["apply"] is True and g["numbers"]["kind"] == "from-image"
    # numbers lost keep the OCR table
    lost = {"verdict": "fixed", "html": SHIFTED.replace("<td>12</td>", "<td></td>")}
    assert tt.gate(lost, OCR, "A", k_a) == {
        "apply": False,
        "reason": "numbers lost",
        "numbers": tt.numbers_outcome(tt.slim(OCR), lost["html"]),
    }
