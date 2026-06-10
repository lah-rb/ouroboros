"""Tests for agent/turn_renderer.py.

Step C migration, Phase 3.

Coverage areas:
  - Banner derivation for each response shape, plus override
  - Section source dispatch: ref / template / literal / dynamic template
  - Conditional omission when ref resolves empty, required override
  - Title prepending — default off, enabled via `title`
  - Envelope rendering for json_document with x-example
  - Envelope for unsupported shapes raises (intentional Phase 3 placeholder)
  - Template file loading, caching, malformed-template diagnostics
  - Substitution patterns compatible with PromptRenderer
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agent.models import TurnDefinition
from agent.schema_registry import SchemaRegistry
from agent.turn_renderer import TurnRenderError, TurnRenderer

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    """Create a minimal turn-format template tree for tests."""
    (tmp_path / "personas").mkdir()
    (tmp_path / "personas" / "env_detector.yaml").write_text(dedent("""
            id: personas/env_detector
            content: |
              ---ACT AS---
              You are a project environment detection module.
              ---END---
            """).strip())
    (tmp_path / "set_env").mkdir()
    (tmp_path / "set_env" / "project_scan.yaml").write_text(dedent("""
            id: set_env/project_scan
            content: |
              ## Project files
              Working directory: {input.working_directory}

              {context.project_file_list}
            """).strip())
    (tmp_path / "set_env" / "detect_tooling_rules.yaml").write_text(dedent("""
            id: set_env/detect_tooling_rules
            content: |
              Determine validation commands for each file extension.
            """).strip())
    # Alternate kind-specific templates for the dynamic-ref test
    (tmp_path / "patch").mkdir()
    (tmp_path / "patch" / "rewrite_class_instruction.yaml").write_text(dedent("""
            id: patch/rewrite_class_instruction
            content: |
              Rewrite this class.
            """).strip())
    (tmp_path / "patch" / "rewrite_function_instruction.yaml").write_text(dedent("""
            id: patch/rewrite_function_instruction
            content: |
              Rewrite this function.
            """).strip())
    return tmp_path


@pytest.fixture
def schema_registry() -> SchemaRegistry:
    """A minimal registry with the validation_env_config schema for
    envelope rendering."""
    return SchemaRegistry(
        {
            "validation_env_config": {
                "type": "object",
                "x-example": {
                    "interactive_prompt": "> ",
                    "py": {
                        "syntax": [
                            "python",
                            "-c",
                            "import ast; ast.parse(open('{file}').read())",
                        ],
                    },
                },
            },
            "no_example_schema": {
                "type": "object",
                # deliberately missing x-example
            },
        }
    )


@pytest.fixture
def renderer(prompts_dir: Path, schema_registry: SchemaRegistry) -> TurnRenderer:
    return TurnRenderer(prompts_dir, schema_registry)


def _make_turn(**overrides) -> TurnDefinition:
    """Build a minimal valid json_document turn for tests, with
    overrides for the fields being tested."""
    base = {
        "response_shape": "json_document",
        "sections": [
            {"type": "instruction", "literal": "Do the thing."},
            {"type": "envelope"},
        ],
        "transitions": {"default": "next", "no_answer": "failed"},
        "response": {"schema_id": "validation_env_config"},
    }
    base.update(overrides)
    return TurnDefinition.model_validate(base)


# ──────────────────────────────────────────────────────────────────────
# Banner
# ──────────────────────────────────────────────────────────────────────


def test_banner_for_json_document(renderer: TurnRenderer) -> None:
    turn = _make_turn()
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    assert prompt.startswith("=== JSON DOCUMENT ===\n")


def test_banner_override(renderer: TurnRenderer) -> None:
    turn = _make_turn(mode_banner="=== TEST EVALUATION ===")
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    assert prompt.startswith("=== TEST EVALUATION ===\n")


def test_banner_unknown_shape_raises(renderer: TurnRenderer) -> None:
    """Belt-and-braces — a shape Pydantic never sees. Construct a mock
    turn manually bypassing validation to exercise the error path."""
    # Direct TurnRenderer._banner call with a crafted input
    from unittest.mock import MagicMock

    mock_turn = MagicMock()
    mock_turn.mode_banner = None
    mock_turn.response_shape = "parquet"  # not in _BANNERS
    with pytest.raises(TurnRenderError, match="Unknown response_shape"):
        renderer._banner(mock_turn)


# ──────────────────────────────────────────────────────────────────────
# Content sections — ref
# ──────────────────────────────────────────────────────────────────────


def test_ref_section_resolves_value(renderer: TurnRenderer) -> None:
    turn = _make_turn(
        sections=[
            {"type": "problem", "ref": {"$ref": "input.target_file_path"}},
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {"target_file_path": "models.py"},
            "context": {},
            "meta": {},
        },
    )
    assert "models.py" in prompt


def test_ref_section_omitted_when_empty(renderer: TurnRenderer) -> None:
    """Site #19's conditional problem section — target_file_path is
    optional, section disappears when absent."""
    turn = _make_turn(
        sections=[
            {"type": "role", "template": "personas/env_detector"},
            {
                "type": "problem",
                "ref": {"$ref": "input.target_file_path"},
                "title": "Target",
            },
            {"type": "instruction", "literal": "Always present."},
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={"input": {}, "context": {}, "meta": {}},
    )
    assert "Always present." in prompt
    # Section omitted when ref source absent and not required.
    # Title would only appear if the section rendered — its absence
    # is the omission signal.
    assert "## Target" not in prompt


def test_ref_section_required_renders_even_when_empty(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn(
        sections=[
            {
                "type": "problem",
                "ref": {"$ref": "input.missing"},
                "required": True,
                "title": "Problem",
            },
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={"input": {}, "context": {}, "meta": {}},
    )
    # Required + title → header renders even with empty content
    assert "## Problem" in prompt


def test_ref_section_empty_string_treated_as_empty(
    renderer: TurnRenderer,
) -> None:
    """Empty string is functionally equivalent to missing for omission
    purposes."""
    turn = _make_turn(
        sections=[
            {
                "type": "problem",
                "ref": {"$ref": "input.target_file_path"},
                "title": "Target",
            },
            {"type": "instruction", "literal": "Only instruction."},
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {"target_file_path": ""},
            "context": {},
            "meta": {},
        },
    )
    assert "Only instruction." in prompt
    # If the problem section had rendered, we'd see "## Target".
    assert "## Target" not in prompt


# ──────────────────────────────────────────────────────────────────────
# Content sections — template
# ──────────────────────────────────────────────────────────────────────


def test_template_section_loads_and_substitutes(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn(
        sections=[
            {"type": "evidence", "template": "set_env/project_scan"},
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {"working_directory": "/tmp/project"},
            "context": {"project_file_list": "- models.py\n- main.py"},
            "meta": {},
        },
    )
    assert "## Project files" in prompt
    assert "Working directory: /tmp/project" in prompt
    assert "- models.py" in prompt


def test_template_section_missing_template_raises(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn(
        sections=[
            {"type": "evidence", "template": "does/not/exist"},
            {"type": "envelope"},
        ]
    )
    with pytest.raises(TurnRenderError, match="Turn template not found"):
        renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})


def test_template_cached_across_renders(
    renderer: TurnRenderer, prompts_dir: Path
) -> None:
    """Same template used twice — renderer should hit the cache on the
    second load."""
    turn = _make_turn(
        sections=[
            {"type": "evidence", "template": "set_env/project_scan"},
            {"type": "envelope"},
        ]
    )
    namespaces = {
        "input": {"working_directory": "/a"},
        "context": {"project_file_list": "x"},
        "meta": {},
    }
    prompt1 = renderer.render(turn, namespaces)

    # Mutate the source file on disk — cached render should NOT pick it up
    (prompts_dir / "set_env" / "project_scan.yaml").write_text(
        "id: set_env/project_scan\ncontent: 'CHANGED'"
    )
    prompt2 = renderer.render(turn, namespaces)

    assert prompt1 == prompt2, "Template should have been served from cache"


def test_template_with_sections_key_rejected_with_helpful_message(
    prompts_dir: Path, renderer: TurnRenderer
) -> None:
    """Legacy multi-section format must be rejected with a clear
    diagnostic — mixing formats silently is a foot-gun."""
    (prompts_dir / "legacy.yaml").write_text(dedent("""
            id: legacy
            sections:
              - id: greeting
                content: hello
            """).strip())
    turn = _make_turn(
        sections=[
            {"type": "evidence", "template": "legacy"},
            {"type": "envelope"},
        ]
    )
    with pytest.raises(TurnRenderError, match="legacy `sections:` format"):
        renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})


def test_template_missing_content_field_raises(
    prompts_dir: Path, renderer: TurnRenderer
) -> None:
    (prompts_dir / "bad.yaml").write_text("id: bad\nsomething_else: value\n")
    turn = _make_turn(
        sections=[
            {"type": "evidence", "template": "bad"},
            {"type": "envelope"},
        ]
    )
    with pytest.raises(TurnRenderError, match="missing required `content`"):
        renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})


# ──────────────────────────────────────────────────────────────────────
# Content sections — dynamic template ref
# ──────────────────────────────────────────────────────────────────────


def test_dynamic_template_ref_resolves_class_case(
    renderer: TurnRenderer,
) -> None:
    """Site #10's kind-aware instruction — context var holds the
    template id."""
    turn = _make_turn(
        sections=[
            {
                "type": "instruction",
                "template": {"$ref": "context.kind_instruction_template"},
            },
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {},
            "context": {"kind_instruction_template": "patch/rewrite_class_instruction"},
            "meta": {},
        },
    )
    assert "Rewrite this class." in prompt


def test_dynamic_template_ref_resolves_function_case(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn(
        sections=[
            {
                "type": "instruction",
                "template": {"$ref": "context.kind_instruction_template"},
            },
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {},
            "context": {
                "kind_instruction_template": "patch/rewrite_function_instruction"
            },
            "meta": {},
        },
    )
    assert "Rewrite this function." in prompt


def test_dynamic_template_ref_empty_omits_section(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn(
        sections=[
            {
                "type": "instruction",
                "template": {"$ref": "context.not_set"},
            },
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    # The instruction templates produce "Rewrite this class." or
    # "Rewrite this function." — neither should appear since the
    # dynamic ref resolved to empty and the section was omitted.
    assert "Rewrite this" not in prompt
    # Envelope still present
    assert "```json" in prompt


def test_dynamic_template_ref_empty_required_raises(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn(
        sections=[
            {
                "type": "instruction",
                "template": {"$ref": "context.must_be_set"},
                "required": True,
            },
            {"type": "envelope"},
        ]
    )
    with pytest.raises(TurnRenderError, match="resolved to empty"):
        renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})


# ──────────────────────────────────────────────────────────────────────
# Content sections — literal
# ──────────────────────────────────────────────────────────────────────


def test_literal_section_renders(renderer: TurnRenderer) -> None:
    turn = _make_turn(
        sections=[
            {"type": "instruction", "literal": "What would you do next?"},
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    assert "What would you do next?" in prompt


def test_literal_with_substitution(renderer: TurnRenderer) -> None:
    turn = _make_turn(
        sections=[
            {
                "type": "instruction",
                "literal": "Working directory: {input.working_directory}",
            },
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {"working_directory": "/tmp/proj"},
            "context": {},
            "meta": {},
        },
    )
    assert "Working directory: /tmp/proj" in prompt


def test_literal_substitution_missing_value_empty(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn(
        sections=[
            {"type": "instruction", "literal": "Path: [{input.missing}]"},
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    assert "Path: []" in prompt


def test_literal_passes_json_braces_through(
    renderer: TurnRenderer,
) -> None:
    """JSON-like braces in instruction literals must not be substituted
    — the regex requires a namespace prefix followed by a dot."""
    turn = _make_turn(
        sections=[
            {
                "type": "instruction",
                "literal": 'Emit JSON like: {"choice": "x"}',
            },
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    assert '{"choice": "x"}' in prompt


# ──────────────────────────────────────────────────────────────────────
# Title handling
# ──────────────────────────────────────────────────────────────────────


def test_section_without_title_has_no_header(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn(
        sections=[
            {"type": "instruction", "literal": "bare text"},
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    assert "## " not in prompt.split("=== JSON DOCUMENT ===\n\n")[1].split("Respond")[0]


def test_section_with_title_prepends_header(renderer: TurnRenderer) -> None:
    turn = _make_turn(
        sections=[
            {
                "type": "target_entity",
                "ref": {"$ref": "input.path"},
                "title": "Target file",
            },
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={"input": {"path": "models.py"}, "context": {}, "meta": {}},
    )
    assert "## Target file\nmodels.py" in prompt


def test_section_title_substitutes_namespace_refs(
    renderer: TurnRenderer,
) -> None:
    """Title supports {namespace.path} placeholders — exercised by
    Site #15's `Current File: {input.target_file_path}` pattern."""
    turn = _make_turn(
        sections=[
            {
                "type": "target_entity",
                "ref": {"$ref": "context.file_body"},
                "title": "Current File: {input.target_file_path}",
            },
            {"type": "envelope"},
        ]
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {"target_file_path": "app/main.py"},
            "context": {"file_body": "def hello(): pass\n"},
            "meta": {},
        },
    )
    assert "## Current File: app/main.py" in prompt
    assert "def hello" in prompt


