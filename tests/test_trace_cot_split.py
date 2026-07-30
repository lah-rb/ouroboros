"""CoT vs agent-turn token split in the trace ledger.

WHY THIS METRIC EXISTS. `generated` alone cannot distinguish a model that
reasoned for 12,000 tokens and answered in 300 from one that wrote 12,000 tokens
of answer. On a fleet of heavy thinkers that is the distinction that decides
whether more wall clock would help: the glm-4.7-flash arm of 2026-07-29 spent
82% of its output on thought, and that had to be derived by hand from server-log
CHARACTER counts after the run was already scored.

WHY COVERAGE IS REPORTED WITH THE RATIO. A non-thinking model and a failed count
are indistinguishable in this field — both are 0. `cot_coverage` says what
fraction of real calls contributed, so a 5% cot_pct measured over 2% of calls
cannot be read as "this model barely thinks".
"""

from __future__ import annotations

from agent.trace import finalize_ledger, fold_event, new_ledger


def _inference(gen: int, reasoning: int, flow: str = "code_core", **kw) -> dict:
    ev = {
        "event_type": "inference_call",
        "flow": flow,
        "wall_ms": 1000.0,
        "generated_tokens": gen,
        "cached_prefix_tokens": 100,
        "fresh_prefill_tokens": 200,
        "reasoning_tokens": reasoning,
        "prefill_ms": 100.0,
        "decode_ms": 800.0,
    }
    ev.update(kw)
    return ev


class TestSplitAggregation:
    def test_reasoning_and_content_sum_to_generated(self):
        led = new_ledger()
        fold_event(led, _inference(gen=1000, reasoning=800))
        fold_event(led, _inference(gen=500, reasoning=100))
        tok = finalize_ledger(led, total_wall_ms=2000.0)["tokens"]
        assert tok["generated"] == 1500
        assert tok["reasoning"] == 900
        assert tok["content"] == 600
        assert tok["reasoning"] + tok["content"] == tok["generated"]

    def test_cot_pct_is_the_thinking_share(self):
        led = new_ledger()
        # The shape that motivated the metric: enormous thought, terse answer.
        fold_event(led, _inference(gen=12909, reasoning=10585))
        tok = finalize_ledger(led, total_wall_ms=1000.0)["tokens"]
        assert tok["cot_pct"] == 82.0

    def test_coverage_reports_how_much_of_the_run_the_ratio_describes(self):
        led = new_ledger()
        fold_event(led, _inference(gen=1000, reasoning=500))   # reported
        fold_event(led, _inference(gen=1000, reasoning=0))     # did not report
        fold_event(led, _inference(gen=1000, reasoning=0))     # did not report
        tok = finalize_ledger(led, total_wall_ms=3000.0)["tokens"]
        assert tok["reasoning_calls"] == 1
        assert tok["real_calls"] == 3
        # 1 of 3 calls contributed — the 16.7% cot_pct must not be read as
        # "this model barely thinks", and the coverage is what says so.
        assert tok["cot_coverage"] == 0.333
        assert tok["cot_pct"] == 16.7

    def test_a_non_thinking_run_reports_zero_not_none(self):
        led = new_ledger()
        fold_event(led, _inference(gen=1000, reasoning=0))
        tok = finalize_ledger(led, total_wall_ms=1000.0)["tokens"]
        assert tok["reasoning"] == 0
        assert tok["content"] == 1000
        assert tok["cot_pct"] == 0.0
        assert tok["cot_coverage"] == 0.0

    def test_no_inferences_at_all_leaves_the_ratio_undefined(self):
        """None, not 0.0 — an empty run has no share to report, and 0.0 would
        read as a measured 'it did not think'."""
        tok = finalize_ledger(new_ledger(), total_wall_ms=1000.0)["tokens"]
        assert tok["cot_pct"] is None
        assert tok["cot_coverage"] is None

    def test_content_never_goes_negative(self):
        """reasoning is tokenized from a detokenized span and generated is an
        exact backend count; they need not agree to the token. Clamped, because
        a negative 'content' in a report is worse than a slightly low one."""
        led = new_ledger()
        fold_event(led, _inference(gen=100, reasoning=140))
        tok = finalize_ledger(led, total_wall_ms=1000.0)["tokens"]
        assert tok["content"] == 0


class TestPerFlowAttribution:
    def test_the_split_is_attributed_per_flow(self):
        """Which flows think most is the actionable form of this metric — a
        coherence critic burning 12k tokens is a different problem from a
        structural build doing it."""
        led = new_ledger()
        fold_event(led, _inference(gen=1000, reasoning=900, flow="diagnose_issue"))
        fold_event(led, _inference(gen=1000, reasoning=100, flow="build_structure"))
        flows = finalize_ledger(led, total_wall_ms=2000.0)["flows"]
        assert flows["diagnose_issue"]["reasoning"] == 900
        assert flows["build_structure"]["reasoning"] == 100
        assert flows["diagnose_issue"]["reasoning_calls"] == 1
