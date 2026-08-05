"""The ✅/❌ convention, and the escape hatch that makes it enforceable.

PROMPTING_CONVENTIONS.md §2 requires ✅ CORRECT and ❌ WRONG examples for
machine-parsed output, and §5 says the opposite for free text:
"constraining the shape of analysis constrains the analysis itself."

check_prompt_conventions cannot tell which is which by reading a prompt.
Left to guess it asked every free-text prompt for examples the doc says
not to add — findings that could never be resolved, which is how a real
signal decays into noise. `output_contract: free_text` lets the prompt
state its own contract; undeclared defaults to parsed, so a new prompt
earns its exemption rather than getting one by omission.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"

# Machine-parsed: a parser reads the result, so examples are mandatory.
PARSED = [
    "ops/judge_task_completion",
    "ops/plan_provision",
    "ops/generate_property_test",
    "ops/check_sanity_plausibility",
    "ops/verify_completion",
]

# Free-text: published as prose, nothing parses it.
FREE_TEXT = [
    "escalate/boss_consult",
    "ops/charter_accomplish",
    "quality_gate/evaluate_ux_session",
    "quality_gate/charter_explore",
]


def _text(tid: str) -> str:
    return (PROMPTS / f"{tid}.yaml").read_text()


@pytest.mark.parametrize("tid", PARSED)
def test_parsed_prompts_show_both_examples(tid):
    text = _text(tid)
    assert "✅" in text or "CORRECT" in text, f"{tid} has no ✅ example"
    assert "❌" in text or "WRONG" in text, f"{tid} has no ❌ example"


@pytest.mark.parametrize("tid", PARSED)
def test_parsed_prompts_do_not_claim_the_exemption(tid):
    assert "output_contract: free_text" not in _text(
        tid
    ), f"{tid} output is parsed — it needs the examples, not the exemption"


@pytest.mark.parametrize("tid", FREE_TEXT)
def test_free_text_prompts_declare_their_contract(tid):
    assert "output_contract: free_text" in _text(tid)


@pytest.mark.parametrize("tid", PARSED + FREE_TEXT)
def test_the_key_does_not_disturb_the_template(tid):
    """It is read by the linter only — no loader or renderer touches it."""
    doc = yaml.safe_load(_text(tid))
    assert doc.get("id") == tid
    assert doc.get("sections"), f"{tid} lost its sections"


@pytest.mark.parametrize("tid", PARSED + FREE_TEXT)
def test_every_touched_prompt_still_renders(tid):
    """Cache-split must still reconstruct the render exactly — these edits
    landed inside cache: true heads, where a mismatch would silently repin
    a stale KV prefix."""
    from agent.loader import PromptRenderer

    renderer = PromptRenderer(PROMPTS)
    namespaces = {"input": {}, "context": {}, "meta": {}}
    full = renderer.render(tid, namespaces)
    assert full.strip()
    static, dynamic = renderer.render_with_cache_split(tid, namespaces)
    assert static + dynamic == full


class TestTheCheckHonoursTheDeclaration:
    def _check(self, tmp_path, body):
        from agent.flow_lint import check_prompt_conventions

        (tmp_path / "t").mkdir(exist_ok=True)
        (tmp_path / "t" / "p.yaml").write_text(body)
        flows = {
            "f": {
                "flow": "f",
                "steps": {
                    "s": {
                        "action": "inference",
                        "prompt_template": {"template": "t/p"},
                    }
                },
            }
        }
        return check_prompt_conventions(flows, tmp_path)

    def test_undeclared_prompts_are_still_asked_for_examples(self):
        """The default must stay strict, or the exemption becomes the rule."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            res = self._check(Path(d), "id: t/p\nsections:\n- id: a\n  content: hi\n")
        assert {r.check for r in res} == {
            "prompt_missing_correct_example",
            "prompt_missing_wrong_example",
        }

    def test_a_declared_prompt_is_exempt(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            res = self._check(
                Path(d),
                "id: t/p\noutput_contract: free_text\nsections:\n- id: a\n  content: hi\n",
            )
        assert res == []
