"""Parser for curated CRF training data in annotated JSON format.

Reads ``knowledge/crf/curated.json`` — a JSON array of examples where
phase boundaries are marked inline with ``***[X]***`` fences:

    ***[C]***content here***[C]***      — content phase
    ***[T]***thinking here***[T]***     — thinking phase

Unmarked regions default to D (delimiter) — structural tokens like
``<|channel|>``, ``<|start|>``, ``[INST]`` are self-evident and don't
need annotation.  E (terminal) is auto-detected from the featurizer's
EOS atoms.

The parser:
  1. Reads each entry's ``raw`` field
  2. Extracts fence positions and the phase they mark
  3. Strips the fence markers to recover the clean raw text
  4. Featurizes the clean text into atoms
  5. Assigns labels based on which fence region each atom falls in
  6. Returns ``(atoms, labels)`` pairs ready for CRF training

Schema (per entry):
    family  (str, required):  "harmony", "chatml", or "tekken"
    raw     (str, required):  annotated raw text with ***[X]*** fences
    source  (str, optional):  origin tag (e.g. "challenge-478", "manual")
    notes   (str, optional):  what makes this example useful for training
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from core.featurizer import featurize, Atom, ObsCategory

log = logging.getLogger("llm-mvp")

# ── Fence pattern ────────────────────────────────────────────────────
# Matches ***[C]***, ***[T]***, etc.  Labels are single uppercase letters.
_FENCE_PATTERN = re.compile(r"\*\*\*\[([A-Z])\]\*\*\*")

# Default path for the curated JSON file
DEFAULT_CURATED_PATH = (
    Path(__file__).resolve().parent.parent / "knowledge" / "crf" / "curated.json"
)


@dataclass
class CuratedEntry:
    """A single parsed curated example."""

    family: str
    raw_clean: str  # raw text with fences stripped
    regions: list[tuple[str, int, int]]  # (label, start, end) in clean text
    source: str = ""
    notes: str = ""


def _parse_fences(annotated: str) -> tuple[str, list[tuple[str, int, int]]]:
    """Strip ***[X]*** fences and extract labeled regions.

    Fences come in pairs — open and close use the same marker.
    The text between each pair is assigned the fence's label.

    Returns:
        (clean_text, regions) where regions is a list of
        (label, start_offset, end_offset) in the clean text.
    """
    # Find all fence positions in the annotated text
    matches = list(_FENCE_PATTERN.finditer(annotated))

    if not matches:
        # No fences — entire text is unlabeled (all delimiter)
        return annotated, []

    # Pair up fences: consecutive same-label fences form open/close pairs
    open_fences: dict[str, int] = {}  # label → annotated offset of content start
    regions_annotated: list[tuple[str, int, int]] = (
        []
    )  # (label, start, end) in annotated text

    for m in matches:
        label = m.group(1)
        if label not in open_fences:
            # Opening fence — content starts after the fence
            open_fences[label] = m.end()
        else:
            # Closing fence — content ends at the fence start
            content_start = open_fences.pop(label)
            content_end = m.start()
            regions_annotated.append((label, content_start, content_end))

    # Warn about unclosed fences
    for label, offset in open_fences.items():
        log.warning(
            "Unclosed ***[%s]*** fence at offset %d — treating rest as %s",
            label,
            offset,
            label,
        )
        regions_annotated.append((label, offset, len(annotated)))

    # Now strip all fence markers to get clean text.
    # Build a mapping from annotated offsets to clean offsets.
    clean_parts: list[str] = []
    fence_ranges = [(m.start(), m.end()) for m in _FENCE_PATTERN.finditer(annotated)]
    prev_end = 0
    offset_adjustments: list[tuple[int, int]] = (
        []
    )  # (annotated_pos, chars_removed_before)

    chars_removed = 0
    for fence_start, fence_end in fence_ranges:
        clean_parts.append(annotated[prev_end:fence_start])
        offset_adjustments.append((fence_start, chars_removed))
        chars_removed += fence_end - fence_start
        offset_adjustments.append((fence_end, chars_removed))
        prev_end = fence_end
    clean_parts.append(annotated[prev_end:])
    offset_adjustments.append((len(annotated), chars_removed))

    clean_text = "".join(clean_parts)

    def _to_clean_offset(annotated_offset: int) -> int:
        """Convert an offset in annotated text to clean text offset."""
        removed = 0
        for pos, adj in offset_adjustments:
            if pos <= annotated_offset:
                removed = adj
            else:
                break
        return annotated_offset - removed

    # Convert annotated regions to clean-text regions
    regions_clean = [
        (label, _to_clean_offset(start), _to_clean_offset(end))
        for label, start, end in regions_annotated
    ]

    return clean_text, regions_clean


def _labels_from_regions(
    atoms: list[Atom],
    regions: list[tuple[str, int, int]],
) -> list[str]:
    """Assign a label to each atom based on fence regions.

    Atoms inside a fenced region get that region's label.
    Atoms outside all regions get "D" (delimiter).
    EOS atoms always get "E" (terminal).
    """
    labels: list[str] = []
    for atom in atoms:
        if atom.category == ObsCategory.EOS:
            labels.append("E")
            continue

        # Check if this atom falls inside any fenced region
        assigned = False
        for label, r_start, r_end in regions:
            if atom.offset >= r_start and atom.offset + atom.length <= r_end:
                labels.append(label)
                assigned = True
                break

        if not assigned:
            labels.append("D")

    return labels


def load_curated(
    path: Path | str | None = None,
) -> list[CuratedEntry]:
    """Load curated examples from the JSON file.

    Args:
        path: Path to the curated JSON file. Defaults to
              knowledge/crf/curated.json.

    Returns:
        List of parsed CuratedEntry objects.
    """
    path = Path(path) if path else DEFAULT_CURATED_PATH
    if not path.exists():
        log.warning("No curated examples file at %s", path)
        return []

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    entries: list[CuratedEntry] = []
    for i, item in enumerate(data):
        raw_annotated = item.get("raw", "")
        if not raw_annotated:
            log.warning("Curated entry %d has empty raw field — skipping", i)
            continue

        clean_text, regions = _parse_fences(raw_annotated)

        entries.append(
            CuratedEntry(
                family=item.get("family", "harmony"),
                raw_clean=clean_text,
                regions=regions,
                source=item.get("source", ""),
                notes=item.get("notes", ""),
            )
        )

    return entries


def get_training_data(
    path: Path | str | None = None,
) -> list[tuple[list[Atom], list[str]]]:
    """Load curated examples and convert to CRF training pairs.

    This is the main entry point consumed by ``train_crf``.

    Returns:
        List of (atoms, labels) tuples ready for CRF training.
    """
    entries = load_curated(path)
    pairs: list[tuple[list[Atom], list[str]]] = []

    for entry in entries:
        atoms = featurize(entry.raw_clean)
        labels = _labels_from_regions(atoms, entry.regions)

        if len(atoms) != len(labels):
            log.error(
                "Atom/label mismatch in curated entry '%s': "
                "%d atoms vs %d labels — skipping",
                entry.notes,
                len(atoms),
                len(labels),
            )
            continue

        pairs.append((atoms, labels))

    log.info(
        "Loaded %d curated training examples from %s",
        len(pairs),
        path or DEFAULT_CURATED_PATH,
    )
    return pairs
