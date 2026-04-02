"""Curated training examples for CRF delimiter detection.

Each example provides a raw model output and explicit content boundaries.
Labels are derived mechanically from the boundaries — no heuristic guessing.

Format of each example dict:
    raw_text:      The full model output as received from llama.cpp
    content_start: Character offset where actual content begins (inclusive)
    content_end:   Character offset where content ends (exclusive)
    source:        Origin tag (e.g. "synthetic", "challenge-run-5", "manual")
    notes:         Optional description of what makes this example useful
    family:        Model family ("harmony", "chatml", etc.)

The label derivation:
    - Atoms with offset < content_start → D (delimiter) or T (thinking)
    - Atoms with content_start <= offset < content_end → C (content)
    - Atoms with offset >= content_end → D (delimiter) or E (terminal)

The T (thinking) vs D (delimiter) distinction within the pre-content zone
is refined by the atom category: structural markers (APO, APC, M_*) are D,
while regular words/text between the analysis channel markers are T.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from training.featurizer import featurize, Atom, ObsCategory


@dataclass
class CuratedExample:
    """A single curated training example."""
    raw_text: str
    content_start: int      # char offset, inclusive
    content_end: int        # char offset, exclusive
    source: str = "manual"
    notes: str = ""
    family: str = "harmony"


# Atom categories that are always structural (delimiter phase)
_STRUCTURAL_CATEGORIES = {
    ObsCategory.ANGLE_PIPE_OPEN,
    ObsCategory.ANGLE_PIPE_CLOSE,
    ObsCategory.ANGLE_OPEN,
    ObsCategory.ANGLE_CLOSE,
    ObsCategory.BRACKET_OPEN,
    ObsCategory.BRACKET_CLOSE,
    ObsCategory.MARKER_CHANNEL,
    ObsCategory.MARKER_START,
    ObsCategory.MARKER_END,
    ObsCategory.MARKER_MESSAGE,
    ObsCategory.MARKER_RETURN,
    ObsCategory.MARKER_CALL,
    ObsCategory.MARKER_CONSTRAIN,
    ObsCategory.MARKER_THINK,
    ObsCategory.MARKER_IM_START,
    ObsCategory.MARKER_IM_END,
    ObsCategory.MARKER_INST,
    ObsCategory.MARKER_END_TAG,
    ObsCategory.CHAN_ANALYSIS,
    ObsCategory.CHAN_FINAL,
    ObsCategory.CHAN_TOOL,
    ObsCategory.EOS,
}


def labels_from_boundaries(
    atoms: list[Atom],
    content_start: int,
    content_end: int,
) -> list[str]:
    """Derive per-atom labels from content boundaries.

    Args:
        atoms: Featurized atom sequence.
        content_start: Character offset where content begins.
        content_end: Character offset where content ends.

    Returns:
        List of label strings, one per atom: "D", "T", "C", or "E".
    """
    labels: list[str] = []
    for atom in atoms:
        if atom.category == ObsCategory.EOS:
            labels.append("E")
            continue

        atom_end = atom.offset + atom.length

        # Content zone
        if atom.offset >= content_start and atom_end <= content_end:
            labels.append("C")
        # Pre-content zone
        elif atom_end <= content_start:
            if atom.category in _STRUCTURAL_CATEGORIES:
                labels.append("D")
            else:
                labels.append("T")  # Thinking text
        # Post-content zone
        else:
            labels.append("D")

    return labels


def curated_to_training_pairs(
    example: CuratedExample,
) -> tuple[list[Atom], list[str]]:
    """Convert a curated example to (atoms, labels) for CRF training.

    Returns:
        Tuple of (atom_list, label_list) ready for the CRF trainer.
    """
    atoms = featurize(example.raw_text)
    labels = labels_from_boundaries(atoms, example.content_start, example.content_end)
    assert len(atoms) == len(labels), (
        f"Atom/label mismatch: {len(atoms)} atoms vs {len(labels)} labels"
    )
    return atoms, labels


# ── Curated example set ────────────────────────────────────────────
#
# Migrated from synthetic.py with explicit content boundaries.
# Each example's content_start/content_end was determined by finding
# the exact position of the content within the raw_text.

CURATED_EXAMPLES: list[CuratedExample] = []


def _add(raw: str, content: str, source: str = "synthetic-migrated",
         notes: str = "", family: str = "harmony") -> None:
    """Helper to add examples by specifying the expected content string."""
    idx = raw.find(content)
    if idx == -1:
        # Content not found — might be empty response
        CURATED_EXAMPLES.append(CuratedExample(
            raw_text=raw,
            content_start=len(raw),
            content_end=len(raw),
            source=source,
            notes=notes + " [empty content]",
            family=family,
        ))
        return
    CURATED_EXAMPLES.append(CuratedExample(
        raw_text=raw,
        content_start=idx,
        content_end=idx + len(content),
        source=source,
        notes=notes,
        family=family,
    ))


# ── Harmony: basic analysis → final ───────────────────────────────

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'The user wants a greeting.'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        'Hello! How can I help you today?'
        '<|end|>'
    ),
    content='Hello! How can I help you today?',
    notes="Basic analysis → final, clean content",
)

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'Let me think about the math problem step by step.\n'
        '2 + 2 = 4\nThe answer is 4.'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        'The answer is 4.'
        '<|end|>'
    ),
    content='The answer is 4.',
    notes="Multi-line thinking, short content",
)

# ── Harmony: code with return statements ──────────────────────────

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'I need to write a function that returns a list.'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        'def list_slugs(self) -> list:\n'
        '    """Return a list of all slugs."""\n'
        '    return [card.slug for card in self._cards]\n'
        '<|end|>'
    ),
    content=(
        'def list_slugs(self) -> list:\n'
        '    """Return a list of all slugs."""\n'
        '    return [card.slug for card in self._cards]\n'
    ),
    notes="Python code with return keyword — critical regression test",
)

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'Writing the engine module.'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        '```python\n'
        'class Engine:\n'
        '    def get_card(self, slug: str):\n'
        '        return self._cards.get(slug)\n'
        '\n'
        '    def list_all(self):\n'
        '        return list(self._cards.values())\n'
        '```\n'
        '<|end|>'
    ),
    content=(
        '```python\n'
        'class Engine:\n'
        '    def get_card(self, slug: str):\n'
        '        return self._cards.get(slug)\n'
        '\n'
        '    def list_all(self):\n'
        '        return list(self._cards.values())\n'
        '```\n'
    ),
    notes="Code block with multiple return statements",
)

_add(
    raw=(
        '<|start|>assistant<|channel|>final<|message|>'
        'The return value of the function is None when no match is found.'
        '<|end|>'
    ),
    content='The return value of the function is None when no match is found.',
    notes="Word 'return' in prose, no analysis channel",
)

# ── Harmony: JSON menu responses ──────────────────────────────────

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'Looking at the options, file_ops is the best choice.'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        '{"choice": "file_ops"}'
        '<|end|>'
    ),
    content='{"choice": "file_ops"}',
    notes="JSON menu response",
)

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'Multiple symbols need editing.'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        '{"choices": ["GameEngine.load", "GameEngine.save"]}'
        '<|end|>'
    ),
    content='{"choices": ["GameEngine.load", "GameEngine.save"]}',
    notes="JSON multi-select menu",
)

# ── Harmony: constrain token ──────────────────────────────────────

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'Responding with JSON.'
        '<|end|><|start|>assistant<|channel|>final <|constrain|>json<|message|>'
        '{"result": "success"}'
        '<|end|>'
    ),
    content='{"result": "success"}',
    notes="Final channel with constrain token",
)

# ── Harmony: empty responses ──────────────────────────────────────

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'Hmm, not sure what to say.'
        '<|end|>'
    ),
    content='',
    notes="Analysis only, no final channel — empty content",
)

_add(
    raw='<|end|>',
    content='',
    notes="Bare end token — 0-token generation",
)

# ── Harmony: content with words that match marker names ───────────

_add(
    raw=(
        '<|start|>assistant<|channel|>final<|message|>'
        'The function should start by loading the channel data, '
        'then return the end result to the message queue.'
        '<|end|>'
    ),
    content=(
        'The function should start by loading the channel data, '
        'then return the end result to the message queue.'
    ),
    notes="Content with words: start, channel, return, end, message — all should be C",
)

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'The analysis is complete.'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        'Call the function at the start of the program. '
        'The final step is to return the analysis results.'
        '<|end|>'
    ),
    content=(
        'Call the function at the start of the program. '
        'The final step is to return the analysis results.'
    ),
    notes="Content uses: call, start, final, return, analysis — all marker-name words",
)

# ── ChatML: inline think tags ─────────────────────────────────────

_add(
    raw=(
        '<|im_start|>assistant\n'
        '<think>\nLet me work through this.\n2+2=4\n</think>\n'
        'The answer is 4.\n'
        '<|im_end|>'
    ),
    content='The answer is 4.\n',
    notes="ChatML with think tags",
    family="chatml",
)

_add(
    raw=(
        '<|im_start|>assistant\n'
        '<think>\nThinking about code.\n</think>\n'
        'def foo():\n    return 42\n'
        '<|im_end|>'
    ),
    content='def foo():\n    return 42\n',
    notes="ChatML code with return keyword",
    family="chatml",
)

# ── Harmony: long code generation (realistic) ────────────────────

_add(
    raw=(
        '<|start|>assistant<|channel|>analysis<|message|>'
        'I need to create the models module with Room, Item, NPC, and GameState classes.'
        '<|end|><|start|>assistant<|channel|>final<|message|>'
        '```python\n'
        'from dataclasses import dataclass, field\n'
        'from typing import Dict, List, Optional\n'
        '\n'
        '@dataclass\n'
        'class Room:\n'
        '    name: str\n'
        '    description: str\n'
        '    exits: Dict[str, str] = field(default_factory=dict)\n'
        '    items: List[str] = field(default_factory=list)\n'
        '\n'
        '    def get_exit(self, direction: str) -> Optional[str]:\n'
        '        return self.exits.get(direction)\n'
        '\n'
        '@dataclass\n'
        'class GameState:\n'
        '    current_room: str = "start"\n'
        '    inventory: List[str] = field(default_factory=list)\n'
        '\n'
        '    def to_dict(self) -> dict:\n'
        '        return {"room": self.current_room, "inventory": self.inventory}\n'
        '```\n'
        '<|end|>'
    ),
    content=(
        '```python\n'
        'from dataclasses import dataclass, field\n'
        'from typing import Dict, List, Optional\n'
        '\n'
        '@dataclass\n'
        'class Room:\n'
        '    name: str\n'
        '    description: str\n'
        '    exits: Dict[str, str] = field(default_factory=dict)\n'
        '    items: List[str] = field(default_factory=list)\n'
        '\n'
        '    def get_exit(self, direction: str) -> Optional[str]:\n'
        '        return self.exits.get(direction)\n'
        '\n'
        '@dataclass\n'
        'class GameState:\n'
        '    current_room: str = "start"\n'
        '    inventory: List[str] = field(default_factory=list)\n'
        '\n'
        '    def to_dict(self) -> dict:\n'
        '        return {"room": self.current_room, "inventory": self.inventory}\n'
        '```\n'
    ),
    notes="Realistic code generation with dataclasses, multiple returns, type hints",
)


def get_all_curated() -> list[CuratedExample]:
    """Return all curated examples."""
    return list(CURATED_EXAMPLES)


def get_training_data() -> list[tuple[list[Atom], list[str]]]:
    """Convert all curated examples to CRF training pairs."""
    return [curated_to_training_pairs(ex) for ex in CURATED_EXAMPLES]
