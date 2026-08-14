"""Curator prompt contracts: templates render with placeholders resolved,
retry sections skip when empty. (Salvaged from the retired bake-off harness
tests when the concluded bake-off apparatus was deleted, 2026-07-16.)
"""

from __future__ import annotations

from pathlib import Path

from agent.actions.curation_actions import format_key_registry, update_key_registry
from agent.loader import PromptRenderer

_ROOT = Path(__file__).parent.parent


def _renderer() -> PromptRenderer:
    return PromptRenderer(_ROOT / "prompts")


def test_review_template_renders_clean():
    out = _renderer().render(
        "curator/review_paper", {"input": {}, "context": {}, "meta": {}}
    )
    # Domain-neutral since 2026-08-14: the role used to say
    # "materials-science", which silently became the corpus-fit criterion for
    # every corpus. The subject now arrives from the mission objective.
    assert "sceptical research-data curator" in out
    assert '"verdict": "accept"' in out  # JSON exemplar braces intact
    assert "{context." not in out  # no unresolved placeholders


def test_pack_template_renders_registry_and_skips_empty_feedback():
    registry = {}
    update_key_registry(registry, {"yield_strength_mpa": 759}, "p1")
    ns = {
        "input": {},
        "context": {
            "key_registry_block": format_key_registry(registry),
            "gate_feedback": "",
        },
        "meta": {},
    }
    out = _renderer().render("curator/pack_data", ns)
    assert "yield_strength_mpa" in out
    assert "previous pack attempt FAILED" not in out  # when: skipped

    ns["context"]["gate_feedback"] = "ungrounded: data.creep_rate token 3.1e-7"
    out2 = _renderer().render("curator/pack_data", ns)
    assert "previous pack attempt FAILED" in out2
    assert "3.1e-7" in out2


# ── the corpus has to name itself ─────────────────────────────────────


def test_review_prompt_states_the_corpus_subject():
    """The prompt asks "is this paper about the corpus's subject matter?" — a
    question the model cannot answer unless told what the corpus collects.

    Left blank it substitutes a domain from the system role, and the pilot
    measured exactly that: on a SPECTROSCOPY corpus, three of four denials were
    "not materials science" — a planetary Raman database, a powder-diffraction
    methods review and an IR study of gallstones, each rejected by a word in
    the prompt rather than by the paper.
    """
    from agent.loader import PromptRenderer
    from agent.actions.curation_actions import _prompts_dir

    r = PromptRenderer(_prompts_dir())
    subject = "Curate extracted spectroscopy papers into the open-key dataset"
    rendered = r.render(
        "curator/review_paper",
        {"input": {}, "context": {"corpus_subject": subject}, "meta": {}},
    )
    assert subject in rendered
    assert "THE CORPUS YOU ARE CURATING FOR" in rendered


def test_review_prompt_names_no_domain_of_its_own():
    """A hardcoded field in the system role IS the bug — it silently becomes
    the fit criterion for every corpus."""
    from agent.loader import PromptRenderer
    from agent.actions.curation_actions import _prompts_dir

    r = PromptRenderer(_prompts_dir())
    for template in ("curator/review_paper", "curator/pack_data"):
        rendered = r.render(
            template,
            {
                "input": {},
                "context": {"key_registry_block": "", "gate_feedback": ""},
                "meta": {},
            },
        ).lower()
        assert "materials-science" not in rendered, template
        assert "materials science" not in rendered, template


def test_without_a_subject_fit_is_not_guessed():
    """Degrade to judging content, never to asserting a domain nobody chose."""
    from agent.loader import PromptRenderer
    from agent.actions.curation_actions import _prompts_dir

    r = PromptRenderer(_prompts_dir())
    rendered = r.render(
        "curator/review_paper", {"input": {}, "context": {}, "meta": {}}
    )
    assert "THE CORPUS YOU ARE CURATING FOR" not in rendered
    assert "do NOT guess one" in rendered