# ──────────────────────────────────────────────────────────────────────
# Envelope — json_document
# ──────────────────────────────────────────────────────────────────────


def test_json_document_envelope_includes_fenced_example(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn()
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    assert "```json" in prompt
    assert '"interactive_prompt": "> "' in prompt
    assert '"py":' in prompt


def test_json_document_envelope_missing_example_raises(
    prompts_dir: Path, schema_registry: SchemaRegistry
) -> None:
    renderer = TurnRenderer(prompts_dir, schema_registry)
    turn = _make_turn(response={"schema_id": "no_example_schema"})
    with pytest.raises(TurnRenderError, match="x-example"):
        renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})


def test_json_document_envelope_unknown_schema_raises(
    renderer: TurnRenderer,
) -> None:
    turn = _make_turn(response={"schema_id": "not_registered"})
    with pytest.raises(Exception, match="not_registered|not in the registry"):
        renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})


# ──────────────────────────────────────────────────────────────────────
# Envelope — other shapes raise (Phase 3 placeholder)
# ──────────────────────────────────────────────────────────────────────


def test_prose_envelope_omitted_cleanly(renderer: TurnRenderer) -> None:
    """Prose shape has no structured envelope — the envelope section is
    omitted from the rendered prompt without leaving trailing
    whitespace. The banner plus SOUL primer carry mode priming."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "prose",
            "sections": [
                {"type": "instruction", "literal": "summarize what you saw"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {},
        }
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    # Banner + instruction only; no envelope fragment
    assert prompt.startswith("=== WRITING ===")
    assert "summarize what you saw" in prompt
    assert "```" not in prompt  # no JSON/code fence
    # No trailing whitespace from an empty envelope section
    assert prompt == prompt.rstrip()
    assert not prompt.endswith("\n\n")


# ──────────────────────────────────────────────────────────────────────
# Code envelope
# ──────────────────────────────────────────────────────────────────────


def test_code_envelope_single_language_python(renderer: TurnRenderer) -> None:
    """Single-language code shape: one fenced example with python tag
    and `# === FILE: path ===` as first line inside."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "code",
            "sections": [
                {"type": "instruction", "literal": "write the code"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "write", "no_answer": "failed"},
            "response": {"language": "python"},
        }
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})

    assert prompt.startswith("=== CODE EDITOR ===")
    # One fence block with the python tag
    assert "```python" in prompt
    # Fence-with-path-comment protocol is shown
    assert "# === FILE:" in prompt
    # Extension matches the mapping
    assert "file.py" in prompt
    # Single-file envelope should not mention multiple fences
    assert "one fenced code block per file" not in prompt


