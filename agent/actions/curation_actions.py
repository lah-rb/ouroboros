"""Curator stage: review + pack extracted papers into the open-key dataset.

Stage 3 of the corpus pipeline (scrape → extract → CURATE). Per paper:
figtext (VLM sidecar, tools/fig_review) is inlined into the curator
doc, one memoryful session ingests the doc once, a review pass delivers
summary + accept/deny, and pack passes strip raw data into an open-key
JSON governed by the key registry. Model output is a CLAIM behind the
deterministic gates in this module; denials and failures are terminal
with reasons, never silent.

## Dataset artifacts (workspace files — never in mission state)

``databank/dataset/<paper_key>.json`` — per-paper envelope::

    {
      "paper_key": ..., "title": ..., "doi": ..., "license": ..., "year": ...,
      "review": {"status": "accepted", "summary": ...},
      "data": { <open keys — registry-governed> },
      "provenance": {"model": ..., "figtext_model": ..., "packed_at": ...,
                     "md_path": ...}
    }

``databank/dataset/key_registry.json`` — the growing key vocabulary::

    {key: {"type": ..., "description": ..., "exemplar": ..., "count": N,
           "first_paper": ..., "similar_to": [..]}}

## Gates (deterministic, ordered)

1. JSON parses (fenced block extraction, parse-and-retry — no grammar
   in v1: grammar would constrain thinking channels too).
2. Required envelope fields (required_fields_check).
3. Per-key type consistency vs the registry (registry_check).
4. Near-duplicate key flags — difflib ratio >= 0.85 + compatible type
   (near_duplicate_keys; v1 FLAGS, merge is a later pass).
5. GROUNDING: every numeric token in ``data`` leaves must appear in the
   curator doc (grounding_check — the anti-fabrication gate).

Numeric tokenization mirrors tools/pdf_extract/extract_batch.py
(separate venv — keep in sync).
"""

from __future__ import annotations

import difflib
import json
import logging
import re

logger = logging.getLogger(__name__)

DATASET_DIR = "databank/dataset"
FIGTEXT_DIR = "databank/figtext"
KEY_REGISTRY_PATH = "databank/dataset/key_registry.json"

MIN_GROUNDING_RATE = 0.95
NEAR_DUP_RATIO = 0.85
REGISTRY_PROMPT_TOP_N = 60

# Mirrors tools/pdf_extract/extract_batch.py _NUM_RE (separate venvs).
_NUM_RE = re.compile(r"-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d{2,}")

_FIGTEXT_MARK = "> [FIGURE {fig} — VLM reading]: {text}"


# ── Curator document ──────────────────────────────────────────────────


def build_curator_doc(md: str, figtext: dict | None) -> str:
    """Paper markdown with figtext inlined as labeled VLM claims.

    Each figure's reading lands directly after the paragraph that
    references it, as an explicitly labeled blockquote — the review
    pass judges the claim in context, and pack grounding matches
    against the combined doc so figure-derived values stay traceable.
    Figures the layout model never anchored are appended at the end.
    Deterministic: the gate rebuilds this identically; the doc is never
    persisted as a second copy of the paper.
    """
    if not figtext or not figtext.get("figs"):
        return md
    key = str(figtext.get("paper_key") or "")
    paragraphs = md.split("\n\n")
    orphans = []
    for entry in figtext["figs"]:
        fig = str(entry.get("fig") or "")
        text = str(entry.get("figtext") or "").strip()
        if not text:
            continue
        block = _FIGTEXT_MARK.format(fig=fig, text=" ".join(text.split()))
        ref = f"figures/{key}/{fig}"
        for i, para in enumerate(paragraphs):
            if ref in para:
                paragraphs[i] = para + "\n\n" + block
                break
        else:
            orphans.append(block)
    doc = "\n\n".join(paragraphs)
    if orphans:
        doc += "\n\n## Unanchored figures (VLM readings)\n\n" + "\n\n".join(orphans)
    return doc


# ── Gates ─────────────────────────────────────────────────────────────


