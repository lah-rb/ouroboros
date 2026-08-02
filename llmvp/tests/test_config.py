"""
Unit tests for core/config.py and formats/ modules.
"""

import pytest
from pathlib import Path
from unittest.mock import patch
import yaml
import tempfile

from core.config import (
    ModelConfig,
    PromptConfig,
    KnowledgeConfig,
    Config,
    _read_pointer_file,
    _default_config_path,
    load_config,
    get_config,
    set_config,
)

# ── Config Model Tests ─────────────────────────────────────────────


def test_model_config_creation():
    """Test ModelConfig creation with valid data including family."""
    config_data = {
        "name": "test-model",
        "family": "harmony",
        "path": "/path/to/model.gguf",
        "n_ctx": 3000,
        "n_gpu_layers": -1,
        "seed": -1,
        "verbose": False,
        "flash_attention": True,
        "batch_size": 512,
    }
    model_config = ModelConfig(**config_data)
    assert model_config.name == "test-model"
    assert model_config.family == "harmony"
    assert str(model_config.path) == "/path/to/model.gguf"


def test_model_config_requires_family():
    """Test that ModelConfig requires family field."""
    with pytest.raises(Exception):
        ModelConfig(
            name="test",
            path="/tmp/test.gguf",
            n_ctx=4096,
            n_gpu_layers=-1,
            seed=-1,
            verbose=False,
        )


def test_prompt_config_defaults():
    """Test PromptConfig with new fields."""
    prompt_config = PromptConfig()
    assert prompt_config.persona_file is None
    assert prompt_config.tools_file is None


def test_prompt_config_with_files():
    """Test PromptConfig with persona and tools."""
    prompt_config = PromptConfig(
        persona_file="./knowledge/SOUL.md",
        tools_file="./knowledge/tools.txt",
    )
    assert str(prompt_config.persona_file) == "knowledge/SOUL.md"
    assert str(prompt_config.tools_file) == "knowledge/tools.txt"


def test_knowledge_config_no_source_path():
    """Test that KnowledgeConfig no longer has source_path."""
    config = KnowledgeConfig(tokens_bin="/tmp/tokens.bin", token_limit=32000)
    assert not hasattr(config, "source_path")
    assert str(config.tokens_bin) == "/tmp/tokens.bin"


# ── Config Discovery Tests ─────────────────────────────────────────


def test_read_pointer_file_no_file():
    """Test _read_pointer_file when pointer file doesn't exist."""
    with patch.object(Path, "is_file", return_value=False):
        result = _read_pointer_file()
        assert result is None


def test_read_pointer_file_empty():
    """Test _read_pointer_file with empty content."""
    with (
        patch.object(Path, "is_file", return_value=True),
        patch.object(Path, "read_text", return_value=""),
    ):
        result = _read_pointer_file()
        assert result is None


def test_default_config_path_no_pointer():
    """Test _default_config_path when no pointer file exists."""
    with patch.object(Path, "is_file", return_value=False):
        with pytest.raises(FileNotFoundError, match="No configuration file"):
            _default_config_path()


# ── Integration Tests ──────────────────────────────────────────────