def test_code_envelope_language_mapping(renderer: TurnRenderer) -> None:
    """Known languages get their conventional extension in the example.
    Unknown languages fall back to the language name."""
    for language, expected_ext in [
        ("rust", "rs"),
        ("typescript", "ts"),
        ("go", "go"),
    ]:
        turn = TurnDefinition.model_validate(
            {
                "response_shape": "code",
                "sections": [
                    {"type": "instruction", "literal": "x"},
                    {"type": "envelope"},
                ],
                "transitions": {"default": "w", "no_answer": "f"},
                "response": {"language": language},
            }
        )
        prompt = renderer.render(
            turn, namespaces={"input": {}, "context": {}, "meta": {}}
        )
        assert (
            f"file.{expected_ext}" in prompt
        ), f"Language {language!r}: expected file.{expected_ext} in envelope"


def test_code_envelope_unknown_language_falls_back_to_name(
    renderer: TurnRenderer,
) -> None:
    """An unknown language has no extension mapping — the envelope
    uses the language name itself as a reasonable fallback."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "code",
            "sections": [
                {"type": "instruction", "literal": "x"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "w", "no_answer": "f"},
            "response": {"language": "esoteric-lang"},
        }
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    # Fallback: language name as extension
    assert "file.esoteric-lang" in prompt


def test_code_envelope_json_target_uses_marker_free_envelope(
    renderer: TurnRenderer,
) -> None:
    """A whole-file JSON write gets the dedicated marker-free envelope:
    a ```json fence with NO `# === FILE:` marker (it's a comment, invalid
    in JSON). Detected from the target extension even though response.language
    is the coarse 'python' default the create flow sets."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "code",
            "sections": [
                {"type": "instruction", "literal": "write the save file"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "write", "no_answer": "failed"},
            "response": {"language": "python"},  # coarse default; target wins
        }
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {"target_file_path": "save_data.json"},
            "context": {},
            "meta": {},
        },
    )
    assert "```json" in prompt
    # The example fence is marker-free — no `# === FILE:` line INSIDE it.
    assert "```json\n<complete file content>\n```" in prompt
    # And the code envelope's marker REQUIREMENT is not imposed.
    assert "first line inside the fence must be" not in prompt
    assert "no comments" in prompt.lower()


