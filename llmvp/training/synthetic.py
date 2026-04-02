"""Synthetic training examples for delimiter detection edge cases.

These examples are derived from actual live failures observed during
Ouroboros agent runs. They cover patterns that the 50-prompt training
suite doesn't exercise — primarily grammar-constrained responses where
the model outputs a valid token then leaks channel markers.

Usage:
    from training.synthetic import SYNTHETIC_EXAMPLES
    # Append to corpus before training:
    for ex in SYNTHETIC_EXAMPLES:
        save_example(ex, corpus_path)

Each example has raw_text that mimics what LLMVP returns BEFORE
delimiter stripping, with the model_family set appropriately.
"""

from __future__ import annotations

from training.corpus import TrainingExample


def _make(raw: str, family: str = "gpt-oss", pid: str = "", category: str = "synthetic_failure") -> TrainingExample:
    return TrainingExample(
        model_family=family,
        model_name=f"{family}-synthetic",
        prompt_id=pid,
        prompt_category=category,
        raw_text=raw,
        generation_meta={"source": "synthetic", "derived_from": "live_failures"},
    )


# ── Grammar-constrained menu responses (B2: delimiter leakage) ────────
# The model picks a valid letter but channel markers leak through.
# These are the most common live failures — the model's grammar
# constraint forces a letter, but special tokens bypass the grammar.

MENU_FAILURES_HARMONY = [
    # Pattern: letter + newlines + channel transition
    _make(
        "a\n\n<|start|>assistant<|channel|>",
        pid="synth-menu-001",
        category="menu_delimiter_leak",
    ),
    _make(
        "b\n\n<|start|>assistant<|channel|>",
        pid="synth-menu-002",
        category="menu_delimiter_leak",
    ),
    _make(
        "c\n\n<|start|>assistant<|channel|>",
        pid="synth-menu-003",
        category="menu_delimiter_leak",
    ),
    _make(
        "d\n\n<|start|>assistant<|channel|>",
        pid="synth-menu-004",
        category="menu_delimiter_leak",
    ),

    # Pattern: letter + end/start + channel (no newlines)
    _make(
        "a<|end|><|start|>assistant<|channel|>",
        pid="synth-menu-005",
        category="menu_delimiter_leak",
    ),
    _make(
        "f<|end|><|start|>assistant<|channel|>",
        pid="synth-menu-006",
        category="menu_delimiter_leak",
    ),
    _make(
        "b<|end|><|start|>assistant<|channel|>",
        pid="synth-menu-007",
        category="menu_delimiter_leak",
    ),

    # Pattern: letter + end + newlines + start (from second challenge run)
    _make(
        "a<|end|>\n\n<|start|>assistant",
        pid="synth-menu-007b",
        category="menu_delimiter_leak",
    ),
    _make(
        "b<|end|>\n\n<|start|>assistant",
        pid="synth-menu-007c",
        category="menu_delimiter_leak",
    ),

    # Pattern: letter + message + start + channel (from second challenge run)
    _make(
        "a<|message|><|start|>assistant<|channel|>",
        pid="synth-menu-007d",
        category="menu_delimiter_leak",
    ),

    # Pattern: pure delimiter leak (no content at all)
    _make(
        "<|end|><|start|>assistant<|channel|>analysis",
        pid="synth-menu-008",
        category="menu_delimiter_leak",
    ),
    _make(
        "<|channel|>analysis...\n<|end|><|start|>",
        pid="synth-menu-009",
        category="menu_delimiter_leak",
    ),
    _make(
        "<|end|>\n\n<|start|>assistant<|channel|>",
        pid="synth-menu-010",
        category="menu_delimiter_leak",
    ),
    _make(
        "<|end|><|start|>assistant<|channel|>final",
        pid="synth-menu-011",
        category="menu_delimiter_leak",
    ),

    # Pattern: letter + end/start + channel + analysis continuation
    _make(
        "a<|end|><|start|>assistant<|channel|>analysis",
        pid="synth-menu-012",
        category="menu_delimiter_leak",
    ),

    # Pattern: full Harmony structure but very short content
    _make(
        "<|channel|>analysis<|message|>Menu pick: a<|end|><|start|>assistant<|channel|>final<|message|>a",
        pid="synth-menu-013",
        category="menu_short_content",
    ),
    _make(
        "<|channel|>analysis<|message|>The answer is b.<|end|><|start|>assistant<|channel|>final<|message|>b",
        pid="synth-menu-014",
        category="menu_short_content",
    ),
    _make(
        "<|channel|>analysis<|message|>Option c is best.<|end|><|start|>assistant<|channel|>final<|message|>c",
        pid="synth-menu-015",
        category="menu_short_content",
    ),
    _make(
        "<|channel|>final<|message|>a",
        pid="synth-menu-016",
        category="menu_short_content",
    ),
    _make(
        "<|channel|>final<|message|>d",
        pid="synth-menu-017",
        category="menu_short_content",
    ),
    _make(
        "<|channel|>final<|message|>f",
        pid="synth-menu-018",
        category="menu_short_content",
    ),

    # Pattern: word fragment leaked as "content"
    _make(
        "assistant",
        pid="synth-menu-019",
        category="menu_delimiter_leak",
    ),
    _make(
        "analysis",
        pid="synth-menu-020",
        category="menu_delimiter_leak",
    ),
]

