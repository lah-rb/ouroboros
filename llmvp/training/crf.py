"""CRF-based supervised delimiter detection.

Uses python-crfsuite for training a Conditional Random Field on
auto-labeled data. The auto-labeler assigns ground truth labels
to each featurized atom using the known delimiter structure:

  Harmony (GPT-OSS):
    <|channel|>analysis<|message|> {thinking} <|end|><|start|>assistant
    <|channel|>final<|message|> {content}

  Qwen3:
    <think> {thinking} </think> {content}

  Mistral:
    [INST] {prompt} [/INST] {content} </s>

Labels:
  - "D" (delimiter): structural markers like <|channel|>, <|message|>, etc.
  - "T" (thinking):  analysis/reasoning text before the final channel
  - "C" (content):   the actual response after the final channel marker
  - "E" (terminal):  end-of-sequence marker

The CRF learns to predict these labels from observation features,
generalizing to handle edge cases, fragmented delimiters, and
unusual model outputs that the auto-labeler's rules don't cover.
"""

from __future__ import annotations

import logging
import pickle
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from training.featurizer import featurize, Atom, ObsCategory
from training.corpus import (
    TrainingExample,
    load_corpus,
    save_example,
    DEFAULT_CORPUS_PATH,
)

log = logging.getLogger("llm-mvp")

# ── Label set ─────────────────────────────────────────────────────────

LABELS = ["D", "T", "C", "E"]  # delimiter, thinking, content, terminal
LABEL_NAMES = {
    "D": "delimiter",
    "T": "thinking",
    "C": "content",
    "E": "terminal",
}


# ── Auto-labeler ──────────────────────────────────────────────────────

