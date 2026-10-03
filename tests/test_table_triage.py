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


def test_grid_widths_count_spans():
    t = (
        "<table><tr><td rowspan=2>A</td><td colspan=2>B</td></tr>"
        "<tr><td>1</td><td>2</td></tr><tr><td>x</td><td>3</td><td>4</td></tr></table>"
    )
    assert tt.grid_widths(t) == [3, 3, 3] and tt.is_rectangular(t)
    short = t.replace(
        "<tr><td>x</td><td>3</td><td>4</td></tr>", "<tr><td>3</td><td>4</td></tr>"
    )
    assert not tt.is_rectangular(short)


def test_gate():
    fixed = {"verdict": "fixed", "html": SHIFTED}
    assert tt.gate({"verdict": "ok"}, OCR)["apply"] is False
    noop = {"verdict": "fixed", "html": OCR.replace("style='text-align: center;'", "")}
    assert tt.gate(noop, OCR)["reason"] == "no-op fix"
    # cells moved, grid intact: applied
    g = tt.gate(fixed, OCR)
    assert g["apply"] is True and g["numbers"]["kind"] == "structure-only"
    # numbers read from the image: applied
    from_image = {"verdict": "fixed", "html": SHIFTED.replace("9334.4", "9334.5")}
    g = tt.gate(from_image, OCR)
    assert g["apply"] is True and g["numbers"]["kind"] == "from-image"
    # numbers lost: kept
    lost = {"verdict": "fixed", "html": SHIFTED.replace("<td>12</td>", "<td></td>")}
    assert tt.gate(lost, OCR)["reason"] == "numbers lost"
    # a rectangular OCR grid broken into unequal rows (bad-03, bad-12): kept
    broken = {"verdict": "fixed", "html": SHIFTED.replace("<td>SSL</td>", "")}
    broken["html"] = broken["html"].replace(
        "<td>12</td>", "<td>12</td><td>SSL</td><td></td>"
    )
    assert tt.gate(broken, OCR)["reason"] == "grid broken (rows of unequal width)"
    # ... but an OCR grid that was ragged already does not veto (ragged-04)
    ragged_ocr = OCR.replace("<td></td></tr>", "</tr>", 1)
    assert not tt.is_rectangular(ragged_ocr)
    assert (
        tt.gate({"verdict": "fixed", "html": broken["html"]}, ragged_ocr)["apply"]
        is True
    )