def test_code_envelope_fence_tag_follows_target_extension(
    renderer: TurnRenderer,
) -> None:
    """The example fence tag + extension follow the ACTUAL target file, not the
    create turn's coarse response.language default. A `.yaml` target renders
    ```yaml / file.yaml even though response.language is 'python' — and the
    `# === FILE:` marker stays (YAML has comments, unlike JSON)."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "code",
            "sections": [
                {"type": "instruction", "literal": "write the world data"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "write", "no_answer": "failed"},
            "response": {"language": "python"},  # coarse default; target wins
        }
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {"target_file_path": "world_data.yaml"},
            "context": {},
            "meta": {},
        },
    )
    assert "```yaml" in prompt
    assert "file.yaml" in prompt
    assert "```python" not in prompt  # the coarse default did NOT leak
    assert "# === FILE:" in prompt  # YAML has comments, so marker stays


def test_code_envelope_no_target_falls_back_to_language(
    renderer: TurnRenderer,
) -> None:
    """With no target_file_path, the envelope falls back to response.language —
    preserving the prior behavior for callers that don't set a target."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "code",
            "sections": [
                {"type": "instruction", "literal": "x"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "w", "no_answer": "f"},
            "response": {"language": "rust"},
        }
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    assert "```rust" in prompt
    assert "file.rs" in prompt