def auto_label_harmony(raw_text: str, atoms: list[Atom]) -> list[str]:
    """Auto-label atoms for a Harmony (GPT-OSS) model output.

    Strategy:
      1. Find the last <|message|> marker — everything after it is content.
      2. Find the <|channel|>final sequence — the delimiter block around it.
      3. Everything before the final channel transition is thinking or delimiter.

    Handles edge cases:
      - Full: <|channel|>analysis<|message|>...<|end|><|start|>...<|channel|>final<|message|>...
      - Short: <|channel|>final<|message|>...
      - Constrain: <|channel|>final <|constrain|>json<|message|>...
      - Menu leak: a<|end|><|start|>assistant<|channel|> (no <|message|>)
      - Pure leak: <|end|><|start|>assistant<|channel|>analysis (no content)
    """
    labels = ["T"] * len(atoms)  # default everything to thinking

    # Set of categories that are ALWAYS structural markers
    MARKER_CATS = {
        ObsCategory.ANGLE_PIPE_OPEN,
        ObsCategory.ANGLE_PIPE_CLOSE,
        ObsCategory.MARKER_CHANNEL,
        ObsCategory.MARKER_START,
        ObsCategory.MARKER_END,
        ObsCategory.MARKER_MESSAGE,
        ObsCategory.MARKER_RETURN,
        ObsCategory.MARKER_CALL,
        ObsCategory.MARKER_CONSTRAIN,
        ObsCategory.MARKER_IM_START,
        ObsCategory.MARKER_IM_END,
        ObsCategory.CHAN_ANALYSIS,
        ObsCategory.CHAN_FINAL,
        ObsCategory.CHAN_TOOL,
    }

    # Find the character offset of the last <|message|> marker
    last_message_end = -1
    for m in re.finditer(r'<\|message\|>', raw_text):
        last_message_end = m.end()

    if last_message_end == -1:
        # No <|message|> found. This covers:
        #   - Pure content with no delimiters at all
        #   - Menu leak: "a\n\n<|start|>assistant<|channel|>"
        #   - Pure delimiter leak: "<|end|><|start|>assistant<|channel|>analysis"
        #
        # Strategy: any structural marker atom → D (delimiter).
        # Any non-marker text that appears BEFORE the first marker → C (content).
        # Any non-marker text that appears AFTER or BETWEEN markers → D (it's
        # a role word like "assistant" or channel name like "analysis").
        # If there are no markers at all → everything is C (content).

        has_any_marker = any(a.category in MARKER_CATS for a in atoms)

        if not has_any_marker:
            # No delimiters at all — pure content
            for i, atom in enumerate(atoms):
                labels[i] = "C"
            if atoms and atoms[-1].category == ObsCategory.EOS:
                labels[-1] = "E"
            return labels

        # Has markers — find where the first marker starts
        first_marker_offset = None
        for atom in atoms:
            if atom.category in MARKER_CATS:
                first_marker_offset = atom.offset
                break

        for i, atom in enumerate(atoms):
            if atom.category == ObsCategory.EOS:
                labels[i] = "E"
            elif atom.category in MARKER_CATS:
                labels[i] = "D"
            elif first_marker_offset is not None and atom.offset < first_marker_offset:
                # Non-marker text before the first marker — this is content
                # (e.g. the "a" in "a<|end|><|start|>...")
                labels[i] = "C"
            else:
                # Non-marker text after/between markers — structural words
                # like "assistant", "analysis", "final" that leaked
                labels[i] = "D"

        return labels

    # ── Standard path: <|message|> found ──────────────────────────

    # Check for a <|channel|>final that comes AFTER the last <|message|>.
    # This handles the pattern where the final channel transition occurs
    # without its own <|message|> tag:
    #   <|channel|>analysis<|message|>thinking...<|end|><|start|>assistant<|channel|>final\ncontent
    # In this case, content starts after "final" + whitespace, not at the earlier <|message|>.
    last_final_after_message = -1
    for m in re.finditer(r'<\|channel\|>final', raw_text):
        if m.start() > last_message_end:
            last_final_after_message = m.end()

    if last_final_after_message > 0:
        # The final channel is after the last <|message|>, so that <|message|>
        # was for an earlier channel (analysis). Recalculate content boundary
        # as the position after "final" + any whitespace.
        content_start = last_final_after_message
        # Skip whitespace/newlines after "final"
        while content_start < len(raw_text) and raw_text[content_start] in ' \t\n\r':
            content_start += 1

        # Find the delimiter block start: the <|end|> before the final channel
        final_block_start = -1
        for m in re.finditer(r'<\|end\|>', raw_text):
            if m.start() < last_final_after_message:
                final_block_start = m.start()

        if final_block_start == -1:
            # No <|end|> before final — the final channel starts the text
            final_block_start = 0
            for m in re.finditer(r'<\|channel\|>final', raw_text):
                if m.start() > last_message_end:
                    final_block_start = m.start()
                    break

        # Label: everything before final_block_start is T (thinking) or D (markers),
        # the final block is D, and content starts at content_start
        for i, atom in enumerate(atoms):
            if atom.category == ObsCategory.EOS:
                labels[i] = "E"
            elif atom.offset >= content_start:
                labels[i] = "C"
            elif atom.offset >= final_block_start:
                labels[i] = "D"
            elif atom.category in MARKER_CATS:
                labels[i] = "D"
            else:
                labels[i] = "T"

        return labels

    # ── Normal path: last <|message|> is the content boundary ────

    # Find where the final channel block starts.
    final_block_start = -1

    for m in re.finditer(r'<\|channel\|>final', raw_text):
        if m.start() < last_message_end:
            final_block_start = m.start()

    for m in re.finditer(r'<\|end\|>', raw_text):
        if m.start() < last_message_end and m.start() > final_block_start:
            if final_block_start == -1:
                final_block_start = m.start()

    if final_block_start == -1:
        first_message = re.search(r'<\|message\|>', raw_text)
        if first_message:
            final_block_start = 0
            last_message_end = first_message.end()

    # Label each atom based on its position
    for i, atom in enumerate(atoms):
        if atom.category == ObsCategory.EOS:
            labels[i] = "E"
            continue

        offset = atom.offset

        if offset >= last_message_end:
            # After the content boundary — but structural markers and
            # role words between markers (e.g. "assistant" between <|start|>
            # and <|channel|>) are delimiters, not content.
            if atom.category in MARKER_CATS:
                labels[i] = "D"
            else:
                # Check if this non-marker atom is sandwiched between markers.
                # Look at neighbors: if both adjacent atoms are markers,
                # this is a structural word like "assistant" or "analysis".
                prev_is_marker = (
                    i > 0 and atoms[i - 1].category in MARKER_CATS
                )
                next_is_marker = (
                    i + 1 < len(atoms) and atoms[i + 1].category in MARKER_CATS
                )
                if prev_is_marker and next_is_marker:
                    labels[i] = "D"
                else:
                    labels[i] = "C"
        elif final_block_start >= 0 and offset >= final_block_start:
            labels[i] = "D"
        elif atom.category in MARKER_CATS:
            labels[i] = "D"
        else:
            labels[i] = "T"

    return labels


