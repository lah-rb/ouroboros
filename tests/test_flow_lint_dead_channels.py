"""The two checks that exist because a dead channel cost us a framework bug.

`design_gate_ground` published `design_gate_feedback` and nothing consumed
it, so every reconcile ran blind to the defect it was invoked to fix. The
linter already had a publish/consume check — it emitted the finding at INFO
with the hedge "may be consumed by parent flow", one of 108 such notes, and
the CLI drops INFO. The signal existed and was unreadable.

These pin the two properties that make it readable now: every LEGITIMATE
consumption path must silence the check (or the noise returns and the
warning gets ignored again), and a genuinely dead key must surface at a
level the CLI actually prints.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.flow_lint import (
    _declared_consumers,
    check_dead_publishes,
    check_prompt_text_in_python,
)

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"


def _flow(steps: dict, returns: dict | None = None) -> dict:
    return {"f": {"flow": "f", "steps": steps, "returns": returns or {}, "input": {}}}


class TestDeadPublishSurfaces:
    def test_a_key_nothing_consumes_is_a_warning(self):
        """WARNING, not INFO — the CLI prints only ERROR/WARNING by default,
        which is exactly how the original finding stayed invisible."""
        res = check_dead_publishes(
            _flow({"a": {"publishes": ["ghost_key_xyz"]}}), AGENT_DIR
        )
        assert [(r.level, r.check) for r in res] == [("WARNING", "dead_publish")]

    def test_the_message_names_the_key(self):
        res = check_dead_publishes(
            _flow({"a": {"publishes": ["ghost_key_xyz"]}}), AGENT_DIR
        )
        assert "ghost_key_xyz" in res[0].message


class TestLegitimateConsumersSilenceIt:
    """Each of these was a reason the old check hedged. If any regresses,
    the noise floor rises and the warning becomes ignorable again."""

    @pytest.mark.parametrize(
        "consumer_step",
        [
            pytest.param({"context": {"required": ["k"]}}, id="context_required"),
            pytest.param({"context": {"optional": ["k"]}}, id="context_optional"),
            pytest.param(
                {"prompt_template": {"context_keys": ["k"]}}, id="prompt_context_key"
            ),
            pytest.param(
                {"resolver": {"rules": [{"condition": "context.k == true"}]}},
                id="resolver_condition",
            ),
            pytest.param(
                {"pre_compute": [{"params": {"x": {"$ref": "context.k"}}}]},
                id="precompute_ref",
            ),
            pytest.param(
                {"input_map": {"x": {"$ref": "context.k"}}}, id="input_map_ref"
            ),
        ],
    )
    def test_declared_consumer_silences_the_check(self, consumer_step):
        res = check_dead_publishes(
            _flow({"a": {"publishes": ["k"]}, "b": consumer_step}), AGENT_DIR
        )
        assert res == [], f"should be silent, got {[r.message for r in res]}"

    def test_a_flow_return_counts_as_consumption(self):
        """The caller consumes it — that is what `returns` means."""
        res = check_dead_publishes(
            _flow({"a": {"publishes": ["k"]}}, returns={"out": {"from": "context.k"}}),
            AGENT_DIR,
        )
        assert res == []

    def test_runtime_machinery_keys_are_not_flagged(self):
        res = check_dead_publishes(
            _flow({"a": {"publishes": ["inference_response", "inference_error"]}}),
            AGENT_DIR,
        )
        assert res == []


class TestPythonOnlyIsItsOwnVerdict:
    """The 'duties have blurred' signal: it works, but the wiring is
    invisible to anyone reading the .cue. Distinct from dead, and quieter.

    The reader must be BOUND TO A STEP IN THIS FLOW. `check_boot_liveness`
    reads `terminal_output`, so a flow containing that step really can
    consume the key.
    """

    def test_python_read_downgrades_to_info(self):
        res = check_dead_publishes(
            _flow(
                {
                    "a": {"publishes": ["terminal_output"]},
                    "b": {"action": "check_boot_liveness"},
                }
            ),
            AGENT_DIR,
        )
        assert [(r.level, r.check) for r in res] == [
            ("INFO", "context_key_python_only")
        ]

    def test_it_names_the_file_so_the_wiring_can_be_found(self):
        res = check_dead_publishes(
            _flow(
                {
                    "a": {"publishes": ["terminal_output"]},
                    "b": {"action": "check_boot_liveness"},
                }
            ),
            AGENT_DIR,
        )
        assert ".py" in res[0].message

    def test_a_reader_in_ANOTHER_flow_does_not_count(self):
        """The scoping fix, pinned. Sub-flows get a fresh accumulator
        (runtime.py:279, :757-775), so a reader elsewhere can never see
        this key — counting it masked real dead publishes, which is how
        file_ops.symbol_menu_options hid behind an action bound to no step.
        """
        res = check_dead_publishes(
            _flow({"a": {"publishes": ["terminal_output"]}}), AGENT_DIR
        )
        assert [(r.level, r.check) for r in res] == [("WARNING", "dead_publish")]


class TestPromptTextInPython:
    def test_a_named_prompt_literal_is_flagged(self, tmp_path):
        (tmp_path / "x_actions.py").write_text(
            'SYSTEM_PROMPT = """' + ("You are a helpful step. " * 20) + '"""\n'
        )
        res = check_prompt_text_in_python(tmp_path)
        assert [r.check for r in res] == ["prompt_text_in_python"]
        assert res[0].level == "WARNING"

    def test_interpolation_syntax_is_flagged_even_unnamed(self, tmp_path):
        """A PLAIN string carrying store placeholders — the shape a prompt
        fragment takes when it is rendered later rather than f-interpolated.
        An f-string is deliberately NOT this: `f"{context.x}"` interpolates a
        real Python variable at runtime and is not a template at all."""
        (tmp_path / "y_actions.py").write_text(
            'x = "Consider {context.terminal_output} and then decide what '
            'to do next, carefully and with attention to detail."\n'
        )
        res = check_prompt_text_in_python(tmp_path)
        assert [r.check for r in res] == ["prompt_text_in_python"]

    def test_docstrings_are_not_prompts(self, tmp_path):
        """Excluded structurally via AST, not by heuristic — a long module
        docstring mentioning {context.x} must not trip the check."""
        (tmp_path / "z_actions.py").write_text(
            '"""Module doc discussing {context.foo} at length. '
            + ("Filler prose. " * 30)
            + '"""\n'
        )
        assert check_prompt_text_in_python(tmp_path) == []

    def test_short_helper_strings_are_not_flagged(self, tmp_path):
        (tmp_path / "w_actions.py").write_text('PROMPT = "Continue."\n')
        assert check_prompt_text_in_python(tmp_path) == []

    def test_the_linter_does_not_flag_itself(self):
        """flow_lint quotes the syntax it detects; excluded by name."""
        res = check_prompt_text_in_python(AGENT_DIR)
        assert not any("flow_lint.py" in r.flow for r in res)


