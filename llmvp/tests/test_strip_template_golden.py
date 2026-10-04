"""A stripped session history must be the template's no-reasoning history.

The prior-turn reasoning strip (strip_prior_reasoning; resident in place,
hybrid replay by turn rollback) keeps each turn's prompt up to the prefilled
think opener and replays the clean answer — reasoning_span's t0 backs over the
opener, the replay prefix is "". That stream is only correct if it IS the
official chat template's rendering of the same conversation with prior
reasoning dropped: qwen3.8 with preserve_thinking=false, qwen3.5/3.6 by their
default (reasoning only after the last user query).

One known, documented boundary difference is normalised away here and pinned
separately: our turn transition is a bare ``<|im_end|>`` where the template
has ``<|im_end|>\\n`` (formats/qwen*.yaml turn_transition). It predates the
strip and wants its own A/B, not a silent fix inside this one.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

DEV = Path(__file__).resolve().parents[2] / "dev"
TURNS = [("first ask", "first answer"), ("second ask", "second answer")]
LAST = "third ask"


def _official(template: str, **kw) -> str:
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()
    env.globals["raise_exception"] = lambda m: (_ for _ in ()).throw(Exception(m))
    messages = []
    for ask, answer in TURNS:
        messages.append({"role": "user", "content": ask})
        messages.append(
            {"role": "assistant", "content": answer, "reasoning_content": "R"}
        )
    messages.append({"role": "user", "content": LAST})
    return env.from_string(template).render(
        messages=messages, add_generation_prompt=True, tools=None, **kw
    )


def _ours(family: str) -> str:
    """The session stream a strip leaves behind, built with the session's own
    renderer calls and reasoning_span's cut (t0 = the opener's start)."""
    from formats.registry import clear_cache, get_renderer

    cfg = SimpleNamespace(
        model=SimpleNamespace(thinking="on", thinking_available=True, family=family)
    )
    with patch("core.config.get_config", return_value=cfg):
        clear_cache()
        r = get_renderer(family)
        t = r.s.thinking
        opener = t.open_tag + ("\n" if t.open_tag_newline else "")
        parts = []
        for i, (ask, answer) in enumerate(TURNS):
            if i:
                parts.append(r.render_turn_transition())
            parts.append(r.render_user(ask))
            gen = r.render_generation_prompt()
            assert gen.endswith(opener), "the strip's cut assumes a prefilled opener"
            parts.append(gen[: -len(opener)] + answer)
        parts.append(r.render_turn_transition())
        parts.append(r.render_user(LAST))
        parts.append(r.render_generation_prompt())
        return "".join(parts)


def _from_first_user(s: str) -> str:
    return s[s.index("<|im_start|>user") :]


def _norm(s: str) -> str:
    return s.replace("<|im_end|>\n", "<|im_end|>")


def _assistant_spans(s: str) -> list[str]:
    return re.findall(r"<\|im_start\|>assistant\n(.*?)<\|im_end\|>", s, flags=re.S)


CASES = [
    ("qwen38", "qwen38_chat_template.jinja", {"preserve_thinking": False}),
    ("qwen", "qwen36_chat_template.jinja", {}),
    ("qwen", "qwen35_chat_template.jinja", {}),
]


@pytest.mark.parametrize("family,template,kw", CASES, ids=[c[1] for c in CASES])
def test_stripped_history_is_the_templates_no_reasoning_history(family, template, kw):
    path = DEV / template
    if not path.is_file():
        pytest.skip(f"{template} not banked")
    official = _from_first_user(_official(path.read_text(), **kw))
    ours = _from_first_user(_ours(family))
    # Every prior assistant turn, byte for byte: role header + answer, no think.
    assert _assistant_spans(ours) == _assistant_spans(official) == [a for _, a in TURNS]
    # The whole stream, modulo the documented transition newline.
    assert _norm(ours) == _norm(official)


def test_the_transition_newline_difference_is_still_the_only_one():
    """Pins finding 8 of the 2026-09-22 plan: if the transition gains its
    newline, drop the normalisation above and this test with it."""
    path = DEV / "qwen38_chat_template.jinja"
    if not path.is_file():
        pytest.skip("qwen38 template not banked")
    official = _from_first_user(_official(path.read_text(), preserve_thinking=False))
    ours = _from_first_user(_ours("qwen38"))
    assert ours != official
    assert "<|im_end|><|im_start|>user" in ours
