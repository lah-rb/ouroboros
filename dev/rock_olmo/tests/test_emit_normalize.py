"""Decimal normalisation of the training text (emit-time, never the databank)."""

from emit import COMMA_CONVENTION_MIN_EVIDENCE, normalize_decimals


def test_raised_dot_becomes_point():
    text = "<td>63\\cdot 57</td> Zn 65\\cdot 37 ; Ag 107·88 ; 39\\cdot4"
    out, n = normalize_decimals(text)
    assert out == "<td>63.57</td> Zn 65.37 ; Ag 107.88 ; 39.4"
    assert n["cdot"] == 4 and n["comma"] == 0


def test_power_of_ten_product_is_left_alone():
    out, n = normalize_decimals("k = 2\\cdot 10^{-3} and 5\\cdot 10^{4}")
    assert "\\cdot 10^" in out and n["cdot"] == 0


def test_comma_decimals_convert_only_under_the_convention():
    english = "samples 1,2 and 3 were 4.4 um; batches 1,2,3,4"
    out, n = normalize_decimals(english)
    assert out == english and n["comma"] == 0
    french = (
        " ".join(f"{10 + i},{i} %" for i in range(COMMA_CONVENTION_MIN_EVIDENCE))
        + " et 5,5 mm"
    )
    out, n = normalize_decimals(french)
    assert "10.0 %" in out and "5.5 mm" in out
    assert n["comma"] == COMMA_CONVENTION_MIN_EVIDENCE + 1


def test_thousands_groups_are_not_decimals():
    text = (
        " ".join(f"{10 + i},{i}" for i in range(COMMA_CONVENTION_MIN_EVIDENCE))
        + " over 1,234,567 counts"
    )
    out, _ = normalize_decimals(text)
    assert "1,234,567" in out


def test_comma_pass_can_be_disabled_and_is_idempotent():
    text = " ".join(f"{10 + i},{i}" for i in range(COMMA_CONVENTION_MIN_EVIDENCE))
    out, n = normalize_decimals(text, commas=False)
    assert out == text and n["comma"] == 0
    once, _ = normalize_decimals(text)
    twice, n2 = normalize_decimals(once)
    assert once == twice and n2 == {"cdot": 0, "comma": 0}


def test_four_place_comma_decimals_convert_and_vote():
    doc = "zwischen 0,0555 und 0,0571, also etwa bei 0,0563; 0,1234 und 0,9876; 1,234 counts"
    out, n = normalize_decimals(doc)
    assert "0.0571" in out and "0.9876" in out
    assert "1,234 counts" in out  # a three-digit tail stays a thousands group
    assert n["comma"] == 5
