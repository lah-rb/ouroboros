"""Regression tests for the path-reachability lint check.

The check was added post-a85 after diagnose_issue's pick_action
__conclude__ → end_session → compile_diagnosis crashed at runtime with
a missing `hypotheses` context key. Per-step lint (publish_consume_chains)
couldn't catch it because the conclude step DID exist as a publisher in
the flow — just not on that path.

These tests exercise the lint check directly on synthetic compiled-flow
fixtures so we can pin the short-circuit evaluator's behavior without
needing real flow fixtures to reproduce the bug shapes.
"""

from __future__ import annotations

from agent.flow_lint import (
    _COND_FALSE,
    _COND_TRUE,
    _COND_UNKNOWN,
    _evaluate_condition,
    _resolver_possible_targets,
    check_path_reachability,
    check_precompute_context_declared,
)

# ── Condition-evaluator unit tests ───────────────────────────────────


def test_evaluate_condition_true_literal() -> None:
    assert _evaluate_condition("true", frozenset()) == _COND_TRUE


def test_evaluate_condition_context_get_published() -> None:
    assert _evaluate_condition("context.get('x')", frozenset({"x"})) == _COND_TRUE
    assert _evaluate_condition('context.get("x")', frozenset({"x"})) == _COND_TRUE


def test_evaluate_condition_context_get_unpublished() -> None:
    assert _evaluate_condition("context.get('x')", frozenset({"y"})) == _COND_FALSE


def test_evaluate_condition_not_context_get() -> None:
    assert _evaluate_condition("not context.get('x')", frozenset({"x"})) == _COND_FALSE
    assert _evaluate_condition("not context.get('x')", frozenset()) == _COND_TRUE


def test_evaluate_condition_result_ref_is_unknown() -> None:
    """Step-result references can't be evaluated statically — any
    condition involving ``result.X`` (or operators, comparisons,
    boolean composition) must be UNKNOWN so both branches stay
    explorable. Returning TRUE or FALSE here would cause the walker
    to prune real paths or admit impossible ones."""
    assert (
        _evaluate_condition("result.session_started == true", frozenset())
        == _COND_UNKNOWN
    )
    assert (
        _evaluate_condition("len(context.get('x', [])) > 0", frozenset())
        == _COND_UNKNOWN
    )


# ── Short-circuit resolver semantics ─────────────────────────────────


def test_resolver_short_circuits_on_first_true_rule() -> None:
    """When a rule is TRUE, subsequent rules don't fire — so the
    walker should only enumerate this rule's target."""
    resolver = {
        "type": "rule",
        "rules": [
            {"condition": "context.get('x')", "transition": "a"},
            {"condition": "true", "transition": "b"},
        ],
    }
    # x is published → first rule fires → only 'a' is reachable
    assert _resolver_possible_targets(resolver, frozenset({"x"})) == ["a"]


def test_resolver_skips_known_false_rules() -> None:
    """When a rule is FALSE, skip it and evaluate the next rule.
    This is the case that eliminates the a85-style false positives:
    a guard like context.get('X') where X hasn't been published
    provably won't fire, so the target behind that guard is NOT
    reachable on this path."""
    resolver = {
        "type": "rule",
        "rules": [
            {"condition": "context.get('x')", "transition": "a"},
            {"condition": "true", "transition": "b"},
        ],
    }
    # x NOT published → rule 1 FALSE, fall through to rule 2 (TRUE) → only 'b'
    assert _resolver_possible_targets(resolver, frozenset()) == ["b"]


def test_resolver_unknown_rules_explore_both_branches() -> None:
    """UNKNOWN rules might or might not fire at runtime. Both the
    UNKNOWN rule's target AND subsequent rules must be explored —
    otherwise a real runtime path could be incorrectly pruned."""
    resolver = {
        "type": "rule",
        "rules": [
            {"condition": "result.ok == true", "transition": "a"},
            {"condition": "true", "transition": "b"},
        ],
    }
    # Both 'a' (if result.ok) and 'b' (fallback) are reachable.
    assert _resolver_possible_targets(resolver, frozenset()) == ["a", "b"]


# ── End-to-end reachability findings ─────────────────────────────────


def _make_flow(
    flow_name: str, entry: str, steps: dict, inputs: list[str] | None = None
) -> dict:
    """Build a minimal flows dict for check_path_reachability."""
    return {
        flow_name: {
            "flow": flow_name,
            "entry": entry,
            "input": {"required": list(inputs or []), "optional": []},
            "steps": steps,
        }
    }


def test_reachability_detects_missing_publisher_on_path() -> None:
    """Two paths from entry to target: one through a publisher of X,
    one bypassing it. Target requires X. Lint should flag the
    bypass path."""
    flows = _make_flow(
        "f",
        "a",
        {
            "a": {
                "resolver": {
                    "type": "rule",
                    "rules": [
                        {"condition": "result.cond == true", "transition": "b"},
                        {"condition": "true", "transition": "target"},
                    ],
                },
                "publishes": [],
            },
            "b": {
                "publishes": ["X"],
                "resolver": {
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "target"}],
                },
            },
            "target": {
                "context": {"required": ["X"], "optional": []},
                "publishes": [],
            },
        },
    )
    findings = check_path_reachability(flows)
    assert any(
        f.step == "target" and "X" in f.message for f in findings
    ), f"Expected a finding on target's missing X, got: {findings!r}"