def auto_label_qwen(raw_text: str, atoms: list[Atom]) -> list[str]:
    """Auto-label atoms for Qwen3 (<think>...</think>) output."""
    labels = ["C"] * len(atoms)  # default to content

    think_end = -1
    m = re.search(r'</think>', raw_text)
    if m:
        think_end = m.end()

    for i, atom in enumerate(atoms):
        if atom.category == ObsCategory.EOS:
            labels[i] = "E"
        elif think_end > 0 and atom.offset < think_end:
            # Inside or before </think>
            if atom.category in {ObsCategory.ANGLE_OPEN, ObsCategory.ANGLE_CLOSE,
                                  ObsCategory.MARKER_THINK, ObsCategory.SLASH}:
                labels[i] = "D"
            else:
                labels[i] = "T"

    return labels


def auto_label_mistral(raw_text: str, atoms: list[Atom]) -> list[str]:
    """Auto-label atoms for Mistral ([INST]...[/INST]) output.

    Careful to distinguish [INST]/[/INST] structural markers from
    LaTeX math brackets \\[...\\] which are common in reasoning output.
    Only labels brackets as delimiters when they're part of actual
    INST tag sequences.
    """
    labels = ["C"] * len(atoms)

    # Find [/INST] boundary — everything before it is thinking/delimiter
    inst_end = -1
    m = re.search(r'\[/INST\]', raw_text)
    if m:
        inst_end = m.end()

    # Also find the [INST] opening and [/INST] closing positions
    # so we can precisely label only those brackets as delimiters
    inst_regions: list[tuple[int, int]] = []
    for match in re.finditer(r'\[/?INST\]', raw_text):
        inst_regions.append((match.start(), match.end()))

    # Also handle </s> and <s> as structural markers
    s_regions: list[tuple[int, int]] = []
    for match in re.finditer(r'</?s>', raw_text):
        s_regions.append((match.start(), match.end()))

    def _in_inst_region(offset: int) -> bool:
        return any(start <= offset < end for start, end in inst_regions)

    def _in_s_region(offset: int) -> bool:
        return any(start <= offset < end for start, end in s_regions)

    for i, atom in enumerate(atoms):
        if atom.category == ObsCategory.EOS:
            labels[i] = "E"
            continue

        offset = atom.offset

        # Is this atom inside an [INST] or [/INST] tag?
        if _in_inst_region(offset):
            labels[i] = "D"
        # Is this a </s> or <s> tag?
        elif _in_s_region(offset):
            labels[i] = "D"
        # Text before [/INST] is thinking
        elif inst_end > 0 and offset < inst_end:
            labels[i] = "T"
        # Text after [/INST] is content
        else:
            labels[i] = "C"

    return labels


def auto_label(raw_text: str, model_family: str) -> list[str]:
    """Auto-label a raw model output based on model family.

    Returns a list of label strings, one per atom from featurize(raw_text).
    """
    atoms = featurize(raw_text)

    if model_family in ("gpt-oss", "unknown"):
        return auto_label_harmony(raw_text, atoms)
    elif model_family in ("qwen3", "qwen"):
        return auto_label_qwen(raw_text, atoms)
    elif model_family in ("mistral", "devstral"):
        return auto_label_mistral(raw_text, atoms)
    else:
        return auto_label_harmony(raw_text, atoms)  # default


# ── CRF Feature Extraction ───────────────────────────────────────────

