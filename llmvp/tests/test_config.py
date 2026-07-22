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
        "resources": {"cpu_threads": 4, "max_concurrent_requests": 8},
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
        assert schema.tokens.msg_close
        assert schema.tokens.gen_stop


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

    # Generation prompt includes <think> tag
    gen = r.render_generation_prompt()
    assert "<|im_start|>assistant" in gen
    assert "<think>" in gen

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

    with_level = r.render_system(persona="P", reasoning="medium")
    assert "Reasoning: medium" in with_level
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