def test_code_envelope_non_json_target_unchanged(renderer: TurnRenderer) -> None:
    """A non-JSON target (even with target_file_path set) flows through the
    unchanged full-file envelope with the `# === FILE:` marker — the existing
    path is byte-for-byte preserved."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "code",
            "sections": [
                {"type": "instruction", "literal": "write the engine"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "write", "no_answer": "failed"},
            "response": {"language": "python"},
        }
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {"target_file_path": "engine.py"},
            "context": {},
            "meta": {},
        },
    )
    assert "# === FILE:" in prompt  # existing protocol preserved
    assert "```python" in prompt


def test_menu_options_resolve_from_context_key(renderer: TurnRenderer) -> None:
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_single",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                "options_from": {
                    "source": "context",
                    "context_key": "dynamic_menu",
                },
            },
        }
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {},
            "context": {
                "dynamic_menu": [
                    {"id": "alpha", "description": "First option"},
                    {"id": "beta", "description": "Second option"},
                ]
            },
            "meta": {},
        },
    )
    assert "- **alpha** — First option" in prompt
    assert "- **beta** — Second option" in prompt


def test_menu_options_embedded_with_stock_merge(renderer: TurnRenderer) -> None:
    """Options declared inline via `response.options` dict plus stock
    options append at the end. Dedup by key is honored."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_single",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                "options": {
                    "proceed": {"key": "proceed", "description": "Continue"},
                    "revise": {"key": "revise", "description": "Rework the plan"},
                },
                "stock": [
                    {"key": "__conclude__", "description": "End the session"},
                ],
            },
        }
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    # Embedded options render first, stock after
    proceed_idx = prompt.find("**proceed**")
    revise_idx = prompt.find("**revise**")
    conclude_idx = prompt.find("**__conclude__**")
    assert proceed_idx < revise_idx < conclude_idx