# ── Empty / near-empty responses ──────────────────────────────────────
# The model generates only delimiter tokens, content extraction
# should return empty string.

EMPTY_RESPONSES = [
    _make(
        "<|end|><|start|>",
        pid="synth-empty-001",
        category="empty_response",
    ),
    _make(
        "<|channel|><|message|>",
        pid="synth-empty-002",
        category="empty_response",
    ),
    _make(
        "\n\n<|start|>assistant<|channel|>",
        pid="synth-empty-003",
        category="empty_response",
    ),
    _make(
        "",
        pid="synth-empty-004",
        category="empty_response",
    ),
]

# ── Constrain marker variations ───────────────────────────────────────
# The <|constrain|> marker appears in grammar-constrained generation.

CONSTRAIN_VARIATIONS = [
    _make(
        '<|channel|>analysis<|message|>JSON output needed.<|end|><|start|>assistant<|channel|>final <|constrain|>json<|message|>{"result": true}',
        pid="synth-constrain-001",
        category="constrain_marker",
    ),
    _make(
        '<|channel|>final <|constrain|>json<|message|>{"key": "value"}',
        pid="synth-constrain-002",
        category="constrain_marker",
    ),
    _make(
        '<|channel|>analysis<|message|>Pick one.<|end|><|start|>assistant<|channel|>final <|constrain|>enum<|message|>b',
        pid="synth-constrain-003",
        category="constrain_marker",
    ),
]

# ── Partial delimiter fragments ───────────────────────────────────────
# Delimiter tokens partially emitted or split across chunks.

PARTIAL_DELIMITERS = [
    _make(
        "a<|start|>",
        pid="synth-partial-001",
        category="partial_delimiter",
    ),
    _make(
        "hello<|end|>",
        pid="synth-partial-002",
        category="partial_delimiter",
    ),
    _make(
        "<|channel|>final<|message|>The answer.\n<|end|>",
        pid="synth-partial-003",
        category="partial_delimiter",
    ),
    _make(
        "result<|channel|>",
        pid="synth-partial-004",
        category="partial_delimiter",
    ),
]

# ── JSON menu responses (B6 fix: no grammar, natural generation) ──────
# With JSON menus the model generates thinking then a JSON choice.
# The CRF must extract only the JSON, not the thinking or channel markers.

JSON_MENU_RESPONSES = [
    # Clean JSON response after channel transition
    _make(
        '<|channel|>analysis<|message|>We need to pick the best action.\n'
        'Looking at the options, file_ops is most appropriate.\n'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        '{"choice": "file_ops"}',
        pid="synth-json-menu-001",
        category="json_menu_response",
    ),
    # JSON response with analysis... prefix leaked
    _make(
        '<|channel|>analysis<|message|>Let me analyze the situation.\n'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        'analysis…\n\n{\n  "choice": "diagnose_issue"\n}',
        pid="synth-json-menu-002",
        category="json_menu_response",
    ),
    # Short JSON, minimal thinking
    _make(
        '<|channel|>final<|message|>{"choice": "interact"}',
        pid="synth-json-menu-003",
        category="json_menu_response",
    ),
    # JSON with truncated closing brace (max_tokens hit)
    _make(
        '<|channel|>analysis<|message|>The best choice here is design_and_plan because...\n'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        '{"choice": "design_and_plan',
        pid="synth-json-menu-004",
        category="json_menu_response",
    ),
    # Quality completion choice
    _make(
        '<|channel|>analysis<|message|>All tasks done, run final gate.\n'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        '{\n  "choice": "quality_completion"\n}',
        pid="synth-json-menu-005",
        category="json_menu_response",
    ),
]

