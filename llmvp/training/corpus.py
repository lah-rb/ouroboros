"""Training corpus management for delimiter detection models.

Handles the lifecycle of training data:
  - Capture raw model outputs during inference (collection mode)
  - Store in a CRF-ready JSONL schema (labels nullable for HMM)
  - Load and iterate for training

Schema is designed for progressive enrichment:
  1. Collection: raw_text + observations populated, labels = null
  2. HMM training: Viterbi output fills labels automatically
  3. CRF training: human-corrected labels override HMM labels

The corpus file is append-only JSONL. Each line is a self-contained
training example.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from training.featurizer import featurize, atoms_to_obs_sequence

log = logging.getLogger("llm-mvp")


# ── Phase labels ──────────────────────────────────────────────────────
# These are the target labels for sequence labeling.
# Used by both HMM (as hidden states) and CRF (as tag set).

PHASE_LABELS = ["thinking", "delimiter", "content", "terminal"]
PHASE_TO_ID = {label: i for i, label in enumerate(PHASE_LABELS)}
ID_TO_PHASE = {i: label for i, label in enumerate(PHASE_LABELS)}


@dataclass
class TrainingExample:
    """A single training example for delimiter detection.

    Designed for progressive enrichment:
      - Collection phase: labels is None
      - HMM auto-label phase: labels populated by Viterbi
      - CRF supervised phase: labels hand-corrected
    """

    # Identity
    model_family: str                # "gpt-oss", "qwen3", "mistral", "devstral"
    model_name: str                  # "gpt-oss-120b-a5"
    prompt_id: str                   # "suite-017"
    prompt_category: str             # "reasoning", "constrained", "tool_call", etc.

    # Raw data
    raw_text: str                    # Raw model output before any stripping
    raw_token_ids: list[int] = field(default_factory=list)  # Optional token IDs

    # Featurized sequence
    observations: list[str] = field(default_factory=list)   # ObsCategory values

    # Labels (nullable — filled by HMM or human correction)
    labels: list[str] | None = None  # PHASE_LABELS per observation, or None

    # Label provenance
    label_source: str | None = None  # "hmm_viterbi", "crf_predict", "human", None

    # Generation metadata
    config: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    generation_meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        # Auto-featurize if observations are empty but raw_text exists
        if not self.observations and self.raw_text:
            atoms = featurize(self.raw_text)
            self.observations = atoms_to_obs_sequence(atoms)


def save_example(example: TrainingExample, corpus_path: Path) -> None:
    """Append a training example to the corpus file."""
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    with corpus_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(example), ensure_ascii=False) + "\n")


def load_corpus(corpus_path: Path) -> list[TrainingExample]:
    """Load all training examples from a corpus file."""
    examples = []
    if not corpus_path.exists():
        return examples
    with corpus_path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                examples.append(TrainingExample(**data))
            except Exception as e:
                log.warning("Skipping malformed corpus line %d: %s", line_num, e)
    return examples


def iter_corpus(corpus_path: Path) -> Iterator[TrainingExample]:
    """Iterate training examples lazily (for large corpora)."""
    if not corpus_path.exists():
        return
    with corpus_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                yield TrainingExample(**data)
            except Exception:
                continue


def corpus_stats(corpus_path: Path) -> dict[str, Any]:
    """Return summary statistics about a corpus file."""
    stats: dict[str, Any] = {
        "total_examples": 0,
        "by_family": {},
        "by_category": {},
        "labeled": 0,
        "unlabeled": 0,
        "label_sources": {},
    }
    for ex in iter_corpus(corpus_path):
        stats["total_examples"] += 1
        stats["by_family"][ex.model_family] = (
            stats["by_family"].get(ex.model_family, 0) + 1
        )
        stats["by_category"][ex.prompt_category] = (
            stats["by_category"].get(ex.prompt_category, 0) + 1
        )
        if ex.labels is not None:
            stats["labeled"] += 1
            if ex.label_source:
                stats["label_sources"][ex.label_source] = (
                    stats["label_sources"].get(ex.label_source, 0) + 1
                )
        else:
            stats["unlabeled"] += 1

    return stats


# ── Default corpus path ───────────────────────────────────────────────

DEFAULT_CORPUS_PATH = Path("data/training_corpus.jsonl")