def _atom_features(atoms: list[Atom], i: int) -> dict[str, Any]:
    """Extract CRF features for the atom at position i.

    Features are designed to be universal across model families:
      - Atom category and bias
      - Position features (decile, BOS/EOS)
      - Context window (±2 atoms' categories + bigrams)
      - Structural marker flag
      - Inside-delimiter detection (angle-pipe, bracket, angle tag)
      - Content boundary detection (after the last content transition
        marker for any family: <|channel|>final, </think>, [/INST])
    """
    atom = atoms[i]
    cat = atom.category.value
    n = len(atoms)

    features = {
        "bias": 1.0,
        f"cat={cat}": 1.0,
        f"pos_pct={i * 10 // n}": 1.0,  # decile position
    }

    # Structural marker flag (universal — covers all families)
    marker_cats = {"APO", "APC", "M_CH", "M_ST", "M_EN", "M_MG", "M_RT",
                   "M_CL", "M_CN", "M_TK", "M_IS", "M_IE", "M_IN", "M_ET",
                   "C_AN", "C_FI", "C_TL"}
    if cat in marker_cats:
        features["is_marker"] = 1.0

    # Begin/end
    if i == 0:
        features["BOS"] = 1.0
    if i == n - 1:
        features["EOS_pos"] = 1.0

    # Context window: -2 to +2
    for offset in [-2, -1, 1, 2]:
        j = i + offset
        if 0 <= j < n:
            neighbor_cat = atoms[j].category.value
            features[f"{offset:+d}:cat={neighbor_cat}"] = 1.0
            features[f"bi={cat}/{neighbor_cat}@{offset:+d}"] = 1.0
        else:
            features[f"{offset:+d}:BOS/EOS"] = 1.0

    # ── Inside-delimiter detection (universal) ────────────────────
    # Harmony: inside <|...|> sequence
    in_angle_pipe = False
    for j in range(i - 1, max(i - 6, -1), -1):
        if atoms[j].category == ObsCategory.ANGLE_PIPE_CLOSE:
            break
        if atoms[j].category == ObsCategory.ANGLE_PIPE_OPEN:
            in_angle_pipe = True
            break
    if in_angle_pipe:
        features["in_delim_seq"] = 1.0
        features["in_angle_pipe"] = 1.0

    # Mistral: inside [INST] or [/INST] sequence
    in_bracket_tag = False
    for j in range(i - 1, max(i - 6, -1), -1):
        if atoms[j].category == ObsCategory.BRACKET_CLOSE:
            break
        if atoms[j].category == ObsCategory.BRACKET_OPEN:
            in_bracket_tag = True
            break
    if in_bracket_tag:
        features["in_delim_seq"] = 1.0
        features["in_bracket_tag"] = 1.0

    # Qwen: inside <think> or </think> tag
    in_angle_tag = False
    for j in range(i - 1, max(i - 4, -1), -1):
        if atoms[j].category == ObsCategory.ANGLE_CLOSE:
            break
        if atoms[j].category == ObsCategory.ANGLE_OPEN:
            in_angle_tag = True
            break
    if in_angle_tag:
        features["in_delim_seq"] = 1.0
        features["in_angle_tag"] = 1.0

    # ── Content boundary detection (universal) ────────────────────
    # Scans backwards to detect whether the content boundary has been
    # crossed. This is THE key feature that distinguishes thinking
    # text from content text — both are made of normal words, but
    # content comes after the boundary.
    #
    # Boundary markers by family:
    #   Harmony:  <|channel|>final ... <|message|>  (last <|message|> after final)
    #   Qwen:     </think>
    #   Mistral:  [/INST]
    #
    # We detect all of them — in a multi-family corpus, each example
    # will only trigger its own family's boundary.

    saw_content_boundary = False
    # Track state for Harmony: need both "final" and a subsequent "message"
    saw_final_channel = False

    for j in range(i):
        jcat = atoms[j].category

        # Harmony: <|channel|>final ... <|message|>
        if jcat == ObsCategory.CHAN_FINAL:
            saw_final_channel = True
        if saw_final_channel and jcat == ObsCategory.MARKER_MESSAGE:
            saw_content_boundary = True

        # Qwen: </think> — featurized as AO("</") + M_TK("think") + AC(">")
        # The opening <think> is AO("<") + M_TK("think") + AC(">")
        # Distinguish by AO text length: "</" (len 2) vs "<" (len 1)
        if jcat == ObsCategory.MARKER_THINK and j > 0:
            prev_atom = atoms[j - 1]
            if (prev_atom.category == ObsCategory.ANGLE_OPEN
                    and len(prev_atom.text) >= 2):  # "</" not just "<"
                saw_content_boundary = True

        # Mistral: [/INST] — featurized as BO("[") + SL("/") + M_IN("INST") + BC("]")
        # Detect: M_IN preceded by SL
        if jcat == ObsCategory.MARKER_INST and j > 0:
            prev = atoms[j - 1].category
            if prev == ObsCategory.SLASH:
                saw_content_boundary = True

        # Also catch MARKER_END_TAG directly (if the featurizer ever
        # produces it as a single token for "/INST" or "/END")
        if jcat == ObsCategory.MARKER_END_TAG:
            saw_content_boundary = True

    if saw_content_boundary:
        features["after_content_boundary"] = 1.0

    # Also keep family-specific boundary features for cases where
    # the universal feature might be ambiguous
    if saw_final_channel:
        features["after_final_channel"] = 1.0

    return features


