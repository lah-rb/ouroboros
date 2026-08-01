"""Serial and batch must block on the SAME findings, via the SAME function.

THE INCIDENT (2026-07-31). The two write paths had drifted:

  * BATCH decided in Python — `structural_block_reason`, which honours the
    one-pass contract: a never-reviewed lint failure blocks for exactly one
    decision pass.
  * SERIAL decided in CUE — file_ops' validate resolver routed on `has_issues`,
    which is true for EVERY non-required finding, so a lint failure was
    indistinguishable from anything else and fell through to
    `log_and_report_success`.

Same finding, same kind of file, opposite verdicts. glm-4.7-flash hit it: its
batch collapsed 1-of-11, everything fell to serial fallback, and
`game_engine.py` shipped with `F821 Undefined name 'Parser'` RECORDED in
checks_failed while the write was reported a success. Its goal "Program starts
cleanly and exits without errors" then failed NINE times with the answer in its
own record, and a blind judge scored the artifact TIER 3 — dead on the first
keystroke.

These tests pin the de-fork: one precedence function, both paths consuming it,
and a serial edge that routes a blocking finding to repair.
"""

from __future__ import annotations

import pytest

from agent.actions.reporting_actions import (
    block_reason_from_checks,
    structural_block_reason,
)
from tests.conftest import compiled_flows as _compiled


class TestOnePrecedenceForEveryCaller:
    @pytest.mark.parametrize(
        "failed,expected",
        [
            (["syntax: a.py", "import: a.py", "lint: a.py"], "syntax"),
            (["import: a.py", "lint: a.py"], "import"),
            (["lint: a.py"], "lint"),
            ([], None),
        ],
    )
    def test_tier_precedence(self, failed, expected):
        assert block_reason_from_checks(failed) == expected

    def test_syntax_always_blocks_regardless_of_review_flags(self):
        """Only syntax is unconditional — review flags cannot wave it through."""
        assert (
            block_reason_from_checks(
                ["syntax: a.py"], import_reviewed=True, lint_reviewed=True
            )
            == "syntax"
        )

    @pytest.mark.parametrize("tier", ["import", "lint"])
    def test_one_pass_contract_releases_after_review(self, tier):
        """The flag bounds the LOOP, not the detection: once the question has
        been asked, the finding stops blocking even if still unfixed."""
        failed = [f"{tier}: a.py"]
        assert block_reason_from_checks(failed) == tier
        assert block_reason_from_checks(failed, **{f"{tier}_reviewed": True}) is None

    def test_the_goal_wrapper_delegates_rather_than_reimplements(self):
        """structural_block_reason must not carry a second copy of the ladder."""
        import inspect

        src = inspect.getsource(structural_block_reason)
        assert "block_reason_from_checks" in src
        assert 'startswith("syntax:")' not in src, "precedence duplicated again"


class TestSerialRoutesABlockingFindingToRepair:
    """The edge that did not exist, and whose absence cost the artifact."""

    @staticmethod
    def _rules(step):
        steps = _compiled()["file_ops"]["steps"]
        return [
            (r["condition"], r["transition"]) for r in steps[step]["resolver"]["rules"]
        ]

    def test_a_blocking_non_syntax_finding_reaches_check_retry(self):
        rules = self._rules("run_checks")
        hit = [t for c, t in rules if "block_reason" in c]
        assert hit == ["check_retry"], f"lint/import must route to repair: {rules}"

    def test_it_is_ordered_BEFORE_the_has_issues_success_edge(self):
        """`has_issues` is true for every non-required finding, so a rule after
        it is dead — which is exactly how lint used to reach success."""
        conds = [c for c, _ in self._rules("run_checks")]
        block = next(i for i, c in enumerate(conds) if "block_reason" in c)
        issues = next(i for i, c in enumerate(conds) if "has_issues" in c)
        assert block < issues

    def test_syntax_keeps_its_own_earlier_edges(self):
        """Syntax must not be swallowed by the new rule — it has dedicated
        handling (oversized -> diagnose, else retry) that must still win."""
        conds = [c for c, _ in self._rules("run_checks")]
        first_syntax = next(i for i, c in enumerate(conds) if "syntax_failed" in c)
        block = next(i for i, c in enumerate(conds) if "block_reason" in c)
        assert first_syntax < block

    def test_the_new_rule_excludes_syntax_explicitly(self):
        cond = next(c for c, _ in self._rules("run_checks") if "block_reason" in c)
        assert "syntax" in cond, "must not double-handle syntax"


class TestTheValidationActionPublishesTheReason:
    def test_action_computes_block_reason_from_the_shared_function(self):
        import inspect

        from agent.actions.pipeline_actions import (
            action_run_validation_checks_from_env,
        )

        src = inspect.getsource(action_run_validation_checks_from_env)
        assert "block_reason_from_checks" in src
        assert '"block_reason"' in src