def test_menu_empty_options_raises(renderer: TurnRenderer) -> None:
    """A menu with no options anywhere is a configuration error — the
    renderer surfaces it immediately rather than emitting an empty list
    that the model can't choose from."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_single",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                "options_from": {
                    "source": "projection",
                    "projection": "empty_menu",
                },
            },
        }
    )
    with pytest.raises(TurnRenderError, match="no options"):
        renderer.render(
            turn,
            namespaces={"input": {"empty_menu": []}, "context": {}, "meta": {}},
        )


def test_menu_projection_missing_raises(renderer: TurnRenderer) -> None:
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_single",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                "options_from": {
                    "source": "projection",
                    "projection": "nowhere",
                },
            },
        }
    )
    with pytest.raises(TurnRenderError, match="not found in namespaces"):
        renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})


def test_menu_compound_arg_shown_in_options_and_envelope(
    renderer: TurnRenderer,
) -> None:
    """menu_compound: each option with an `arg` shows the arg name in
    the rendered list, and the envelope example includes the arg."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_compound",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                "options": {
                    "shell_command": {
                        "key": "shell_command",
                        "description": "Run a shell command",
                        "arg": {
                            "name": "command",
                            "description": "The command to run",
                        },
                    },
                    "send_input": {
                        "key": "send_input",
                        "description": "Send input to session",
                        "arg": {
                            "name": "text",
                            "description": "Input to send",
                        },
                    },
                },
            },
        }
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    # Banner-compound is recognizable
    assert prompt.startswith("=== MENU + ARGUMENT ===")
    # Options list shows each arg name inline
    assert "**shell_command**" in prompt
    assert "(argument: `command`)" in prompt
    assert "**send_input**" in prompt
    assert "(argument: `text`)" in prompt
    # Envelope example has both "choice" and an arg field
    assert '"choice":' in prompt
    # Uses the first option's arg name as the concrete example
    assert "shell_command" in prompt
    assert '"command":' in prompt


