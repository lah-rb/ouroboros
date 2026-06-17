"""Task-judge routing: override / parse / heuristic (no network)."""

import os
from unittest import mock

from tb_adapter.task_judge import _heuristic, _parse, classify_flow_set


def test_parse_prefers_code_core_when_both_present():
    # A thinking preamble that mentions both still resolves to the specific label.
    assert _parse("I'd route this to code_core, not ops.") == "code_core"
    assert _parse("ops") == "ops"
    assert _parse("```\ncode_core\n```") == "code_core"
    assert _parse("neither word here") is None


def test_heuristic_flips_only_on_explicit_multifile_signal():
    # Multi-file scope -> code_core.
    assert _heuristic("The scripts in the pipeline fail; fix them.") == "code_core"
    assert _heuristic("Refactor the auth logic across the modules.") == "code_core"
    assert _heuristic("Make the failing tests pass.") == "code_core"
    assert _heuristic("Debug the repository so the build works.") == "code_core"
    # Single-file authoring/repair now stays on lightweight ops (the key change).
    assert _heuristic("Implement the missing solve() function") == "ops"
    assert _heuristic("Fix the bug in parser.py") == "ops"
    # Operational / non-code tasks stay on ops.
    assert _heuristic("A script won't run. Figure out what's wrong and fix it.") == "ops"
    assert _heuristic("Create an S3 bucket and set it public-read.") == "ops"
    assert _heuristic("Extract archive.tar to /app/solution.txt") == "ops"


def test_override_wins_and_skips_llm():
    with mock.patch.dict(os.environ, {"OURO_FLOW_SET": "code_core"}):
        with mock.patch("tb_adapter.task_judge._ask_llm") as ask:
            fs, profile, method = classify_flow_set("anything", "http://x")
            assert (fs, method) == ("code_core", "override")
            ask.assert_not_called()


def test_invalid_override_ignored_falls_through_to_llm():
    with mock.patch.dict(os.environ, {"OURO_FLOW_SET": "bogus"}):
        with mock.patch(
            "tb_adapter.task_judge._ask_llm", return_value=("code_core", "repair")
        ):
            fs, profile, method = classify_flow_set("write a parser", "http://x")
            assert (fs, profile, method) == ("code_core", "repair", "llm")


def test_llm_failure_falls_back_to_heuristic():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OURO_FLOW_SET", None)
        with mock.patch("tb_adapter.task_judge._ask_llm", return_value=None):
            fs, profile, method = classify_flow_set("Fix the failing tests across the repo", "http://x")
            assert (fs, method) == ("code_core", "heuristic")
            # single-file authoring now falls back to ops
            fs, profile, method = classify_flow_set("Implement the solver function", "http://x")
            assert (fs, method) == ("ops", "heuristic")


def test_heuristic_profile_classifies_kind():
    from tb_adapter.task_judge import _heuristic_profile

    assert _heuristic_profile("Start an nginx server on port 80") == "service"
    assert _heuristic_profile("Compress /app/logs into a tarball") == "invertible"
    assert _heuristic_profile("Fix the broken script so it runs") == "repair"
    assert _heuristic_profile("Write the integer token count to /app/answer.txt") == "answer"
    assert _heuristic_profile("Convert in.csv to out.parquet") == "data_transform"
    assert _heuristic_profile("Create an S3 bucket and set it public-read") == "plain"
