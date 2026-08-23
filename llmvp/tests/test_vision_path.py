"""The vision path — separation, safety, and inertness.

Three properties matter more than the feature working:

1. The vision instance is NEVER a pool slot. MTMDChatHandler is hard-coded to
   seq_id=0 and calls memory_clear(True) on a prefix mismatch, which clears
   EVERY sequence. On a pooled instance that destroys SEQ_STATIC, the flow
   band, the snapshot band and every pinned reasoning head — and the text path
   keeps running afterwards producing wrong output with no error at all.
2. Vision is INERT until called. A config that declares a projector must not
   change one byte of the text path.
3. Image intake is a SECURITY boundary, not a convenience.
"""

from __future__ import annotations

import base64
import os

import pytest

from inference.vision_handlers import resolve_handler_name
from inference.vision_images import (
    ImageIntakeError,
    resolve_image_part,
    to_data_uri,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"payload" * 20


# ── handler resolution (pure — no llama_cpp import) ───────────────────


@pytest.mark.parametrize(
    "family,expected",
    [
        ("chatml", "Qwen3VLChatHandler"),
        ("qwen", "Qwen3VLChatHandler"),
        ("gemma", "Gemma4ChatHandler"),
        ("step3", "Step3VLChatHandler"),
        ("paddleocr", "PaddleOCRChatHandler"),
        ("muse-glimmer", "GenericMTMDChatHandler"),  # no dedicated handler
        ("", "GenericMTMDChatHandler"),
    ],
)
def test_family_resolves_to_its_handler(family, expected):
    assert resolve_handler_name(family) == expected


def test_explicit_handler_overrides_the_family_default():
    assert resolve_handler_name("chatml", "generic") == "GenericMTMDChatHandler"
    assert resolve_handler_name("muse-glimmer", "qwen3vl") == "Qwen3VLChatHandler"
    # A class name is accepted as-is.
    assert resolve_handler_name("x", "Step3VLChatHandler") == "Step3VLChatHandler"


def test_auto_means_resolve_by_family():
    assert resolve_handler_name("gemma", "auto") == "Gemma4ChatHandler"


# ── image intake: the security boundary ───────────────────────────────


def test_base64_data_uri_round_trips():
    part = {"type": "image_url", "image_url": {"url": to_data_uri(PNG)}}
    assert resolve_image_part(part, [], 10_000) == PNG


def test_path_inside_an_allowed_root_is_read(tmp_path):
    p = tmp_path / "fig.png"
    p.write_bytes(PNG)
    part = {"type": "image_path", "path": str(p)}
    assert resolve_image_part(part, [str(tmp_path)], 10_000) == PNG


def test_paths_are_refused_entirely_when_no_root_is_configured(tmp_path):
    """Empty allowlist must mean DISABLED, not 'allow everything'."""
    p = tmp_path / "fig.png"
    p.write_bytes(PNG)
    with pytest.raises(ImageIntakeError, match="disabled"):
        resolve_image_part({"type": "image_path", "path": str(p)}, [], 10_000)


def test_path_outside_every_root_is_rejected(tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    with pytest.raises(ImageIntakeError, match="outside"):
        resolve_image_part(
            {"type": "image_path", "path": str(outside)}, [str(allowed)], 10_000
        )


def test_dot_dot_traversal_is_rejected(tmp_path):
    """Checked on the RESOLVED path — a string check would pass this."""
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    sneaky = os.path.join(str(allowed), "..", "secret.png")
    with pytest.raises(ImageIntakeError, match="outside"):
        resolve_image_part(
            {"type": "image_path", "path": sneaky}, [str(allowed)], 10_000
        )


def test_symlink_escaping_the_root_is_rejected(tmp_path):
    """The reason resolution happens before the check."""
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    link = allowed / "innocent.png"
    os.symlink(secret, link)
    with pytest.raises(ImageIntakeError, match="outside"):
        resolve_image_part(
            {"type": "image_path", "path": str(link)}, [str(allowed)], 10_000
        )


def test_remote_urls_are_refused_rather_than_fetched():
    """Fetching caller-supplied URLs would be an SSRF primitive."""
    part = {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}
    with pytest.raises(ImageIntakeError, match="not fetched"):
        resolve_image_part(part, [], 10_000)


def test_oversize_is_rejected_before_the_model_sees_it(tmp_path):
    p = tmp_path / "big.png"
    p.write_bytes(PNG * 100)
    with pytest.raises(ImageIntakeError, match="limit"):
        resolve_image_part({"type": "image_path", "path": str(p)}, [str(tmp_path)], 64)


def test_malformed_base64_is_a_clean_error_not_a_crash():
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!!"}}
    with pytest.raises(ImageIntakeError, match="base64"):
        resolve_image_part(part, [], 10_000)


def test_empty_image_is_rejected():
    part = {"type": "image_url", "image_url": {"url": to_data_uri(b"")}}
    with pytest.raises(ImageIntakeError, match="empty"):
        resolve_image_part(part, [], 10_000)


def test_base64_decode_is_strict():
    """Non-strict decoding silently accepts garbage, which would reach mtmd."""
    raw = base64.b64encode(PNG).decode()[:-2] + "@@"
    with pytest.raises(ImageIntakeError):
        resolve_image_part(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{raw}"}},
            [],
            10_000,
        )


# ── the vision instance is not a pool slot ────────────────────────────


def test_vision_instance_is_never_registered_as_a_pool_slot():
    """THE invariant that protects the text path's KV bands.

    If the vision instance ever lands in _all_instances or a pool queue, a
    vision request can be handed a context whose seq 0 is a live fork target —
    and MTMDChatHandler's memory_clear(True) would wipe SEQ_STATIC, the flow
    band, the snapshot band and every pinned reasoning head, silently, while
    the text path kept running and produced wrong output.

    Checked against the SOURCE rather than by constructing an instance,
    because building one needs a real llama_model. A source check is what
    actually catches the regression this guards: someone adding the vision
    instance to the pool the way every other instance is added.
    """
    import inspect

    from inference.backends import llama_cpp_backend as mod

    for name in ("_create_vision_instance", "get_vision_instance"):
        src = inspect.getsource(getattr(mod.LlamaCppBackend, name))
        for forbidden in (
            "_all_instances",
            "_pool_queue",
            "_persona_queues",
            "_idle_queue",
        ):
            assert forbidden not in src, (
                f"{name} touches {forbidden} — the vision instance must never "
                "be poolable; MTMDChatHandler's memory_clear(True) would "
                "destroy the shared KV bands"
            )


def test_vision_instance_is_built_single_sequence():
    """n_seq_max=1 is what makes the handler's hard-coded seq_id=0 SAFE rather
    than dangerous — seq 0 becomes the only sequence it can reach."""
    import inspect

    from inference.backends import llama_cpp_backend as mod

    src = inspect.getsource(mod.LlamaCppBackend._create_vision_instance)
    assert "n_seq_max = 1" in src or "n_seq_max=1" in src


def test_only_generic_handler_receives_chat_format():
    """The family handlers carry a fixed CHAT_FORMAT and raise TypeError on the
    kwarg — passing it unconditionally broke every family handler at
    construction (caught live 2026-08-12)."""
    import inspect

    from inference.backends import llama_cpp_backend as mod

    src = inspect.getsource(mod.LlamaCppBackend._create_vision_instance)
    assert "GenericMTMDChatHandler" in src, "the chat_format guard is gone"
    assert 'handler_kwargs["chat_format"]' in src


# ── vision must be INERT until called ─────────────────────────────────


def test_text_prompt_tokens_are_identical_with_and_without_a_projector():
    """THE golden. Declaring a projector must not move one token of the text
    path.

    The text path renders through formats/*.yaml into list[int]; the vision
    path never touches that machinery. If adding mmproj_path ever changed a
    rendered prompt, every cached static prefix and every probe-verified
    ceiling in the fleet would silently be measuring something else.
    """
    from inference.tokenizer import tokenize_segments
    from formats.registry import get_renderer

    class _Tok:
        """Deterministic stand-in — we are comparing two renderings, not
        exercising a real vocabulary."""

        def tokenize(self, text, add_bos=False, special=False):
            return [len(text), sum(text) % 251]

    from core.config import get_config, set_config
    from tests.conftest import make_config

    def _render_under(cfg) -> list:
        """Render a fixed conversation through the ACTIVE config's renderer."""
        prev = getattr(get_config, "_config", None)
        set_config(cfg)
        try:
            renderer = get_renderer(get_config().model.family)
            segments = renderer.render_system_segments(
                persona="You are a careful engineer.", reasoning="high", tools=""
            ) + renderer.render_user_segments("Describe the KV cache.")
            return tokenize_segments(_Tok(), segments, add_bos=True)
        finally:
            if prev is not None:
                set_config(prev)
            else:
                get_config._config = None

    plain = _render_under(make_config())
    with_vision = _render_under(
        make_config(
            model_extra={
                "mmproj_path": "/nonexistent/mmproj.gguf",
                "vision_handler": "qwen3vl",
                "vision_n_ctx": 4096,
                "vision_image_roots": ["/tmp"],
            }
        )
    )

    assert plain == with_vision, (
        "declaring a projector changed the TEXT path's tokens — every cached "
        "static prefix and probe-verified ceiling in the fleet would silently "
        "be measuring something else"
    )
    assert plain, "the fixture rendered nothing; the comparison proved nothing"


def test_preflight_counts_the_projector_only_when_configured():
    """weights_bytes_total deliberately EXCLUDES a sibling mmproj (its
    docstring warns a directory glob would add ~8GB to single-digit headroom).
    That stays true; the projector is added at the preflight site instead, and
    only when we are actually going to load it."""
    import inspect

    from inference.backends import llama_cpp_backend as mod

    pre = inspect.getsource(mod.LlamaCppBackend._kv_preflight)
    assert "mmproj_path" in pre, "preflight does not account for the projector"
    assert "vision_bytes" in pre

    wt = inspect.getsource(mod.LlamaCppBackend.weights_bytes_total)
    assert "mmproj" in wt, "the exclusion note must survive"
    # The exclusion itself must not have been turned into an inclusion.
    assert "EXCLUDED" in wt or "excluded" in wt


# ── the reasoning dial on the vision path (2026-08-23) ────────────────
#
# run_vision_completion had NO reasoning parameter, so the vision path served
# whatever each family's resting default was and no caller could ask for a
# depth. Wiring it exposed a sharper bug: the mtmd handler builds its prompt
# from the MODEL'S OWN chat template, so the level has to arrive as a SYSTEM
# MESSAGE, and the first implementation rendered that block with persona="".
#
# A stripped block is not a smaller prompt, it is a DIFFERENT one. Given only
# "Reasoning effort is set to low…" plus a bare identity, qwen3.8 emitted a
# STOP TOKEN AS ITS FIRST TOKEN (finish_reason='stop', raw_len=0) on every
# budget from 300 to 4096, while the same model at high — and on the text
# path, where the persona is present — answered normally.


def _pinned_config():
    """Pin thinking=per_request for the dial tests.

    resolve_thinking reads the GLOBAL config, so a sibling test that leaves a
    thinking="off" or thinking_available=False config installed collapses every
    level onto one and these assertions fail only in full-suite order — which
    is exactly how they first failed. The gemma gate tests pin the same way.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    cfg = SimpleNamespace(
        model=SimpleNamespace(
            thinking="per_request", thinking_available=True, thinking_mode=None
        )
    )
    return patch("core.config.get_config", return_value=cfg)


def _vision_system_body(family: str, *, persona: str, reasoning: str) -> str:
    """The system body run_vision_completion injects, by the same rule.

    Segments [0] and [-1] are the ROLE WRAPPER, which the model's own template
    supplies; everything between is the body. Sliced rather than filtered on
    is_framing on purpose — qwen38's effort sentence is marked framing (it
    comes from reasoning_prefix), so a content-only filter drops exactly the
    directive being requested.
    """
    from formats.registry import clear_cache, get_renderer

    with _pinned_config():
        clear_cache()
        segs = get_renderer(family).render_system_segments(
            persona=persona, reasoning=reasoning
        )
    return "".join(t for t, _fr in segs[1:-1]) if len(segs) > 2 else ""


@pytest.mark.parametrize("family", ["muse-glimmer", "qwen38"])
def test_the_injected_block_carries_the_persona_not_just_the_directive(family):
    body = _vision_system_body(family, persona="SENTINEL_PERSONA", reasoning="low")
    assert "SENTINEL_PERSONA" in body, (
        f"{family}: the vision system block dropped the persona — this is the "
        "exact shape that made qwen3.8 stop on its first token"
    )


@pytest.mark.parametrize("family", ["muse-glimmer", "qwen38"])
def test_low_and_high_render_different_bodies(family):
    low = _vision_system_body(family, persona="P", reasoning="low")
    high = _vision_system_body(family, persona="P", reasoning="high")
    assert low != high, f"{family}: the dial is inert — low and high are identical"


def test_the_role_wrapper_is_excluded():
    # The template supplies its own <|im_start|>system / <|start|>system.
    # Leaking ours would double-wrap the message.
    for family, marker in (("muse-glimmer", "<|start|>"), ("qwen38", "<|im_start|>")):
        body = _vision_system_body(family, persona="P", reasoning="low")
        assert marker not in body, f"{family}: role framing leaked into the body"


def test_a_family_with_no_dial_yields_an_empty_body():
    # deepseek4's system template is "{reasoning_prefix}{persona}" with an
    # empty identity, so with no persona there is nothing to inject and the
    # caller must leave the request alone rather than prepend an empty system.
    assert _vision_system_body("deepseek4", persona="", reasoning="low").strip() == ""