def test_config_integration():
    """Test loading a configuration with the new schema."""
    test_config_data = {
        "app": {"host": "0.0.0.0", "port": 8000, "log_level": "info"},
        "model": {
            "name": "test-model",
            "family": "harmony",
            "path": "/tmp/test.gguf",
            "n_ctx": 3000,
            "n_gpu_layers": -1,
            "seed": -1,
            "verbose": False,
        },
        "prompt": {
            "persona_file": "./knowledge/SOUL.md",
        },
        "generation": {},
        "knowledge": {
            "tokens_bin": "/tmp/tokens.bin",
            "token_limit": 31000,
        },
        "resources": {"cpu_threads": 4, "max_concurrent_requests": 4},
        "logging": {"enabled": False, "directory": "./logs"},
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(test_config_data, f)
        temp_config_path = Path(f.name)

    try:
        loaded_config = load_config(temp_config_path)
        assert isinstance(loaded_config, Config)
        assert loaded_config.model.family == "harmony"
        assert loaded_config.prompt.persona_file is not None

        set_config(loaded_config)
        global_config = get_config()
        assert global_config == loaded_config
    finally:
        temp_config_path.unlink()


# ── Format Schema Tests ────────────────────────────────────────────


def test_format_schema_loading():
    """Test that format schemas load and validate."""
    from formats.registry import load_schema, available_families

    families = available_families()
    assert "harmony" in families
    assert "chatml" in families

    for family in families:
        schema = load_schema(family)
        assert schema.family == family
        assert schema.tokens.msg_open
        assert schema.tokens.gen_stop
        # THE REAL INVARIANT IS THAT TURNS ARE UNAMBIGUOUSLY DELIMITED, and
        # there are two ways to achieve it. Laguna (2026-07-26) closes every
        # role explicitly (<user>...</user>). Hunyuan-3 (2026-07-28) does not
        # close user or system turns AT ALL — its template emits
        # `user_token + content` and lets the NEXT role's opener delimit, so
        # the sequence is unambiguous without a closer anywhere but the
        # assistant turn, which ends on EOS.
        #
        # This was originally "no role closes with nothing", which the laguna
        # comment already justified relaxing once: forcing a generic msg_close
        # would push a fake value into a spec where it could leak into rendered
        # output. Hunyuan is the same argument one step further — inventing a
        # user-turn closer would inject a token the model never saw in
        # training. So the assertion follows the property instead of the shape.
        opener_delimited = all(
            (schema.role_tokens.get(r) or schema.tokens).msg_open
            for r in schema.roles
            if r != "system"  # hunyuan emits the system prompt bare after BOS
        )
        for role in schema.roles:
            rt = schema.role_tokens.get(role)
            closed = schema.tokens.msg_close or (rt and rt.msg_close)
            assert closed or opener_delimited, (
                f"{family}/{role} has no close token and the family does not "
                f"delimit turns by the next role's opener either — a rendered "
                f"conversation would run its turns together"
            )


def test_format_renderer_harmony():
    """Test Harmony format rendering."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("harmony")

    # System + developer split
    output = r.render_system(persona="Test persona", date="2026-01-01")
    assert "<|start|>system<|message|>" in output
    assert "<|start|>developer<|message|>" in output
    # Channel directive comes from harmony.yaml's system_block.channel_directive.
    assert "# Valid channels: analysis, final" in output
    assert "Reasoning: medium" in output
    assert "Test persona" in output

    # User
    assert r.render_user("Hello") == "<|start|>user<|message|>Hello<|end|>"

    # Generation prompt — no channel token
    gen = r.render_generation_prompt()
    assert gen == "<|start|>assistant"

    # Stop tokens — completion mode stops on gen_stop plus fake user turn
    assert r.stop_tokens() == ["<|return|>", "<|start|>user"]
    # Session mode currently matches completion mode (the assistant
    # opener was removed after e75 — see stop_tokens() design note)
    assert r.stop_tokens(mode="session") == ["<|return|>", "<|start|>user"]

    # Delimiter pattern
    assert r.delimiter_pattern() == "<|channel|>final*<|message|>"

    # Turn transition
    assert r.render_turn_transition() == "<|end|>\n"

    # Developer override
    dev = r.render_developer("Reasoning: low")
    assert "<|start|>developer<|message|>Reasoning: low<|end|>" == dev


def test_format_renderer_chatml():
    """Test ChatML format rendering."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("chatml")

    # System with persona (no developer split)
    output = r.render_system(persona="Test persona")
    assert "<|im_start|>system" in output
    assert "Test persona" in output
    assert "developer" not in output

    # User
    assert r.render_user("Hi") == "<|im_start|>user\nHi<|im_end|>"

    # Generation prompt includes <think> tag (no level = legacy flag path)
    gen = r.render_generation_prompt()
    assert "<|im_start|>assistant" in gen
    assert "<think>" in gen

    # Per-level think GATE (Step-3.7 mechanics): chatml declares
    # gate_levels [medium, high] — an explicit low OMITS the prefill for
    # the turn; medium/high keep it; None keeps the config-flag behavior.
    assert "<think>" not in r.render_generation_prompt(reasoning="low")
    assert "<think>" in r.render_generation_prompt(reasoning="medium")
    assert "<think>" in r.render_generation_prompt(reasoning="high")

    # Stop tokens — completion mode stops on im_end plus fake user turn
    assert r.stop_tokens() == ["<|im_end|>", "<|im_start|>user"]
    # Session mode currently matches completion mode (the assistant
    # opener was removed after e75 — see stop_tokens() design note)
    assert r.stop_tokens(mode="session") == ["<|im_end|>", "<|im_start|>user"]

    # Delimiter
    assert r.delimiter_pattern() == "</think>"

    # Developer override is empty for ChatML
    assert r.render_developer("anything") == ""


# ── thinking_mode (reasoning effort) ───────────────────────────────


def _model_cfg(**overrides):
    base = {
        "name": "t",
        "family": "harmony",
        "path": "/tmp/m.gguf",
        "n_ctx": 4096,
        "n_gpu_layers": -1,
        "seed": -1,
        "verbose": False,
    }
    base.update(overrides)
    return ModelConfig(**base)


def test_thinking_mode_validation():
    """thinking_mode accepts none/low/medium/high (+None), normalizes, rejects junk."""
    assert _model_cfg().thinking_mode is None  # default — preserves old behavior
    for level in ("none", "low", "medium", "high"):
        assert _model_cfg(thinking_mode=level).thinking_mode == level
    # case-insensitive + whitespace normalization
    assert _model_cfg(thinking_mode=" High ").thinking_mode == "high"
    with pytest.raises(Exception):
        _model_cfg(thinking_mode="ultra")


def test_harmony_reasoning_driven_by_config():
    """Harmony's inline {reasoning} slot reflects the passed level; default=medium."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("harmony")
    assert "Reasoning: high" in r.render_system(reasoning="high")
    # None falls back to the family reasoning_default ("medium")
    assert "Reasoning: medium" in r.render_system(reasoning=None)


def test_chatml_reasoning_prefix_gated():
    """Step/chatml render a top-of-system 'Reasoning:' prefix only when a level
    is set; generic Qwen (reasoning=None) stays byte-identical to before."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("chatml")

    # IDENTITY mapping as of 2026-07-25 (revised after the blind boss panel):
    # each canonical level renders Step's own dial. The earlier collapse sent
    # medium -> "low", which left the router able to do nothing but open or
    # close the gate — every thinking turn thought at the floor. `low` is
    # inert in practice (its turn has the gate closed anyway) but still
    # renders, and all three MUST stay equal-length so the mid-session head
    # splice remains legal.
    with_level = r.render_system(persona="P", reasoning="medium")
    assert "Reasoning: medium" in with_level
    assert "Reasoning: high" in r.render_system(persona="P", reasoning="high")
    assert "Reasoning: low" in r.render_system(persona="P", reasoning="low")
    # prefix sits at the very top of the system content (before identity)
    assert with_level.index("Reasoning: medium") < with_level.index("helpful assistant")

    # Qwen path: no thinking_mode → no Reasoning line at all
    without = r.render_system(persona="P", reasoning=None)
    assert "Reasoning:" not in without


def test_tekken_reasoning_mode_inert():
    """Binary families (Mistral/tekken) never render a Reasoning line — effort
    is the `thinking` bool, not a level."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("tekken")
    assert "Reasoning:" not in r.render_system(persona="P", reasoning="high")


def test_format_renderer_assistant_history():
    """Test multi-turn assistant history rendering."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("harmony")

    # With thinking — should produce two channel blocks
    history = r.render_assistant_history("Answer", thinking="Reasoning...")
    assert "<|channel|>analysis" in history
    assert "<|channel|>final" in history
    assert "Reasoning..." in history
    assert "Answer" in history

    # Without thinking — single final channel block
    history = r.render_assistant_history("Answer")
    assert "<|channel|>final" in history
    assert "<|channel|>analysis" not in history


# ── per-family reasoning level map (ReasoningSpec) ─────────────────────
#
# The agent-side router always speaks the CANONICAL levels (low/medium/high).
# A family may declare how each maps to rendered text, so bimodal models
# (thinking on/off) become expressible without touching the router or its
# trained artifact. Absent block = identity, which is harmony's behavior.


def test_reasoning_map_absent_is_identity():
    """Families without a `reasoning:` block render the level name verbatim —
    harmony must be byte-identical to its pre-generalization output."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("harmony")
    assert r.s.reasoning.levels == {}
    for level in ("low", "medium", "high"):
        out = r.render_system(persona="P", reasoning=level, date="2026-01-01")
        assert f"Reasoning: {level}" in out


def test_reasoning_map_substitutes_family_text():
    """A declared map replaces the canonical name with the family's text."""
    from formats.schema import FormatSchema
    from formats.renderer import FormatRenderer

    spec = FormatSchema.model_validate(
        {
            "family": "probe",
            "tokens": {
                "msg_open": "<s>",
                "msg_content": "\n",
                "msg_close": "</s>",
                "gen_stop": "</s>",
                "history_close": "</s>",
            },
            "roles": {"system": "system", "user": "user", "assistant": "model"},
            "thinking": {"style": "inline_tags"},
            "system_block": {
                "template": "{reasoning_prefix}{persona}",
                "reasoning_prefix": "[{reasoning}]",
                "reasoning_default": "medium",
            },
            "reasoning": {"levels": {"low": "OFF", "medium": "OFF", "high": "ON"}},
        }
    )
    r = FormatRenderer(spec)
    assert "[ON]" in r.render_system(persona="P", reasoning="high")
    # bimodal collapse: two canonical levels render the SAME text, so the
    # backend dedupes them to one pinned head rather than two.
    assert "[OFF]" in r.render_system(persona="P", reasoning="low")
    assert "[OFF]" in r.render_system(persona="P", reasoning="medium")


def test_reasoning_map_passes_through_unknown_level():
    """An unmapped level falls through unchanged rather than raising —
    a bad/partial map degrades to the old behavior, never to a crash."""
    from formats.schema import FormatSchema
    from formats.renderer import FormatRenderer

    spec = FormatSchema.model_validate(
        {
            "family": "probe2",
            "tokens": {
                "msg_open": "<s>",
                "msg_content": "\n",
                "msg_close": "</s>",
                "gen_stop": "</s>",
                "history_close": "</s>",
            },
            "roles": {"system": "system", "user": "user", "assistant": "model"},
            "thinking": {"style": "inline_tags"},
            "system_block": {
                "template": "{reasoning_prefix}{persona}",
                "reasoning_prefix": "[{reasoning}]",
            },
            "reasoning": {"levels": {"high": "ON"}},
        }
    )
    r = FormatRenderer(spec)
    assert "[ON]" in r.render_system(persona="P", reasoning="high")
    assert "[low]" in r.render_system(persona="P", reasoning="low")


# ── Gemma 4 golden test vs the official chat template ─────────────────
#
# formats/gemma.yaml previously modelled Gemma 3 (<start_of_turn> framing,
# "no system role") while gemma-4-31b.yaml was its only consumer — so Gemma 4
# was served with the wrong framing entirely. This pins our rendering against
# the REAL template (dev/gemma4_chat_template.jinja, pulled from
# google/gemma-4-26B-A4B-it) so it cannot silently drift again.


def test_gemma4_rendering_matches_official_template():
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch

    jinja2 = pytest.importorskip("jinja2")
    tpl_path = (
        Path(__file__).resolve().parents[2] / "dev" / "gemma4_chat_template.jinja"
    )
    if not tpl_path.is_file():
        pytest.skip("official gemma-4 template not banked")

    from formats.registry import get_renderer, clear_cache

    env = jinja2.Environment()
    env.globals["raise_exception"] = lambda m: (_ for _ in ()).throw(Exception(m))
    tpl = env.from_string(tpl_path.read_text())

    messages = [
        {"role": "system", "content": "PERSONA"},
        {"role": "user", "content": "Hello"},
    ]

    def render_ours(reasoning=None):
        clear_cache()
        r = get_renderer("gemma")
        # Production serves thinking: true (gemma-4-31b.yaml) — load-bearing:
        # false would prefill the CLOSED empty thought channel and cancel the
        # <|think|> head. The per-turn on/off toggle lives in the reasoning
        # LEVEL (medium/high -> <|think|>, low -> padding), not this flag.
        cfg = SimpleNamespace(model=SimpleNamespace(thinking=True))
        with patch("core.config.get_config", return_value=cfg):
            kw = {"reasoning": reasoning} if reasoning else {}
            segs = r.render_system_segments(persona="PERSONA", **kw)
            system = "".join(s[0] if isinstance(s, tuple) else str(s) for s in segs)
            # Production (build_full_prompt) forwards the per-request level to
            # the generation prompt too — that is where the gate_levels think
            # gate lives, so the golden render must mirror it.
            return (
                "<bos>"
                + system
                + r.render_user("Hello")
                + r.render_generation_prompt(reasoning=reasoning)
            )

    def render_official(enable_thinking):
        return tpl.render(
            messages=messages,
            bos_token="<bos>",
            add_generation_prompt=True,
            enable_thinking=enable_thinking,
            tools=None,
        )

    # ── Thinking ON — the DEFAULT (reasoning_default medium -> <|think|>).
    # BYTE-EQUAL to the official enable_thinking branch, generation prompt
    # included. The old pinned deviation (prefilling the <|channel>thought
    # opener) claimed behavioral equivalence off a short-prompt probe; two
    # full tier runs (2026-08-03) falsified it — under production prompts
    # both 26B-A4B and 31b answered the off-distribution opener with an
    # immediate <channel|> close on EVERY turn, i.e. silent thinking-OFF.
    # The renderer now emits nothing when enabled (the model opens its own
    # channel), matching the official template exactly.
    expected_on = render_official(True)
    ours_on = render_ours()
    assert ours_on == expected_on, f"\nexpected: {expected_on!r}\nours    : {ours_on!r}"

    # ── Thinking OFF — the router's "low" level. ONE remaining known
    # deviation: the off-state system slot is PADDED ("  \n",
    # token-length-matched to "<|think|>\n") so the mid-session head splice
    # stays legal; official emits nothing there. The generation prompt's
    # pre-closed empty channel now matches the official disabled branch
    # exactly.
    official_off = render_official(False)
    assert official_off.endswith("<|channel>thought\n<channel|>")
    expected_off = official_off.replace("<|turn>system\n", "<|turn>system\n  \n", 1)
    ours_off = render_ours(reasoning="low")
    assert (
        ours_off == expected_off
    ), f"\nexpected: {expected_off!r}\nours    : {ours_off!r}"


def test_gemma4_uses_turn_framing_not_gemma3():
    """Guard against a regression to the Gemma-3 spec."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("gemma")
    assert r.s.tokens.msg_open == "<|turn>"
    assert "start_of_turn" not in r.s.tokens.msg_open
    assert r.s.roles.get("system") == "system"  # Gemma 4 HAS a system role
    assert r.s.traits.fold_system_into_first_user is False


def test_reasoning_prefix_survives_edge_trim():
    """A whitespace-padded reasoning prefix must NOT be eaten by the cosmetic
    edge-trim — otherwise the head-splice's equal-length check silently
    refuses every swap (fail-safe, but invisibly broken)."""
    from formats.schema import FormatSchema
    from formats.renderer import FormatRenderer

    spec = FormatSchema.model_validate(
        {
            "family": "probe3",
            "tokens": {
                "msg_open": "<s>",
                "msg_content": "\n",
                "msg_close": "</s>",
                "gen_stop": "</s>",
                "history_close": "</s>",
            },
            "roles": {"system": "system", "user": "user", "assistant": "model"},
            "thinking": {"style": "inline_tags"},
            "system_block": {
                "template": "{reasoning_prefix}{persona}",
                "reasoning_prefix": "{reasoning}\n",
                "reasoning_default": "medium",
            },
            # bimodal: the off-state is PADDED to keep both heads equal length
            "reasoning": {"levels": {"medium": "  ", "high": "<|think|>"}},
        }
    )
    r = FormatRenderer(spec)
    on = r.render_system(persona="P", reasoning="high")
    off = r.render_system(persona="P", reasoning="medium")
    assert "<|think|>\n" in on
    assert "  \n" in off, f"padding was stripped: {off!r}"


# ── template-supplied BOS (tokens.bos) ────────────────────────────────
#
# Mistral GGUFs set tokenizer.ggml.add_bos_token=false, so llama.cpp does NOT
# prepend BOS — the chat template owns it. Our format layer had no BOS concept
# at all, so every Mistral prompt went out WITHOUT its sequence-start token.
# Live 2026-07-22: Mistral Medium 3.5 answered "What is 2+2?" with
# " the number of a$)bz20)b$n)5 ..." (character salad) BOS-less, and "4" once
# the BOS was restored — same model, same server, same prompt.


def test_tekken_declares_template_bos():
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("tekken")
    assert r.s.tokens.bos == "<s>"
    segs = r.render_system_segments(persona="P")
    rendered = "".join(s[0] if isinstance(s, tuple) else str(s) for s in segs)
    assert rendered.startswith("<s>[SYSTEM_PROMPT]"), rendered[:60]
    # exactly once — a duplicated BOS is its own corruption
    assert rendered.count("<s>") == 1


@pytest.mark.parametrize("family", ["harmony", "chatml", "gemma"])
def test_other_families_emit_no_bos(family):
    """bos is opt-in: families whose tokenizer adds BOS itself must not get a
    duplicate, and their rendering must be unchanged by this feature."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer(family)
    assert r.s.tokens.bos == ""
    segs = r.render_system_segments(persona="P", date="2026-01-01")
    rendered = "".join(s[0] if isinstance(s, tuple) else str(s) for s in segs)
    assert not rendered.startswith("<s>")


def test_template_bos_per_model_override():
    """BOS is per-MODEL, not per-family: the two Mistral builds disagree
    despite identical tokenizer metadata (add_bos_token absent from both,
    same bos_id). Small-4 breaks WITH <s>, Medium-3.5 breaks WITHOUT it."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("tekken")

    def head(emit_bos):
        segs = r.render_system_segments(persona="P", emit_bos=emit_bos)
        return "".join(s[0] if isinstance(s, tuple) else str(s) for s in segs)

    assert head(None).startswith("<s>")  # family default
    assert head(True).startswith("<s>")  # explicit on
    assert not head(False).startswith("<s>")  # explicit off wins
    assert head(False).startswith("[SYSTEM_PROMPT]")


def test_tekken_post_system_emits_valid_reasoning_effort():
    """The official template raises on any reasoning_effort outside
    none|high, so an unmapped/empty level would emit a malformed block."""
    from formats.registry import get_renderer, clear_cache

    clear_cache()
    r = get_renderer("tekken")
    for level, expected in ((None, "none"), ("low", "none"), ("high", "high")):
        segs = r.render_system_segments(persona="P", reasoning=level)
        j = "".join(s[0] if isinstance(s, tuple) else str(s) for s in segs)
        assert f'{{"reasoning_effort": "{expected}"}}' in j, (level, j[-90:])


# ── OLMo 3.1 golden test vs the GGUF-embedded template ────────────────
#
# OLMo is function-calling-trained: its template appends a no-functions
# capability declaration to every system message. Plain chatml omitted it
# (off-distribution system block) — formats/olmo.yaml adds it. Pin our
# rendering against the REAL template (dev/olmo31_chat_template.jinja,
# extracted from the GGUF) so it cannot drift.


def test_olmo_rendering_matches_gguf_template():
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch

    jinja2 = pytest.importorskip("jinja2")
    tpl_path = (
        Path(__file__).resolve().parents[2] / "dev" / "olmo31_chat_template.jinja"
    )
    if not tpl_path.is_file():
        pytest.skip("olmo GGUF template not banked")

    from formats.registry import get_renderer, clear_cache

    env = jinja2.Environment()
    tpl = env.from_string(tpl_path.read_text())
    official = tpl.render(
        messages=[
            {"role": "system", "content": "PERSONA"},
            {"role": "user", "content": "Hello"},
        ],
        add_generation_prompt=True,
        eos_token="<|endoftext|>",
    )

    clear_cache()
    r = get_renderer("olmo")
    cfg = SimpleNamespace(model=SimpleNamespace(thinking=True))
    with patch("core.config.get_config", return_value=cfg):
        segs = r.render_system_segments(persona="PERSONA")
        system = "".join(s[0] if isinstance(s, tuple) else str(s) for s in segs)
        ours = system + r.render_user("Hello") + r.render_generation_prompt()

    # Three pinned deviations (see formats/olmo.yaml header):
    #  1. identity line — our system template prefixes the generic identity
    #     before the persona; official passes system content through as-is.
    #  2. message boundaries — bare <|im_end|> without the trailing newline
    #     (chatml tokenization-boundary rationale).
    #  3. "<think>\n" — the shared inline-tags renderer appends a newline
    #     after the injected opener (production-proven on qwen); official
    #     ends the generation prompt at bare "<think>".
    expected = (
        official.replace(
            "<|im_start|>system\nPERSONA",
            "<|im_start|>system\nYou are a helpful assistant.\nPERSONA",
            1,
        ).replace("<|im_end|>\n", "<|im_end|>")
        + "\n"
    )
    assert ours == expected, f"\nexpected: {expected!r}\nours    : {ours!r}"
    # The load-bearing details, asserted directly so a template rewrite
    # cannot silently drop them:
    assert (
        "You do not currently have access to any functions. <functions></functions>"
        in ours
    )


def test_pool_mode_rejects_high_concurrency():
    """The alternating pool allocates a full KV context per slot; the
    2026-07-24 crash was decode_mode silently defaulting to 'pool' with
    max_concurrent 32 (32 x 16GB contexts). Must fail at load."""
    import pytest as _pytest

    from core.config import load_config
    from pathlib import Path
    import tempfile
    import yaml as _yaml

    base = _yaml.safe_load(
        (Path(__file__).parent.parent / "configs" / "gpt-oss-120b-a5.yaml").read_text()
    )
    base["resources"]["max_concurrent_requests"] = 32
    base["resources"]["decode_mode"] = "pool"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        _yaml.dump(base, f)
        p = Path(f.name)
    try:
        with _pytest.raises(ValueError, match="PER SLOT"):
            load_config(p)
    finally:
        p.unlink()


def test_max_concurrent_requests_is_optional_and_defaults_by_mode():
    """Uncapped by default as of 2026-07-26. Seats were measured nearly free
    (128 streams = ~2.7% of a 393k pool, ladder clean 1->128), so a fixed
    admission cap does no useful work — the real limiter is pool cells. The
    old default of 32 cost ~40% of achievable throughput.

    Pool mode is NOT uncapped: it allocates a full KV context per slot, which
    is how the 2026-07-24 machine crash happened.
    """
    from types import SimpleNamespace

    from core.config import (
        DEFAULT_BATCHED_SEATS,
        DEFAULT_POOL_SLOTS,
        resolve_working_seats,
    )

    batched = SimpleNamespace(max_concurrent_requests=None, decode_mode="batched")
    assert resolve_working_seats(batched) == DEFAULT_BATCHED_SEATS == 128

    pool = SimpleNamespace(max_concurrent_requests=None, decode_mode="pool")
    assert resolve_working_seats(pool) == DEFAULT_POOL_SLOTS == 1

    # an explicit value still pins the width — the machinery is kept
    pinned = SimpleNamespace(max_concurrent_requests=3, decode_mode="batched")
    assert resolve_working_seats(pinned) == 3


def test_laguna_family_renders_its_xml_framing_and_close_only_thinking(monkeypatch):
    """Laguna onboarding (2026-07-26). PREPARED but never rendered against the
    real model — the arch is absent from our b9860 llama.cpp build.

    Three things this pins, each of which burned a prior onboarding:
    - per-role XML framing (<user>...</user>), NOT chatml's <|im_start|>, so
      it is a real family rather than a chatml alias;
    - thinking ON prefills the OPEN tag (template: "<assistant>" + "<think>");
    - thinking OFF prefills the CLOSE TAG ALONE. A THIRD variant — step-3.7
      OMITS the opener, gemma-4 supplies a full empty block, laguna supplies
      close-only — so it cannot be inferred from the other families.

    NOTE: the first version of this test wrapped the thinking-OFF assertions in
    a signature check that silently skipped them, so mutating the close-only
    branch changed nothing. It drives the real config path now.
    """
    from types import SimpleNamespace

    import formats.renderer as rmod
    from formats.registry import clear_cache, get_renderer

    clear_cache()
    r = get_renderer("laguna")

    sys_block = r.render_system(persona="PERSONA")
    assert sys_block.startswith("<system>"), sys_block[:40]
    assert sys_block.endswith("</system>\n")
    assert "<|im_start|>" not in sys_block, "leaked chatml framing"

    def _cfg(thinking: bool):
        return SimpleNamespace(model=SimpleNamespace(thinking=thinking))

    monkeypatch.setattr(rmod, "get_config", lambda: _cfg(True), raising=False)
    import core.config as ccfg

    monkeypatch.setattr(ccfg, "get_config", lambda: _cfg(True))
    on = r.render_generation_prompt()
    assert on.startswith("<assistant>"), on[:40]
    assert "<think>" in on, f"thinking ON must prefill the opener: {on!r}"

    monkeypatch.setattr(ccfg, "get_config", lambda: _cfg(False))
    off = r.render_generation_prompt()
    assert "</think>" in off, f"thinking OFF must prefill the close tag: {off!r}"
    assert "<think>" not in off.replace("</think>", ""), (
        f"laguna must NOT supply an opener when thinking is off (that is the "
        f"gemma form, not this one): {off!r}"
    )


def test_laguna_gets_think_marker_handling_in_the_fsm():
    """The OLMo lesson, restated for the derivation refactor (2026-07-26).

    An unregistered thinking family used to fall to the unknown-family default
    and leave a "think>" residue at the head of extracted CONTENT — a
    SyntaxError as line 1 of a generated file — while capturing zero thinking.
    Registration was a manual two-place edit; it is now DERIVED from
    formats/laguna.yaml, so this asserts the OUTCOME rather than table
    membership: laguna must be treated exactly like chatml.
    """
    from core.fsm_labeller import _shape_for, _structural_cats_for, _ThinkShape

    assert _shape_for("laguna") is _ThinkShape.ANGLE
    assert _structural_cats_for("laguna") == _structural_cats_for("chatml")