def test_reachability_prunes_unreachable_branch_via_context_get() -> None:
    """A branch guarded on context.get('X') where X has not been
    published is unreachable. Paths through that branch should NOT
    be flagged. Mirrors the post-a85 situation pre-fix where the
    lint was flagging runtime-impossible paths."""
    flows = _make_flow(
        "f",
        "a",
        {
            "a": {
                # 'a' does NOT publish 'y' (the guard key)
                "publishes": [],
                "resolver": {
                    "type": "rule",
                    "rules": [
                        # rule 1: guarded on y → FALSE (y not published)
                        {"condition": "context.get('y')", "transition": "bad"},
                        # rule 2: always fires → goes to 'good'
                        {"condition": "true", "transition": "good"},
                    ],
                },
            },
            "bad": {
                # Requires Z; reaching 'bad' without Z would normally
                # be flagged — but the guard makes this branch
                # unreachable, so no finding should appear.
                "context": {"required": ["Z"], "optional": []},
                "publishes": [],
            },
            "good": {"publishes": []},
        },
    )
    findings = check_path_reachability(flows)
    bad_findings = [f for f in findings if f.step == "bad"]
    assert not bad_findings, (
        f"Branch into 'bad' is guarded by context.get('y') and y is "
        f"never published — the walker should have pruned that branch "
        f"and not produced a finding. Got: {bad_findings!r}"
    )


def test_reachability_admits_branch_when_guard_is_satisfied() -> None:
    """Mirror of the above: when the guard IS satisfied, the branch
    IS reachable. Any context gaps on that path must be flagged."""
    flows = _make_flow(
        "f",
        "a",
        {
            "a": {
                # 'a' publishes 'y' (the guard key)
                "publishes": ["y"],
                "resolver": {
                    "type": "rule",
                    "rules": [
                        {"condition": "context.get('y')", "transition": "bad"},
                        {"condition": "true", "transition": "good"},
                    ],
                },
            },
            "bad": {
                "context": {"required": ["Z"], "optional": []},
                "publishes": [],
            },
            "good": {"publishes": []},
        },
    )
    findings = check_path_reachability(flows)
    # 'bad' is now reachable (because y is published), and Z isn't
    # available — must flag.
    assert any(
        f.step == "bad" and "Z" in f.message for f in findings
    ), f"Expected a finding on 'bad' missing Z; got: {findings!r}"


def test_reachability_seeds_flow_inputs_and_ambient_keys() -> None:
    """Flow inputs and ambient keys (session_injections,
    inference_session_id) are seeded into the initial published set.
    A step requiring one of these should not be flagged."""
    flows = _make_flow(
        "f",
        "a",
        {
            "a": {
                "context": {
                    "required": [
                        "mission_id",  # flow input
                        "inference_session_id",  # ambient
                    ],
                    "optional": [],
                },
                "publishes": [],
            },
        },
        inputs=["mission_id"],
    )
    findings = check_path_reachability(flows)
    assert not findings, (
        f"Flow inputs and ambient keys must be treated as available "
        f"from flow entry. Got findings: {findings!r}"
    )


# ── Pre-compute context-ref declaration ──────────────────────────────


def test_precompute_context_ref_must_be_declared() -> None:
    """A pre_compute $ref to context.<key> the step doesn't declare is an
    ERROR: the runtime context filter hides the key, so the ref resolves to
    None and the formatter renders empty. Regression for the ops_task.run_checks
    bug (referenced context.mission without declaring it → empty checks →
    completion gate silently bypassed)."""
    flows = _make_flow(
        "f",
        "a",
        {
            "a": {
                "context": {"required": [], "optional": ["validation_strategy"]},
                "pre_compute": [
                    {
                        "formatter": "format_completion_criteria",
                        "output_key": "validation_strategy",
                        "params": {
                            "source": {
                                "$ref": "context.mission.task_definition.completion_criteria"
                            }
                        },
                    }
                ],
                "publishes": [],
            },
        },
    )
    findings = check_precompute_context_declared(flows)
    assert any(
        f.step == "a" and f.check == "precompute_context_undeclared" for f in findings
    ), f"undeclared context.mission in pre_compute must be flagged. Got: {findings!r}"


def test_precompute_context_ref_declared_is_clean() -> None:
    """When the step declares the context key (or it's ambient), no finding."""
    flows = _make_flow(
        "f",
        "a",
        {
            "a": {
                "context": {"required": ["mission"], "optional": []},
                "pre_compute": [
                    {
                        "formatter": "format_completion_criteria",
                        "output_key": "validation_strategy",
                        "params": {
                            "source": {"$ref": "context.mission.task_definition"}
                        },
                    }
                ],
                "publishes": [],
            },
        },
    )
    assert not check_precompute_context_declared(flows)
