"""The shared validation-check-result constructor (dedup of 7 inline sites)."""

from agent.actions.check_result import CHECK_OUTPUT_CAP, check_result


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


def test_output_capped_at_500():
    big = "x" * 1000
    r = check_result("n", "c", False, stdout=big, stderr=big)
    assert len(r["stdout"]) == CHECK_OUTPUT_CAP
    assert len(r["stderr"]) == CHECK_OUTPUT_CAP
    # None-safe
    assert check_result("n", "c", True, stdout=None, stderr=None)["stdout"] == ""