def test_menu_stock_option_dedup_by_key(renderer: TurnRenderer) -> None:
    """If a stock option shares a key with an embedded or projection
    option, it's skipped so the model doesn't see duplicates."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_single",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                "options": {
                    "my_option": {
                        "key": "my_option",
                        "description": "Primary slot",
                    },
                },
                "stock": [
                    {"key": "my_option", "description": "Dup — should skip"},
                    {"key": "__conclude__", "description": "End"},
                ],
            },
        }
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    # Primary description wins, stock dup is suppressed
    assert "Primary slot" in prompt
    assert "Dup — should skip" not in prompt
    # Non-duplicate stock option still lands
    assert "**__conclude__**" in prompt


# ──────────────────────────────────────────────────────────────────────
# Integration — full set_env-style turn
# ──────────────────────────────────────────────────────────────────────


def test_full_set_env_turn_renders_expected_structure(
    renderer: TurnRenderer,
) -> None:
    """End-to-end: a turn shaped like Site #19's detect_tooling, with
    all section types set_env needs.

    Verifies the rendered prompt contains every expected piece:
      - =   === banner at the top
      - Persona block (role template)
      - Evidence block with working dir + file list
      - Conditional problem section present (target_file_path given)
      - Instruction block
      - Envelope with fenced JSON example
    """
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "role", "template": "personas/env_detector"},
                {"type": "evidence", "template": "set_env/project_scan"},
                {"type": "problem", "ref": {"$ref": "input.target_file_path"}},
                {
                    "type": "instruction",
                    "template": "set_env/detect_tooling_rules",
                },
                {"type": "envelope"},
            ],
            "transitions": {"default": "persist_env", "no_answer": "failed"},
            "config": {"temperature": "t*0.0"},
            "response": {"schema_id": "validation_env_config"},
        }
    )
    prompt = renderer.render(
        turn,
        namespaces={
            "input": {
                "working_directory": "/tmp/project",
                "target_file_path": "models.py",
            },
            "context": {
                "project_file_list": "- models.py\n- main.py\n- pyproject.toml"
            },
            "meta": {"flow_name": "set_env", "step_id": "detect_tooling"},
        },
    )

    # Structure: banner at top
    assert prompt.startswith("=== JSON DOCUMENT ===")

    # All expected content appears
    assert "---ACT AS---" in prompt  # from role template
    assert "project environment detection module" in prompt
    assert "Working directory: /tmp/project" in prompt  # from evidence template
    assert "- models.py" in prompt
    assert "models.py" in prompt  # from problem ref
    assert "Determine validation commands" in prompt  # from instruction template
    assert "```json" in prompt  # from envelope
    assert '"interactive_prompt": "> "' in prompt  # envelope x-example

    # Sanity: sections separated by blank lines, not crushed together
    assert prompt.count("\n\n") >= 4


# ══════════════════════════════════════════════════════════════════════
# Regression: menu_compound envelope must use stock options' arg names
# when the flow-specific options don't declare one. b6c live-test bug.
# ══════════════════════════════════════════════════════════════════════


def test_menu_compound_envelope_uses_stock_option_arg_name(
    renderer: TurnRenderer,
) -> None:
    """The b6c trace exposed this: pick_action's only flow-specific
    option (examine_another_file) has no arg, but its stock list
    includes __run_command__ with arg.name = "command". Pre-fix,
    _render_menu_envelope only inspected response.options (embedded,
    flow-specific) so the loop found nothing, and the example fell
    through to the placeholder "argument": "value".

    Consequence: the model copied the placeholder and emitted
    {"choice": "__run_command__", "argument": "grep ..."}. The
    runtime's _extract_menu_arg looked up __run_command__'s declared
    arg.name (which is "command") as the key to pull from the
    response JSON, missed "command" because the model wrote
    "argument" instead, returned None, and execute_investigation_tool
    silently no-op'd for 6+ consecutive turns. Every diagnose cycle
    was fundamentally broken.

    Fix: envelope example consults resolve_options (which merges
    stock + flow-specific consistently with what the model sees in
    the Options section).
    """
    from agent.models import MenuOption, OptionArg

    stock_run_cmd = MenuOption(
        key="__run_command__",
        description="Run a shell command in the project directory",
        arg=OptionArg(name="command", description="The command to run"),
    )

    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_compound",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                # Flow-specific option has NO arg (mirrors pick_action's
                # examine_another_file). Pre-fix, this would be the
                # only source checked for an arg example, and the
                # loop would find nothing.
                "options": {
                    "examine_another_file": {
                        "key": "examine_another_file",
                        "description": "Switch to another file",
                    },
                },
                # Stock includes __run_command__ with an arg. The
                # envelope MUST discover this.
                "stock": [stock_run_cmd.model_dump(by_alias=True)],
            },
        }
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})

    # The example's arg key must be the stock option's declared arg name.
    # If the test reads "argument": "value" we have regressed.
    assert '"command":' in prompt, (
        "Envelope must emit the schema's declared arg name "
        '("command" from __run_command__ stock option), not the '
        '"argument" placeholder. Pre-fix, the renderer only inspected '
        "response.options (flow-specific embedded), missed the stock "
        "option's arg, and fell through to a placeholder key name "
        "that models then dutifully copied — leading to the b6c "
        "regression where execute_investigation_tool silently no-op'd."
    )
    assert '"argument":' not in prompt, (
        "Envelope must not leak the placeholder arg name when a real "
        "option with an arg is available (even via stock merge)."
    )
    # And the example's chosen key should be the option that had the arg
    assert '"choice": "__run_command__"' in prompt


def test_menu_compound_envelope_prefers_first_arg_bearing_option(
    renderer: TurnRenderer,
) -> None:
    """When multiple options declare args, use the first in the
    merged order (flow-specific embedded first, then stock).
    """
    from agent.models import MenuOption, OptionArg

    stock_opt = MenuOption(
        key="__stock_action__",
        description="A stock action",
        arg=OptionArg(name="payload", description="Stock arg"),
    )

    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_compound",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                "options": {
                    "first": {
                        "key": "first",
                        "description": "First option",
                        "arg": {"name": "first_arg", "description": "d"},
                    },
                    "second": {
                        "key": "second",
                        "description": "Second option",
                        "arg": {"name": "second_arg", "description": "d"},
                    },
                },
                "stock": [stock_opt.model_dump(by_alias=True)],
            },
        }
    )
    prompt = renderer.render(turn, namespaces={"input": {}, "context": {}, "meta": {}})
    # Flow-specific "first" comes before stock — so its arg is used.
    assert '"first_arg":' in prompt
    assert '"second_arg":' not in prompt
    assert '"payload":' not in prompt


def test_menu_envelopes_emit_one_block_directive(
    renderer: TurnRenderer,
) -> None:
    """The cycle-13 cascade in the a4d run started when the model
    emitted three concatenated JSON objects in a single
    plan_interaction response — json_repair merged them with
    last-key-wins, the empty third object's empty text routed the
    session to close_failure, and the truncated 2-turn transcript
    drove the eval to "insufficient evidence" → false → diagnose
    cascade.

    The SOUL.md JSON hedge is upstream context; this envelope
    reminder is at the response cursor itself, where the model is
    actively composing output. Both menu_single and menu_compound
    envelopes must say "one JSON object" to set cardinality
    expectations right before the model writes.
    """
    # menu_single — no arg, simple choice
    single_turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_single",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                "options": {
                    "alpha": {"key": "alpha", "description": "First"},
                    "beta": {"key": "beta", "description": "Second"},
                },
            },
        }
    )
    single_prompt = renderer.render(
        single_turn, namespaces={"input": {}, "context": {}, "meta": {}}
    )
    assert (
        "Respond with one JSON object:" in single_prompt
    ), "menu_single envelope must emit cardinality directive at cursor"

    # menu_compound — option with an arg
    compound_turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_compound",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {
                "options": {
                    "send_input": {
                        "key": "send_input",
                        "description": "Send text",
                        "arg": {"name": "text", "description": "input text"},
                    },
                },
            },
        }
    )
    compound_prompt = renderer.render(
        compound_turn, namespaces={"input": {}, "context": {}, "meta": {}}
    )
    assert (
        "Respond with one JSON object:" in compound_prompt
    ), "menu_compound envelope must emit cardinality directive at cursor"