def example_to_features(atoms: list[Atom]) -> list[dict[str, Any]]:
    """Convert a full atom sequence to CRF feature dicts."""
    return [_atom_features(atoms, i) for i in range(len(atoms))]


# ── CRF Training ──────────────────────────────────────────────────────

@dataclass
class CRFModel:
    """Wrapper around a trained CRF model file."""
    model_path: str
    model_family: str
    n_examples: int
    label_set: list[str]


def train_crf(
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    model_family: str | None = None,
    output_path: Path | None = None,
    c1: float = 0.1,
    c2: float = 0.1,
    max_iterations: int = 200,
) -> CRFModel:
    """Train a CRF on curated + auto-labeled training data.

    Training data priority:
      1. Curated examples (hand-labeled content boundaries)
      2. Auto-labeled corpus examples (heuristic labels from raw text)

    Args:
        corpus_path: Path to the training corpus JSONL.
        model_family: Filter to this model family. If None, use all.
        output_path: Where to save the trained model. Defaults to
                     {corpus_dir}/delim_crf.model
        c1: L1 regularization coefficient.
        c2: L2 regularization coefficient.
        max_iterations: Maximum training iterations.

    Returns:
        CRFModel with the path to the saved model.
    """
    import pycrfsuite

    # Load auto-labeled corpus
    examples = load_corpus(corpus_path)
    if model_family:
        examples = [ex for ex in examples if ex.model_family == model_family]

    output_path = output_path or corpus_path.parent / "delim_crf.model"

    # Load curated examples
    try:
        from training.curated import get_training_data as get_curated_data
        curated_pairs = get_curated_data()
    except Exception as e:
        print(f"⚠️  Could not load curated examples: {e}")
        curated_pairs = []

    if not examples and not curated_pairs:
        raise ValueError(
            f"No examples found in {corpus_path}"
            + (f" for family '{model_family}'" if model_family else "")
            + " and no curated examples available"
        )

    print(f"🏋️ CRF Training")
    print(f"   Curated examples: {len(curated_pairs)}")
    print(f"   Corpus examples: {len(examples)}")
    print(f"   Family: {model_family or 'all'}")
    print(f"   L1={c1}, L2={c2}, max_iter={max_iterations}")
    print()

    # Build training data — curated first, then auto-labeled
    X_train = []
    y_train = []
    label_counts = {l: 0 for l in LABELS}

    # Curated examples (highest quality — add first)
    for atoms, labels in curated_pairs:
        features = example_to_features(atoms)
        X_train.append(features)
        y_train.append(labels)
        for l in labels:
            label_counts[l] = label_counts.get(l, 0) + 1

    # Auto-labeled corpus examples
    for ex in examples:
        atoms = featurize(ex.raw_text)
        labels = auto_label(ex.raw_text, ex.model_family)

        assert len(atoms) == len(labels), (
            f"Atom/label length mismatch for {ex.prompt_id}: "
            f"{len(atoms)} atoms, {len(labels)} labels"
        )

        features = example_to_features(atoms)
        X_train.append(features)
        y_train.append(labels)

        for l in labels:
            label_counts[l] = label_counts.get(l, 0) + 1

    print(f"   Label distribution:")
    total = sum(label_counts.values())
    for label, count in sorted(label_counts.items()):
        pct = 100 * count / total if total > 0 else 0
        print(f"     {label} ({LABEL_NAMES.get(label, label):>10s}): {count:6d} ({pct:.1f}%)")
    print()

    # Train
    trainer = pycrfsuite.Trainer(verbose=False)

    for xseq, yseq in zip(X_train, y_train):
        trainer.append(xseq, yseq)

    trainer.set_params({
        "c1": c1,
        "c2": c2,
        "max_iterations": max_iterations,
        "feature.possible_transitions": True,
    })

    trainer.train(str(output_path))

    # Report
    print(f"   Training complete.")
    print(f"   Model saved to: {output_path}")

    return CRFModel(
        model_path=str(output_path),
        model_family=model_family or "all",
        n_examples=len(examples),
        label_set=LABELS,
    )


# ── CRF Inference ─────────────────────────────────────────────────────

_crf_tagger = None
_crf_model_path = None


def _get_tagger(model_path: str):
    """Lazy-load the CRF tagger, caching across calls."""
    global _crf_tagger, _crf_model_path
    if _crf_tagger is None or _crf_model_path != model_path:
        import pycrfsuite
        _crf_tagger = pycrfsuite.Tagger()
        _crf_tagger.open(model_path)
        _crf_model_path = model_path
    return _crf_tagger