class TestDeclaredConsumersHelper:
    def test_it_reports_the_union_of_every_declared_path(self):
        found = _declared_consumers(
            {
                "returns": {"o": {"from": "context.ret"}},
                "steps": {
                    "a": {"context": {"required": ["req"], "optional": ["opt"]}},
                    "b": {"prompt_template": {"context_keys": ["tpl"]}},
                    "c": {"resolver": {"rules": [{"condition": "context.rule > 0"}]}},
                },
            }
        )
        assert {"ret", "req", "opt", "tpl", "rule"} <= found


class TestConsumptionSpellingsTheScanMissed:
    """Two regex defects that made the check call wired keys dead. Both
    were found by triaging its own output against the tree, and both are
    the kind that quietly erode trust in a warning.
    """

    def test_context_get_in_a_resolver_condition_counts(self):
        """`context.get('k')` is as live a read as `context.k`, but a bare
        `context\\.(\\w+)` scan captures the word `get`. project_ops.all_passed
        linted as dead while its own resolver consumed it
        (project_ops.cue:322)."""
        res = check_dead_publishes(
            _flow(
                {
                    "a": {"publishes": ["all_passed"]},
                    "b": {
                        "resolver": {
                            "rules": [
                                {"condition": "context.get('all_passed') == true"}
                            ]
                        }
                    },
                }
            ),
            AGENT_DIR,
        )
        assert res == [], f"should be silent, got {[r.message for r in res]}"

    def test_a_returns_entry_reaching_into_the_value_counts(self):
        """research_gate exports `context.gate_results.verdict`. Splitting
        on the FIRST dot yielded `gate_results.verdict`, so the root key
        never matched its publisher (research_gate.cue:24-26)."""
        res = check_dead_publishes(
            _flow(
                {"a": {"publishes": ["gate_results"]}},
                returns={"verdict": {"from": "context.gate_results.verdict"}},
            ),
            AGENT_DIR,
        )
        assert res == []

    def test_the_plain_context_dot_form_still_counts(self):
        """Guard against fixing one spelling by breaking the other."""
        res = check_dead_publishes(
            _flow(
                {
                    "a": {"publishes": ["k"]},
                    "b": {"resolver": {"rules": [{"condition": "context.k == true"}]}},
                }
            ),
            AGENT_DIR,
        )
        assert res == []