def _norm(s: str) -> str:
    s = re.sub(r"[#*_`|\[\]()>~\-]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _numeric_leaf_tokens(value, path: str = "") -> list[tuple[str, str]]:
    """(json_path, numeric_token) for every leaf under ``value``.

    Numbers inside strings count too — "1.2 GPa at 77 K" carries two
    groundable claims. Booleans are not numerics.
    """
    out: list[tuple[str, str]] = []
    if isinstance(value, bool) or value is None:
        return out
    if isinstance(value, (int, float)):
        for tok in _NUM_RE.findall(repr(value)):
            out.append((path, tok))
    elif isinstance(value, str):
        for tok in _NUM_RE.findall(value):
            out.append((path, tok))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(_numeric_leaf_tokens(v, f"{path}[{i}]"))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.extend(_numeric_leaf_tokens(v, f"{path}.{k}" if path else str(k)))
    return out


def grounding_check(data: dict, doc: str) -> dict:
    """Every numeric token in ``data`` must appear in the curator doc.

    The anti-fabrication gate: a value the paper (text or inlined
    figtext) never states cannot be packed. Matching runs against both
    the normalized doc and a whitespace-compacted form (tables and
    thousands-separated numbers split tokens across whitespace).
    """
    doc_n = _norm(doc)
    doc_compact = re.sub(r"[\s,]", "", doc_n)
    tokens = _numeric_leaf_tokens(data)
    ungrounded = [
        {"path": path, "token": tok}
        for path, tok in tokens
        if tok not in doc_n and tok not in doc_compact
    ]
    rate = 1.0 if not tokens else 1 - len(ungrounded) / len(tokens)
    return {
        "grounding_rate": round(rate, 4),
        "numeric_leaves": len(tokens),
        "ungrounded": ungrounded[:25],
        "passed": rate >= MIN_GROUNDING_RATE,
    }


def required_fields_check(envelope: dict) -> list[str]:
    """Missing/invalid required envelope fields (empty list = pass)."""
    problems = []
    for field in ("paper_key", "title", "license"):
        if not str(envelope.get(field) or "").strip():
            problems.append(f"missing {field}")
    if not (envelope.get("doi") or envelope.get("arxiv_id")):
        problems.append("missing doi|arxiv_id")
    review = envelope.get("review") or {}
    if review.get("status") not in ("accepted", "denied"):
        problems.append("review.status not in accepted|denied")
    if not str(review.get("summary") or "").strip():
        problems.append("missing review.summary")
    data = envelope.get("data")
    if not isinstance(data, dict) or not data:
        problems.append("data empty or not an object")
    return problems


def _type_name(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        inner = {_type_name(v) for v in value}
        return f"list[{inner.pop()}]" if len(inner) == 1 else "list"
    return type(value).__name__


def registry_check(data: dict, registry: dict) -> dict:
    """Per-key type consistency vs the registry + new-key inventory."""
    mismatches, new_keys, reused = [], [], []
    for key, value in data.items():
        entry = registry.get(key)
        if entry is None:
            new_keys.append(key)
            continue
        reused.append(key)
        expected = str(entry.get("type") or "")
        actual = _type_name(value)
        if expected and actual != expected:
            mismatches.append({"key": key, "expected": expected, "actual": actual})
    return {"type_mismatches": mismatches, "new_keys": new_keys, "reused_keys": reused}


def near_duplicate_keys(new_keys: list[str], registry: dict, data: dict) -> list[dict]:
    """New keys suspiciously close to existing ones (flag, don't merge).

    A near-duplicate is a probable synonym the model coined instead of
    reusing the registry ("yield_strength_mpa" vs "yield_strength") —
    string ratio >= NEAR_DUP_RATIO AND compatible value type.
    """
    flags = []
    for new in new_keys:
        new_n = new.lower().replace("-", "_")
        for existing, entry in registry.items():
            ratio = difflib.SequenceMatcher(
                None, new_n, existing.lower().replace("-", "_")
            ).ratio()
            if ratio >= NEAR_DUP_RATIO and _type_name(data.get(new)) == str(
                entry.get("type") or ""
            ):
                flags.append(
                    {"new_key": new, "existing": existing, "ratio": round(ratio, 3)}
                )
    return flags


# ── Key registry ──────────────────────────────────────────────────────


def update_key_registry(registry: dict, data: dict, paper_key: str) -> dict:
    """Fold an ACCEPTED pack into the registry (returns the same dict).

    Deterministic bookkeeping only — descriptions come from the first
    exemplar; near-duplicate merging is a later, separate pass.
    """
    for key, value in data.items():
        entry = registry.get(key)
        if entry is None:
            exemplar = json.dumps(value, ensure_ascii=False)
            registry[key] = {
                "type": _type_name(value),
                "description": "",
                "exemplar": exemplar[:120],
                "count": 1,
                "first_paper": paper_key,
                "similar_to": [],
            }
        else:
            entry["count"] = int(entry.get("count") or 0) + 1
    return registry


def format_key_registry(registry: dict, top_n: int = REGISTRY_PROMPT_TOP_N) -> str:
    """Prompt block: the current vocabulary, most-used first.

    Presented to the pack turn so the model reuses an equivalent key
    instead of coining a synonym; new keys stay allowed for genuinely
    new quantities.
    """
    if not registry:
        return "(registry is empty — this is the first paper; coin clear, unit-suffixed keys)"
    ranked = sorted(registry.items(), key=lambda kv: -int(kv[1].get("count") or 0))
    lines = []
    for key, entry in ranked[:top_n]:
        desc = str(entry.get("description") or "").strip()
        line = f"- {key} ({entry.get('type')}, {entry.get('count')} paper(s))"
        if desc:
            line += f": {desc}"
        line += f" — e.g. {entry.get('exemplar')}"
        lines.append(line)
    if len(ranked) > top_n:
        lines.append(f"...and {len(ranked) - top_n} more keys")
    return "\n".join(lines)