def crf_decode(model_path: str, raw_text: str) -> list[tuple[str, str]]:
    """Decode raw model output into (atom_text, label) pairs.

    Args:
        model_path: Path to the trained CRF model file.
        raw_text: Raw model output.

    Returns:
        List of (text, label) tuples.
    """
    tagger = _get_tagger(model_path)
    atoms = featurize(raw_text)
    features = example_to_features(atoms)
    labels = tagger.tag(features)
    return [(atom.text, label) for atom, label in zip(atoms, labels)]


def crf_extract_content(model_path: str, raw_text: str) -> str:
    """Extract the content phase from raw model output using the CRF.

    This is the inference-time function that replaces _strip_delimiter.
    """
    decoded = crf_decode(model_path, raw_text)
    content_parts = [text for text, label in decoded if label == "C"]
    return "".join(content_parts).strip()


def crf_extract_phases(model_path: str, raw_text: str) -> dict[str, str]:
    """Extract all phases from raw model output."""
    decoded = crf_decode(model_path, raw_text)
    phases: dict[str, list[str]] = {l: [] for l in LABELS}
    for text, label in decoded:
        if label in phases:
            phases[label].append(text)
    return {
        LABEL_NAMES.get(k, k): "".join(v).strip()
        for k, v in phases.items()
    }


# ── Evaluation ────────────────────────────────────────────────────────

def evaluate_crf(
    model_path: str,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    model_family: str | None = None,
) -> dict[str, Any]:
    """Evaluate CRF accuracy against auto-labels.

    Returns per-label precision, recall, F1 and overall accuracy.
    """
    examples = load_corpus(corpus_path)
    if model_family:
        examples = [ex for ex in examples if ex.model_family == model_family]

    tagger = _get_tagger(model_path)
    total = 0
    correct = 0
    label_tp = {l: 0 for l in LABELS}
    label_fp = {l: 0 for l in LABELS}
    label_fn = {l: 0 for l in LABELS}

    # Also track content extraction accuracy
    content_correct = 0
    content_total = 0

    for ex in examples:
        atoms = featurize(ex.raw_text)
        gold_labels = auto_label(ex.raw_text, ex.model_family)
        features = example_to_features(atoms)
        pred_labels = tagger.tag(features)

        for gold, pred in zip(gold_labels, pred_labels):
            total += 1
            if gold == pred:
                correct += 1
                label_tp[gold] = label_tp.get(gold, 0) + 1
            else:
                label_fp[pred] = label_fp.get(pred, 0) + 1
                label_fn[gold] = label_fn.get(gold, 0) + 1

        # Content extraction check
        gold_content = "".join(
            atom.text for atom, label in zip(atoms, gold_labels) if label == "C"
        ).strip()
        pred_content = "".join(
            atom.text for atom, label in zip(atoms, pred_labels) if label == "C"
        ).strip()
        content_total += 1
        if gold_content == pred_content:
            content_correct += 1

    # Compute per-label metrics
    results = {
        "accuracy": correct / total if total > 0 else 0,
        "total_tokens": total,
        "content_extraction_accuracy": content_correct / content_total if content_total > 0 else 0,
        "per_label": {},
    }

    for label in LABELS:
        tp = label_tp.get(label, 0)
        fp = label_fp.get(label, 0)
        fn = label_fn.get(label, 0)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        results["per_label"][label] = {
            "name": LABEL_NAMES.get(label, label),
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": tp + fn,
        }

    return results


def print_evaluation(results: dict[str, Any]) -> None:
    """Pretty-print evaluation results."""
    print(f"\n{'='*60}")
    print(f"CRF Evaluation Results")
    print(f"{'='*60}")
    print(f"Overall accuracy: {results['accuracy']:.4f} ({results['total_tokens']} tokens)")
    print(f"Content extraction: {results['content_extraction_accuracy']:.4f}")
    print()
    print(f"{'Label':<12s} {'Precision':>10s} {'Recall':>10s} {'F1':>10s} {'Support':>10s}")
    print(f"{'-'*52}")
    for label in LABELS:
        m = results["per_label"].get(label, {})
        print(
            f"{LABEL_NAMES.get(label, label):<12s} "
            f"{m.get('precision', 0):>10.4f} "
            f"{m.get('recall', 0):>10.4f} "
            f"{m.get('f1', 0):>10.4f} "
            f"{m.get('support', 0):>10d}"
        )
    print()