# ── Content boundary leaks (from 2026-04-01 challenge run) ────────────
# Patterns where channel markers or role words leak into the content
# phase. These are the CRF's primary failure cases observed in live runs.

CONTENT_BOUNDARY_LEAKS = [
    # Pattern 1: "assistant" word + <|channel|>final leaked before real content
    # Expected: "assistant<|channel|>final\n" → all D, "**Analysis**..." → C
    _make(
        '<|channel|>analysis<|message|>Thinking about goals and status.\n'
        '<|end|><|start|>assistant<|channel|>final\n'
        '**Analysis**  \n1. The current goals are not visible in this turn.\n'
        '2. No file operations were executed.\n'
        '3. The next step should be design_and_plan.\n',
        pid="synth-boundary-001",
        category="content_boundary_leak",
    ),

    # Pattern 2: Multiple channel transitions interleaved with ellipsis
    # The entire raw output is structural — content should be empty or minimal
    _make(
        '…\n\n<|end|><|start|>assistant<|channel|>final...\n\n'
        '<|end|><|start|>assistant<|channel|>analysis…\n\n'
        '<|end|><|start|>assistant<|channel|>final…\n\n'
        '<|end|><|start|>User…\n\n'
        '<|end|><|start|>assistant<|channel|>analysis…\n\n'
        ' ... Provide 3-5 sentences of concise analysis ending with a recommendation.\n'
        'So we need to analyze current state.',
        pid="synth-boundary-002",
        category="content_boundary_leak",
    ),

    # Pattern 3: Short delimiter fragment as entire response
    _make(
        '<|channel|>... ......',
        pid="synth-boundary-003",
        category="content_boundary_leak",
    ),

    # Pattern 4: "assistant" leaked at content start (no <|message|> before content)
    _make(
        '<|end|><|start|>assistant<|channel|>final\n'
        'The project needs file_ops to create the initial structure.',
        pid="synth-boundary-004",
        category="content_boundary_leak",
    ),

    # Pattern 5: Channel transition with no final <|message|> — content follows
    # directly after "final\n"
    _make(
        '<|channel|>analysis<|message|>We should set up the environment first.\n'
        '<|end|><|start|>assistant<|channel|>final\n'
        '{"choice": "project_ops"}',
        pid="synth-boundary-005",
        category="content_boundary_leak",
    ),

    # Pattern 6: Role word "assistant" between markers treated as content
    _make(
        '<|end|>\nassistant<|channel|>final<|message|>The answer is here.',
        pid="synth-boundary-006",
        category="content_boundary_leak",
    ),

    # Pattern 7: Repeated channel cycling — model confused, cycling through channels
    _make(
        '<|channel|>analysis<|message|>first thought\n'
        '<|end|><|start|>assistant<|channel|>final<|message|>answer one\n'
        '<|end|><|start|>assistant<|channel|>analysis<|message|>second thought\n'
        '<|end|><|start|>assistant<|channel|>final<|message|>answer two',
        pid="synth-boundary-007",
        category="content_boundary_leak",
    ),

    # Pattern 8: Missing <|message|> after final — content starts with newline
    _make(
        '<|channel|>analysis<|message|>Reasoning about next action.\n'
        '<|end|><|start|>assistant<|channel|>final\n'
        'Based on the analysis, we should proceed with file_ops.\n'
        'The target file is main.py.',
        pid="synth-boundary-008",
        category="content_boundary_leak",
    ),
]

# ── All synthetic examples combined ───────────────────────────────────

SYNTHETIC_EXAMPLES: list[TrainingExample] = (
    MENU_FAILURES_HARMONY
    + EMPTY_RESPONSES
    + CONSTRAIN_VARIATIONS
    + PARTIAL_DELIMITERS
    + JSON_MENU_RESPONSES
    + CONTENT_BOUNDARY_LEAKS
)