class TestPersonaIsAPromptName:
    def test_persona_bindings_are_flagged(self, tmp_path):
        """OPERATOR_PERSONA (1,511 chars, md5-keyed as a static prefix like
        every other one) escaped the first sweep purely because `persona`
        was absent from the name hints."""
        (tmp_path / "p_actions.py").write_text(
            'OPERATOR_PERSONA = """' + ("You are the operator. " * 20) + '"""\n'
        )
        res = check_prompt_text_in_python(tmp_path)
        assert [r.check for r in res] == ["prompt_text_in_python"]


class TestRuntimeOwnedTurnsCarryNoResolver:
    """A resolver on a runtime-owned turn step is dead code that reads as
    load-bearing.

    runtime.py:552-556: a turn step with action == "inference" routes from
    `turn.transitions` and NEVER consults `step.resolver`. When the action
    is a wrapper instead, the dispatch inverts and the resolver is the live
    path. add_symbol.generate_new_symbol carried a resolver whose rules
    exactly mirrored its transitions, so it looked correct and never ran —
    the last lint warning in the tree.
    """

    def _compiled(self):
        import json

        return json.loads(
            (
                Path(__file__).resolve().parent.parent / "flows" / "compiled.json"
            ).read_text()
        )

    def test_no_inference_turn_step_declares_a_resolver(self):
        offenders = []
        for flow_name, flow in self._compiled().items():
            if not isinstance(flow, dict):
                continue
            for step_name, step in (flow.get("steps") or {}).items():
                if not isinstance(step, dict) or not step.get("turn"):
                    continue
                if step.get("action") != "inference":
                    continue  # wrapper-driven: the resolver IS the live path
                if (step.get("resolver") or {}).get("rules"):
                    offenders.append(f"{flow_name}.{step_name}")
        assert not offenders, (
            f"dead resolvers on runtime-owned turn steps: {offenders} — "
            f"turn.transitions decides; these rules never execute"
        )

    def test_wrapper_driven_turn_steps_keep_theirs(self):
        """The other half of the rule — deleting these would break routing."""
        compiled = self._compiled()
        for flow_name, step_name in (
            ("patch", "rewrite_symbol"),
            ("patch", "capture_bail_reason"),
        ):
            step = compiled[flow_name]["steps"][step_name]
            assert step["action"] != "inference"
            assert (step.get("resolver") or {}).get(
                "rules"
            ), f"{flow_name}.{step_name} lost the resolver its routing depends on"
