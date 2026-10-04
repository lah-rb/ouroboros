"""The shared validation-check-result constructor (dedup of 7 inline sites)."""

from agent.actions.check_result import check_result


def test_shape_is_the_seven_key_contract():
    r = check_result("n", "cmd", True)
    assert set(r) == {
        "name",
        "command",
        "passed",
        "required",
        "stdout",
        "stderr",
        "return_code",
    }


def test_return_code_defaults_from_passed():
    assert check_result("n", "c", True)["return_code"] == 0
    assert check_result("n", "c", False)["return_code"] == 1
    # explicit return_code wins (the validator / probe pass the real rc)
    assert check_result("n", "c", False, return_code=5)["return_code"] == 5


def test_required_defaults_true_and_overridable():
    assert check_result("n", "c", True)["required"] is True
    assert check_result("n", "c", True, required=False)["required"] is False


def test_output_is_kept_whole():
    """The 500-char head cut kept pytest's header and lost its failures —
    the part every judge reads. Rows keep the whole output now; the prompt
    that renders them sizes them to the window (2026-09-26)."""
    out = "collected 40 items\n" + "." * 2000 + "\nFAILED test_x - AssertionError"
    r = check_result("n", "c", False, stdout=out, stderr=out)
    assert r["stdout"] == out and r["stderr"] == out
    # None-safe
    assert check_result("n", "c", True, stdout=None, stderr=None)["stdout"] == ""
