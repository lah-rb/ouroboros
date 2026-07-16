"""Task-judge routing: JSON parse / override / retry / exhaustion default (no network)."""

import os
from unittest import mock

from adapters.tb.task_judge import _parse_judgment, classify_flow_set


def test_parse_judgment_json_only():
    assert _parse_judgment('{"flow_set":"code_core","profile":"repair"}') == ("code_core", "repair")
    # robust to a thinking preamble / fence around the JSON
    assert _parse_judgment(
        'thinking...\n```json\n{"flow_set":"ops","profile":"answer"}\n```') == ("ops", "answer")
    # invalid labels -> None (each field validated against its set)
    assert _parse_judgment('{"flow_set":"bogus","profile":"nope"}') == (None, None)
    # no JSON -> (None, None) so the caller retries (no keyword-substring fallback)
    assert _parse_judgment("I'd route this to code_core") == (None, None)


def test_override_wins_flow_set_llm_still_supplies_profile():
    with mock.patch.dict(os.environ, {"OURO_FLOW_SET": "code_core"}):
        with mock.patch("adapters.tb.task_judge._ask_llm", return_value=("ops", "repair")) as ask:
            fs, profile, method = classify_flow_set("anything", "http://x")
            assert (fs, method) == ("code_core", "override")  # override forces the flow set
            assert profile == "repair"                         # LLM still supplies the profile
            ask.assert_called()


def test_invalid_override_ignored_uses_llm():
    with mock.patch.dict(os.environ, {"OURO_FLOW_SET": "bogus"}):
        with mock.patch("adapters.tb.task_judge._ask_llm", return_value=("code_core", "repair")):
            fs, profile, method = classify_flow_set("write a parser", "http://x")
            assert (fs, profile, method) == ("code_core", "repair", "llm")


def test_retry_then_success():
    # First two attempts fail (None), the third parses -> uses the LLM answer.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OURO_FLOW_SET", None)
        with mock.patch("adapters.tb.task_judge._ask_llm",
                        side_effect=[None, None, ("code_core", "repair")]) as ask:
            fs, profile, method = classify_flow_set("x", "http://x")
            assert (fs, profile, method) == ("code_core", "repair", "llm")
            assert ask.call_count == 3


def test_retry_exhausted_defaults_to_ops_plain():
    # No keyword heuristic: an unreachable LLM, retried to the limit, defaults to (ops, plain).
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OURO_FLOW_SET", None)
        with mock.patch("adapters.tb.task_judge._ask_llm", return_value=None) as ask:
            fs, profile, method = classify_flow_set(
                "Fix the failing tests across the repo", "http://x")
            assert (fs, profile, method) == ("ops", "plain", "default")
            assert ask.call_count == 3  # retried up to _MAX_RETRIES
