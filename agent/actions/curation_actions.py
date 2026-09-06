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
from agent.actions.identifiers import envelope_identity
from agent.paths import repo_root as _repo_root
from agent.scheduler.capacity_claim import current_claim
from agent.actions.drain_lane import (
    ClaimSet,
    decline,
    drain_budget,
    select_cost_bounded,
    server_alive,
)

import difflib
import json
import os
import time
import logging
import re

logger = logging.getLogger(__name__)

DATASET_DIR = "databank/dataset"
FIGTEXT_DIR = "databank/figtext"
KEY_REGISTRY_PATH = "databank/dataset/key_registry.json"

MIN_GROUNDING_RATE = 0.95
NEAR_DUP_RATIO = 0.85
REGISTRY_PROMPT_TOP_N = 60
KEY_QUARANTINE_PATH = "databank/dataset/key_registry_quarantine.json"
# COINAGE GUARD (operator ruling 2026-09-04). Measured over 1,766 packs the
# day it was added: new_keys median 0, p90 9, p99 42, max 313; 19 packs
# coined more than 40 keys and 18 of those coined more than twice what they
# reused. The right tail is where a pack stops speaking the registry's
# language -- a faculty newsletter accepted on one LIBS figure coined 227
# keys of admission dates and figure captions, every one folded silently
# into the registry. A pack past BOTH thresholds keeps its data but its new
# keys are HELD in a quarantine file, never folded, until reviewed
# (dev/coinage_quarantine.py). A GREEN registry exempts itself: while fewer
# than COINAGE_MATURE_SHARED_KEYS keys have been seen in two or more papers,
# every pack legitimately coins most of its vocabulary, so the guard only
# logs. The prompt shows the 60 most-used keys, so coined singletons never
# reach a model either way; the guard protects the registry as a shared
# vocabulary, which is the property that makes packs comparable.
COINAGE_MAX_NEW = 40
COINAGE_RATIO = 2.0
COINAGE_MATURE_SHARED_KEYS = 200

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


#: A comma acting as a DECIMAL separator: between digits, with one or two
#: digits after it. Two exclusions carry real weight:
#:   * thousands separators always group in THREES, so {1,2} drops "1,234";
#:   * the trailing (?!\d{1,2},) drops CHAINS — an enumeration like
#:     "batches 1,2,3,4,5,6,7" otherwise reads as six decimal commas and
#:     can convince the convention detector below that a point-convention
#:     document is a comma-convention one, which then grounds a
#:     fabricated "1.2". Caught by test_point_convention_document_is_
#:     left_alone before this ever ran on the corpus.
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d{1,2}\b)(?!\d{1,2},)")
#: A point used the same way, for deciding which convention a doc follows.
_DECIMAL_POINT_RE = re.compile(r"\d\.\d")


#: EVIDENCE that a document uses the comma convention, as opposed to the
#: substitution pattern above. Requires TWO OR MORE digits before the
#: comma, because the cheap look-alikes are single-digit: citation
#: brackets ("[1,2]"), figure references ("Fig. 3,4") and short
#: enumerations all pair single digits, while real measurements
#: ("57,65 %", "1486,6 eV", "45,3 emu/g") carry a multi-digit integer
#: part. Counting only those keeps a bibliography from voting.
_DECIMAL_COMMA_EVIDENCE_RE = re.compile(r"(?<!\d)\d{2,},(?=\d{1,2}\b)(?!\d{1,2},)")


def _decimal_comma_variant(doc_n: str) -> str:
    """``doc_n`` with decimal commas rewritten as points, or "" if the
    document does not use that convention.

    WHY THIS EXISTS. The comma-stripped compact form below was added so
    thousands-separated numbers ground ("1,234,567" -> "1234567"). On a
    document that writes decimals with commas — French, Spanish,
    Portuguese, Russian, German, and plenty of European-published
    English — that same stripping DESTROYS the number: "57,65 %"
    compacts to "5765", so a correctly parsed 57.65 could never match
    and the paper was rejected for being right. Measured 2026-08-24:
    46% of corpus markdown contains decimal-comma numbers and 10% use
    the convention predominantly; 4 of the 5 standing ungrounded pack
    failures were this bug, not fabrication.

    GATED ON THE DOCUMENT'S OWN CONVENTION, deliberately. Rewriting
    unconditionally would turn an enumeration like "samples 1,2 and 3"
    into "1.2" and could ground a value the paper never states — the
    exact failure this gate exists to prevent. So the rewrite applies
    only where comma-decimals are frequent AND at least as common as
    point-decimals, which is the signature of a document that has
    committed to the convention.
    """
    # Evidence and substitution use DIFFERENT patterns on purpose. The
    # evidence bar is strict (multi-digit integer part) so only real
    # measurements vote; once the document has demonstrably committed to
    # the convention, substitution runs on the looser pattern so short
    # values like "5,5" convert too.
    #
    # NOT a ratio against point-decimals — that was the first attempt and
    # it failed on real documents: paddle's HTML tables, DOIs and version
    # strings contribute plenty of point-decimals, so a Russian paper
    # with 94 comma-decimals lost 94-to-190 and never engaged. An
    # absolute floor of demonstrably-measurement-shaped commas is the
    # signal; the point count is not evidence about the body text.
    if len(_DECIMAL_COMMA_EVIDENCE_RE.findall(doc_n)) < 5:
        return ""
    return _DECIMAL_COMMA_RE.sub(".", doc_n)


def grounding_check(data: dict, doc: str) -> dict:
    """Every numeric token in ``data`` must appear in the curator doc.

    The anti-fabrication gate: a value the paper (text or inlined
    figtext) never states cannot be packed. Matching runs against the
    normalized doc, a whitespace-compacted form (tables and
    thousands-separated numbers split tokens across whitespace), and —
    only for documents that use the convention — a decimal-comma
    variant (see ``_decimal_comma_variant``).
    """
    doc_n = _norm(doc)
    doc_compact = re.sub(r"[\s,]", "", doc_n)
    doc_decimal = _decimal_comma_variant(doc_n)
    tokens = _numeric_leaf_tokens(data)
    # Match UNSIGNED: _norm strips '-' from the doc (markdown dash
    # punctuation), so a signed packed token can never match — live,
    # every negative quantity (Curie-Weiss theta, mixing enthalpies,
    # interaction parameters) failed grounding while sitting verbatim
    # in the paper's tables. Magnitude+digits is the grounding anchor;
    # the sign is not a fabrication discriminator.
    ungrounded = [
        {"path": path, "token": tok}
        for path, tok in tokens
        if (u := tok.lstrip("-")) not in doc_n
        and u not in doc_compact
        and not (doc_decimal and u in doc_decimal)
    ]
    rate = 1.0 if not tokens else 1 - len(ungrounded) / len(tokens)
    return {
        "grounding_rate": round(rate, 4),
        "numeric_leaves": len(tokens),
        "ungrounded": ungrounded[:25],
        "passed": rate >= MIN_GROUNDING_RATE,
    }


def required_fields_check(envelope: dict) -> list[str]:
    """Missing/invalid required envelope fields (empty list = pass).

    IDENTITY (operator ruling 2026-09-03). The rule was `doi|arxiv_id`, which
    refused 332 accepted-or-pending papers -- theses and institutional reports
    from CORE, national registries and university repositories, two thirds of
    everything waiting on curation. They packed cleanly and were thrown away
    at the envelope. The envelope now requires an identity BLOCK rather than a
    DOI: whatever tier was resolved (doi, arxiv, openalex, a repository
    handle/urn, a CORE id) or the explicit "none". "none" is a legitimate,
    RECORDED state -- a consumer can tell an unidentified paper from one
    nobody looked at -- and `identifier_kind` says which tier it is, so a
    weakly-identified record is never mistaken for a DOI'd one.
    """
    problems = []
    for field in ("paper_key", "title", "license"):
        if not str(envelope.get(field) or "").strip():
            problems.append(f"missing {field}")
    kind = str(envelope.get("identifier_kind") or "").strip()
    if not kind:
        problems.append("missing identifier_kind (expected a tier or 'none')")
    elif kind != "none" and not str(envelope.get("identifier") or "").strip():
        problems.append(f"identifier_kind {kind!r} with no identifier")
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


def _types_compatible(expected: str, actual: str) -> bool:
    """T and list[T] are one vocabulary slot, not a mismatch.

    The registry pins a key's type from its FIRST paper; a later paper
    with several phases/samples honestly needs a list of the same
    scalar (live: lattice_parameter_angstrom, one phase then three).
    Real drift (number vs string) still fails.
    """
    if actual == f"list[{expected}]" or expected == f"list[{actual}]":
        return True
    # A registry entry typed bare "list" was pinned by a paper whose value was
    # a MIXED list, so it says "a list of things" and cannot then refuse a list
    # of one kind of thing. Measured 2026-09-03: a window grounding 547 values
    # at 0.998 was thrown away because xps_peak_binding_energy_ev is registered
    # `list` and the pack sent `list[object]`.
    return "list" in (expected, actual) and (
        expected.startswith("list") and actual.startswith("list")
    )


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
        if expected and actual != expected and not _types_compatible(expected, actual):
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


# ── canonical vocabulary (operator reform, 2026-08-22) ────────────────
#
# The registry grew to 1,522 observed keys, 94% used once. The reform
# tiers it: `core` (>=10 uses, 54 keys), `family` (753 keys annotated
# into six semantic families), `bespoke-pool` (764 idiosyncratic
# quantities kept AS a pool, observed for future overlap). Only
# UNIT-SAFE spelling variants merge (12 aliases in
# databank/dataset/key_aliases.json): plural/filler/unit-spelling
# differences of the SAME unit. Cross-unit (um vs nm) and
# digit-parameterized keys (ph2/ph3, formula_2) never merge — those
# digits carry conditions, and a merge would corrupt values.

_KEY_FAMILY_PATTERNS = [
    (
        "composition",
        r"composition|content|concentration|ratio|formula|elemental|stoichi",
    ),
    (
        "instrument",
        r"laser|voltage|current|power|detector|resolution|exposure|wavelength|source|excitation|spectrometer|diffractometer|grating",
    ),
    ("peaks", r"peak|band|shift|line|mode|assign|wavenumber|frequenc|2theta|dspacing"),
    ("conditions", r"temperature|pressure|time|duration|humidit|atmosphere|ph\b|rate"),
    (
        "sample",
        r"sample|specimen|prep|synthesi|anneal|sinter|deposit|coating|substrate|particle|grain",
    ),
    ("structure", r"crystal|lattice|space_group|phase|structure|cell|symmetry"),
]


def key_family(key: str) -> str:
    """The semantic family a key belongs to, '' when bespoke."""
    low = key.lower()
    for fam, pat in _KEY_FAMILY_PATTERNS:
        if re.search(pat, low):
            return fam
    return ""


async def load_key_aliases(effects) -> dict:
    """alias -> canonical spelling, {} when the map is absent."""
    try:
        fc = await effects.read_file("databank/dataset/key_aliases.json")
        if getattr(fc, "exists", False):
            return json.loads(fc.content)
    except Exception:  # noqa: BLE001 — canonicalization is best-effort
        pass
    return {}


def canonicalize_pack_keys(data: dict, aliases: dict) -> dict:
    """Rename aliased keys to their canonical spellings; merge lists on
    collision, first-value-wins otherwise."""
    if not aliases:
        return data
    out: dict = {}
    for k, v in data.items():
        ck = aliases.get(k, k)
        if ck in out:
            if isinstance(out[ck], list) and isinstance(v, list):
                out[ck] = out[ck] + [x for x in v if x not in out[ck]]
        else:
            out[ck] = v
    return out


def update_key_registry(registry: dict, data: dict, paper_key: str) -> dict:
    """Fold an ACCEPTED pack into the registry (returns the same dict).

    Deterministic bookkeeping only — descriptions come from the first
    exemplar; near-duplicate merging is a later, separate pass.
    """
    for key, value in data.items():
        entry = registry.get(key)
        if entry is None:
            exemplar = json.dumps(value, ensure_ascii=False)
            fam = key_family(key)
            registry[key] = {
                "type": _type_name(value),
                "description": "",
                "exemplar": exemplar[:120],
                "count": 1,
                "first_paper": paper_key,
                "similar_to": [],
                # tier: promoted to "core" by count in later passes; new
                # keys start as their family or in the observed pool.
                "tier": "family" if fam else "bespoke-pool",
                **({"family": fam} if fam else {}),
            }
        else:
            entry["count"] = int(entry.get("count") or 0) + 1
            # WIDEN, don't drift. _types_compatible already lets a paper with
            # several values send list[T] where the registry says T, so the
            # pack passes -- but the entry kept saying T forever and the
            # registry stopped describing its own data. Worse, a model reading
            # the registry block sees a scalar and coins the plural spelling
            # instead, which is how 19 duplicate key pairs were manufactured
            # (spectrometer_model/spectrometer_models). A container is the
            # honest slot: list[T] holds one value, T cannot hold several.
            actual = _type_name(value)
            if actual == f"list[{entry.get('type')}]":
                from datetime import datetime, timezone

                entry["type"] = actual
                entry["widened_at"] = datetime.now(timezone.utc).date().isoformat()
    return registry


def coinage_verdict(registry: dict, data: dict) -> dict:
    """Should this pack's NEW keys be folded into the registry, or held?

    Pure: the thresholds above against the pack's key set and the
    registry's maturity. Returned fields ride on pack_quality.
    """
    new = [k for k in data if k not in registry]
    reused = [k for k in data if k in registry]
    shared = sum(
        1
        for e in registry.values()
        if isinstance(e, dict) and int(e.get("count") or 0) >= 2
    )
    mature = shared >= COINAGE_MATURE_SHARED_KEYS
    excessive = len(new) > COINAGE_MAX_NEW and len(new) > COINAGE_RATIO * len(reused)
    held = mature and excessive
    if held:
        rule = (
            f"{len(new)} new keys > {COINAGE_MAX_NEW} and > {COINAGE_RATIO:g}x "
            f"{len(reused)} reused; registry mature ({shared} shared keys)"
        )
    elif excessive:
        rule = f"excessive coinage tolerated: registry green ({shared} shared keys)"
    else:
        rule = ""
    return {
        "coinage_quarantined": len(new) if held else 0,
        "coinage_rule": rule,
        "registry_shared_keys": shared,
        "_new": new,
        "_reused": reused,
    }


async def _load_quarantine(effects) -> dict:
    fc = await effects.read_file(KEY_QUARANTINE_PATH)
    if not getattr(fc, "exists", False) or not fc.content.strip():
        return {}
    try:
        data = json.loads(fc.content)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def fold_pack_into_registry(
    effects, registry: dict, data: dict, paper_key: str
) -> dict:
    """Fold an accepted pack into the registry under the coinage guard.

    Held keys go to KEY_QUARANTINE_PATH with exemplar, first paper and a
    count that grows when another paper coins the same key -- a second
    independent coinage is evidence of real vocabulary, which is what the
    review tool promotes on. Reused keys always count. Saves the registry.
    """
    from datetime import datetime, timezone

    v = coinage_verdict(registry, data)
    new, reused = v.pop("_new"), v.pop("_reused")
    if v["coinage_quarantined"]:
        update_key_registry(registry, {k: data[k] for k in reused}, paper_key)
        q = await _load_quarantine(effects)
        stamp = datetime.now(timezone.utc).isoformat()
        for k in new:
            e = q.get(k)
            if not isinstance(e, dict):
                q[k] = {
                    "type": _type_name(data[k]),
                    "exemplar": json.dumps(data[k], ensure_ascii=False)[:120],
                    "count": 1,
                    "first_paper": paper_key,
                    "papers": [paper_key],
                    "quarantined_at": stamp,
                }
            else:
                if paper_key not in (e.get("papers") or []):
                    e["count"] = int(e.get("count") or 0) + 1
                    e.setdefault("papers", []).append(paper_key)
        await effects.write_file(
            KEY_QUARANTINE_PATH, json.dumps(q, indent=1, ensure_ascii=False)
        )
        logger.warning(
            "coinage guard: %s held %d new keys out of the registry (%s)",
            paper_key,
            len(new),
            v["coinage_rule"],
        )
    else:
        update_key_registry(registry, data, paper_key)
        if v["coinage_rule"]:
            logger.info("coinage guard: %s -- %s", paper_key, v["coinage_rule"])
    await _save_registry(effects, registry)
    return v


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


# ── Stage constants ───────────────────────────────────────────────────

FIG_BATCH_SIZE = 3
# Figures, not papers, are what the dispatch actually spends. At the endpoint's
# measured ~37s/figure this budget is ~37 min against FIG_TIMEOUT_S=3600, which
# leaves headroom for a slow figure without letting three figure-heavy papers
# (this corpus has 54, 53 and 40) walk the batch past its own timeout.
FIG_BATCH_FIGURES = 60
FIG_TIMEOUT_S = 3600
CURATE_SESSION_TTL = 1800  # a paper's review+pack passes span many minutes

FIG_GOAL_SIGNATURE = "corpus-fig-review"
CURATE_GOAL_SIGNATURE = "corpus-curate"

_FIG_TOOL_PY = "tools/fig_review/.venv/bin/python"
_FIG_TOOL_SCRIPT = "tools/fig_review/fig_review.py"

# WHO READS THE FIGURES. Default llmvp: the fleet server's /v1/vision, i.e.
# whatever model the active config serves. That settles the open FIG_MODEL
# question by removing it — model choice belongs to LLMVP's config, not to a
# constant in a curation action.
#
# The retired constant was "mlx-community/Qwen3-VL-8B-Instruct-8bit", marked
# "M6's vision bake-off decides the production model; mid-size default". That
# bake-off ran on 2026-08-11 and ANSWERED it: the whole Qwen3-VL MLX family
# (4B/8B/30B-A3B) went in with every other mmproj-bearing model in the initial
# pass and was dominated on BOTH speed and transcription quality, so it never
# reached the 10-figure final. muse-glimmer-30b won that final at 145/192, and
# scored 155/192 re-measured through this endpoint. The incumbent was not
# untested — it was beaten, and had simply never been replaced.
#
# `mlx` keeps the private mlx_vlm.server child, which needs no server running
# and reaches MLX-only models; it then needs --model again.
FIG_BACKEND_DEFAULT = "llmvp"
# NO DEFAULT MLX MODEL, deliberately. It used to be Qwen3-VL-8B-8bit; that
# family was retired after losing the bake-off and its 45GB of weights were
# deleted on 2026-08-12. Keeping the name here would make the mlx opt-in
# silently re-download 9.2GB of a beaten model. Name one explicitly instead —
# fig_review fails fast and says so when the mlx backend has no --model.
FIG_MODEL = ""


def _fig_backend() -> str:
    """Read at call time, not import time, so a station's env actually
    reaches an already-imported module (and tests can flip it)."""
    import os

    return os.environ.get("OUROBOROS_FIG_BACKEND", FIG_BACKEND_DEFAULT)


def _fig_mlx_model() -> str:
    """The MLX model for the opt-in path — env only, no baked-in default."""
    import os

    return os.environ.get("OUROBOROS_FIG_MLX_MODEL", FIG_MODEL)


def _active_text_model() -> str:
    """The LLMVP config name serving this run (per-paper provenance —
    the denial second-opinion pass runs a different model over the same
    corpus, so 'which model said this' must live on the record)."""
    import os

    try:
        # Sanctioned raw read: repo-level server config OUTSIDE the workspace
        # root — the workspace-scoped effects seam cannot reach it by design.
        path = os.path.join(_repo_root(), "llmvp", "active_config.txt")
        return open(path).read().strip() or "unknown"
    except OSError:
        return "unknown"


def _provenance_model(effects) -> str:
    """Which model actually reviewed and packed this paper. A lane routed to
    another engine (ChildEffects.inference_domain -> llmvp_domains[domain])
    ran on THAT domain's model; only a lane on the default server ran on the
    config named in llmvp/active_config.txt. Before this, every pack a remote
    lane produced was stamped with the LOCAL server's config."""
    domain = str(getattr(effects, "_inference_domain", "") or "")
    routes = getattr(effects, "_llmvp_domains", None) or {}
    if domain and isinstance(routes, dict):
        model = str((routes.get(domain) or {}).get("model") or "")
        if model:
            return model
    return _active_text_model()


def _prompts_dir():
    import os

    return os.path.join(_repo_root(), "prompts")


# EXTRACTION STATES THIS STAGE WILL CONSUME.
#
# `extract_unverified` is here by operator decision (2026-08-14). It means OCR
# produced a document but the source PDF has NO TEXT LAYER to check it against
# — which is the scanned-paper case OCR exists for. Verification cannot ever
# succeed there: there is nothing to compare with, and re-running produces the
# same unverifiable result, so holding these forever only guaranteed they were
# never looked at.
#
# The curator is a competent judge of them because its own gate does not depend
# on the missing text layer: `grounding_check` matches the extracted data
# against the CURATOR DOC — the markdown itself — so it works identically on a
# scan. What it cannot detect is an OCR that hallucinated the scan wholesale
# and reads plausibly; that residual risk is the reason the STATUS IS NOT
# REWRITTEN. A promoted paper keeps `extract_unverified`, so an audit can
# always separate "machine-verified against a publisher text layer" from
# "admitted on curator judgement alone".
#
# extract_lingual joined 2026-08-29 (post-acceptance translation, executing
# the 2026-08-22 lane-closure design): the curator reviews non-English
# originals accurately — all 22 untranslated non-en denials were substantive
# content verdicts — so lingual papers pay the acceptance tax FIRST and only
# the accepted ones spend translate seats (_translation_pending gates on
# review_status == "accepted"). The status stays extract_lingual through
# review; the translation drain books it to "extracted" + writes the .en.md
# beside the source, which _build_doc_for already prefers by convention.
_EXTRACTION_USABLE = ("extracted", "extract_unverified", "extract_lingual")


def _fig_pending(record: dict) -> bool:
    """A record the fig-review pass still owes work to."""
    return (
        record.get("extraction_status") in _EXTRACTION_USABLE
        and int(record.get("figure_count") or 0) > 0
        and record.get("figtext_status") not in ("figtext_done", "figtext_failed")
    )


def _figtext_ready(record: dict) -> bool:
    """Fig pass terminal for this record (done, failed, or no figures)."""
    return int(record.get("figure_count") or 0) == 0 or record.get(
        "figtext_status"
    ) in ("figtext_done", "figtext_failed")


def _curation_pending(record: dict) -> bool:
    """A record the curate pass still owes work to.

    Terminal review states: denied, review_failed. An accepted paper
    stays pending until its pack reaches packed | pack_failed.
    """
    if record.get("extraction_status") not in _EXTRACTION_USABLE:
        return False
    if not _figtext_ready(record):
        return False
    review = record.get("review_status") or ""
    if review in ("denied", "review_failed"):
        return False
    if review == "accepted":
        return record.get("pack_status") not in ("packed", "pack_failed")
    return True  # review not yet run


# ── Drain backlog: the gate's admission check ─────────────────────────


async def action_check_drain_backlog(step_input):
    """How much unfinished pipeline work is waiting, by stage.

    WHY THE GATE NEEDS THIS. The research gate asks "is the corpus
    covered?" — a question whose answer is only stable once the pipeline
    has drained. Asked while thousands of papers are still queued it
    reports a shortfall, `harvest_research_findings` reopens the aspect,
    discovery finds nothing new (its queries are exhausted), the gate is
    re-asked, and the controller spins. Measured 2026-09-01: 1,479
    gate_fails against 96 curate rounds, both engines idle, two packs in
    six hours — the lanes had 1,548 papers ready and could not get the
    event loop.

    So the gate is treated as a JOB that must be ADMITTED, exactly as a
    worker lane is admitted against free seats (scheduler/capacity_model).
    A lane waits for capacity; the gate waits for the backlog to clear.
    The difference from a plain retry cap is that this is not a guess
    about how many failures are too many — it is the actual condition
    under which the gate's answer means anything.

    Counts reuse the drains' OWN pending predicates rather than
    re-deriving them, so "pending" cannot drift between the lane that
    does the work and the gate that waits for it.

    Context: none. Result: backlog, by_stage, admitted.
    """
    from agent.actions.extraction_actions import (
        _extraction_pending,
        _translation_pending,
    )
    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepOutput

    effects = step_input.effects
    try:
        databank = await read_databank(effects)
    except Exception as e:  # noqa: BLE001 — an unreadable databank must not
        # wedge the controller; admit the gate and let it decide.
        logger.warning("drain backlog unreadable (%s) — admitting the gate", e)
        return StepOutput(
            result={"backlog": 0, "by_stage": {}, "admitted": True},
            observations="drain backlog unreadable; gate admitted",
        )

    by_stage = {
        "extract": 0,
        "figtext": 0,
        "curate": 0,
        "translate": 0,
    }
    for rec in databank.values():
        if _extraction_pending(rec):
            by_stage["extract"] += 1
        if _fig_pending(rec):
            by_stage["figtext"] += 1
        if _curation_pending(rec):
            by_stage["curate"] += 1
        if _translation_pending(rec):
            by_stage["translate"] += 1
    backlog = sum(by_stage.values())
    admitted = backlog == 0
    detail = ", ".join(f"{k} {v}" for k, v in by_stage.items() if v)
    return StepOutput(
        result={
            "backlog": backlog,
            "by_stage": by_stage,
            "admitted": admitted,
        },
        observations=(
            f"drain backlog {backlog} ({detail}) — gate stands down"
            if backlog
            else "pipeline drained — gate admitted"
        ),
    )


async def _load_registry(effects) -> dict:
    fc = await effects.read_file(KEY_REGISTRY_PATH)
    if not getattr(fc, "exists", False) or not fc.content.strip():
        return {}
    try:
        data = json.loads(fc.content)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning("key_registry.json unparseable — starting fresh")
        return {}


async def _save_registry(effects, registry: dict) -> None:
    await effects.write_file(
        KEY_REGISTRY_PATH, json.dumps(registry, indent=1, ensure_ascii=False)
    )


async def _load_figtext(effects, paper_key: str) -> dict | None:
    fc = await effects.read_file(f"{FIGTEXT_DIR}/{paper_key}.json")
    if not getattr(fc, "exists", False):
        return None
    try:
        data = json.loads(fc.content)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def _figtext_model(effects, paper_key: str) -> str:
    """Which VLM actually read THIS paper's figures.

    Read from the sidecar rather than assumed from config: under the llmvp
    backend the caller does not choose the model — LLMVP's active config
    does — so a constant here would record a guess. Papers processed before
    the backend changed keep the model that really read them, which is the
    whole point of provenance.
    """
    figtext = await _load_figtext(effects, paper_key)
    served = str((figtext or {}).get("model") or "").strip()
    # "unknown" beats an empty string in a dataset envelope, which would read
    # as "no VLM involved" rather than "the sidecar did not say".
    return served or "unknown"


# ── Actions: goals + sweeps ───────────────────────────────────────────


async def action_derive_curation_goals(step_input):
    """Bootstrap the two corpus-level goals (idempotent by signature).

    fig_review sweeps VLM figure readings; curate reviews + packs each
    paper. The databank is the plan — a databank without extracted
    papers means this flow set has nothing to do.
    """
    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepOutput
    from agent.persistence.models import GoalRecord

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or not effects:
        return StepOutput(
            result={"goals_ready": False},
            observations="No mission/effects — cannot derive curation goals",
        )

    databank = await read_databank(effects)
    extracted = sum(
        1 for r in databank.values() if r.get("extraction_status") in _EXTRACTION_USABLE
    )
    if not extracted:
        return StepOutput(
            result={"goals_ready": False, "created": 0},
            observations=(
                "Databank has no extracted papers — run an extractor mission first"
            ),
        )

    created = 0
    existing = {g.finding_signature for g in mission.goals}
    if FIG_GOAL_SIGNATURE not in existing:
        mission.goals.append(
            GoalRecord(
                description="VLM figure readings (figtext) for every extracted paper",
                type="fig_review",
                status="incomplete",
                finding_signature=FIG_GOAL_SIGNATURE,
            )
        )
        created += 1
    if CURATE_GOAL_SIGNATURE not in existing:
        mission.goals.append(
            GoalRecord(
                description=(
                    f"Review + pack every extracted paper ({extracted} in the databank)"
                ),
                type="curate",
                status="incomplete",
                finding_signature=CURATE_GOAL_SIGNATURE,
            )
        )
        created += 1
    if created:
        await effects.save_mission(mission)
    return StepOutput(
        result={"goals_ready": True, "created": created},
        observations=f"Curation goals ready ({extracted} extracted papers)",
    )


def _fig_batch(databank: dict) -> list[str]:
    """The next batch, budgeted by FIGURES rather than papers.

    A paper-count batch was safe against an 8B MLX model at a few seconds a
    figure. It is not safe against the endpoint: muse reads a figure in ~16s
    on this rig (measured 2026-08-16, afc65ec, once the vision projector moved
    to CUDA1; it was ~37s on the M1, and that stale constant over-budgeted
    figure batches by more than 2x). This corpus has papers with 54, 53 and 40
    figures — three of those in one dispatch still approaches the 3600s
    timeout, and the batch would die mid-flight with every paper in it booking
    figtext_failed, having done the work.

    So the unit of work is the figure, which is what actually costs time.
    FIG_BATCH_SIZE still caps the paper count (a batch of many tiny papers
    stays bounded), and a single paper over the figure budget is ALWAYS
    dispatched alone rather than skipped — it must still get its turn.
    """
    pending = sorted(k for k, r in databank.items() if _fig_pending(r))
    batch: list[str] = []
    figures = 0
    for key in pending:
        n = int((databank.get(key) or {}).get("figure_count") or 0)
        if batch and (figures + n > FIG_BATCH_FIGURES or len(batch) >= FIG_BATCH_SIZE):
            break
        batch.append(key)
        figures += n
    return batch


# ── figtext drain (parallel-branch consumer) ──────────────────────────
#
# Same claim discipline as the OCR drain (extraction_actions._OCR_CLAIMS):
# figtext_status is only booked AFTER the tool runs, so a drain branch and
# a curator fig_review dispatch selecting concurrently would read the same
# figures twice. In-process set; the multi-process claim is the same
# follow-on flock story. At the endpoint's measured ~37 s/figure the drain
# budget is FIGURES (default 6 ≈ ~4 min — one discovery round), and unlike
# _fig_batch a paper OVER the budget is SKIPPED here, not dispatched alone:
# an 86-minute figure-heavy paper belongs to a dedicated curator dispatch,
# never to a branch riding a discovery round.
_FIGTEXT_CLAIMS = ClaimSet("figtext")


def _figtext_drain_budget() -> int:
    return drain_budget("OUROBOROS_FIGTEXT_FIGS", 6)


def _figs_remaining(record: dict) -> int:
    """Undescribed figures on this record, from booked partial progress."""
    total = int(record.get("figure_count") or 0)
    prog = str(record.get("figtext_progress") or "")
    if "/" in prog:
        try:
            return max(0, total - int(prog.split("/")[0]))
        except ValueError:
            return total
    return total


def select_figtext_batch(databank: dict, max_figures: int) -> list[str]:
    """Claimed, figure-budgeted selection — REMAINING-aware.

    Papers in-progress finish first, then fewest-remaining. Whole papers
    pack greedily into the budget; when none fits whole, the head paper is
    taken ALONE and the tool's --max-figures pool caps the round — big
    papers make progress across rounds instead of being skipped forever
    (693 papers at 13+ figures were structurally unreachable under the
    old skip rule).

    The packing rule itself now lives in drain_lane.select_cost_bounded,
    shared with the OCR drain; what stays here is what is actually about
    figures — the pending predicate, the cost, and the finish-first order.
    """
    return select_cost_bounded(
        databank,
        budget=max_figures,
        pending=_fig_pending,
        cost=lambda k, r: _figs_remaining(r),
        sort_key=lambda k, r: (
            0 if "/" in str(r.get("figtext_progress") or "") else 1,
            _figs_remaining(r),
            k,
        ),
        claims=_FIGTEXT_CLAIMS,
    )


def release_figtext_keys(keys: list[str]) -> None:
    _FIGTEXT_CLAIMS.release(keys)


async def action_figtext_drain_batch(step_input):
    """Describe a bounded, claimed slice of undescribed figures — the
    figtext_drain flow's one work step, built to ride as a parallel branch.

    Delegates to action_fig_review_batch (the bake-off-validated
    /v1/vision pipeline); muse vision runs on its own vision contexts, so
    this consumes NO batched text seats (measured vision+text
    serialization 0.068). Preflights the tool venv: a missing interpreter
    DECLINES the round instead of booking figtext_failed on papers the
    tool never saw.

    Inputs: working_directory. Result: attempted_papers, figures, done,
    failed, reason.
    """
    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepOutput

    effects = step_input.effects
    budget = _figtext_drain_budget()

    def _decline(reason: str) -> StepOutput:
        return decline(
            reason,
            summary_key="figtext_summary",
            counters={"attempted_papers": 0, "figures": 0},
        )

    if budget <= 0:
        return _decline("disabled")
    if effects is None:
        return _decline("no effects")
    tool_py = os.path.join(_repo_root(), _FIG_TOOL_PY)
    if not os.path.isfile(tool_py):
        return _decline("fig_review venv missing")

    databank = await read_databank(effects)
    keys = select_figtext_batch(databank, budget)
    if not keys:
        return _decline("nothing unclaimed pending")
    figures = min(budget, sum(_figs_remaining(databank.get(k) or {}) for k in keys))
    try:
        sub = step_input.model_copy(
            update={
                "inputs": {
                    **dict(step_input.inputs or {}),
                    "paper_keys": keys,
                    # The tool banks per-figure readings and resumes, so the
                    # budget is a round PACER, not an eligibility wall.
                    "max_figures": budget,
                }
            }
        )
        out = await action_fig_review_batch(sub)
        result = dict(out.result or {})
    finally:
        release_figtext_keys(keys)
    summary = {
        "attempted_papers": len(keys),
        "figures": figures,
        "done": result.get("done", 0),
        "failed": result.get("failed", 0),
        "partial": result.get("partial", 0),
    }
    return StepOutput(
        result=summary,
        observations=(
            f"figtext drain: {figures} figure(s) across {len(keys)} paper(s) — "
            f"{summary['done']} done, {summary['partial']} partial, "
            f"{summary['failed']} failed"
        ),
        context_updates={"figtext_summary": summary},
    )


# ── The curate drain: whole-paper review+pack on an idle text seat ───
#
# WHY STATELESS. The dispatch curator runs a memoryful session (ingest →
# review → pack in one KV lineage). A drain branch instead re-sends the doc
# per turn: the context PEAK is one doc + one prompt + one answer, never the
# accumulated session — which is what lets whole-paper work fit a shared
# batched cell at all. The doc prefix is identical across the two turns, so
# prefix reuse recovers most of the re-prefill when the engine offers it.

_CURATE_CLAIMS: set[str] = set()
#: paper_key -> (raw_chars, floor_chars). BOTH ARE BUDGET-INDEPENDENT.
#: The old cache stored the size of a doc built FOR ONE BUDGET, which under a
#: per-round budget is wrong in both directions: a doc compressed for a small
#: budget caches small and looks eligible forever, and a doc measured under a
#: large budget never learns it could compress under a small one. Keying the
#: cache by budget instead would rebuild ~500-1,100 compressed docs per round
#: in an executor, on the same box as the GPU.
_CURATE_DOC_CACHE: dict[str, tuple[int, int]] = {}
#: paper_key -> selection rounds this paper was pending but over budget.
#: In-process on purpose: it rides the same lifetime as _CURATE_CLAIMS and
#: _CURATE_DOC_CACHE, and a mission run sees hundreds of rounds (400+ idle
#: rounds per lane observed), so aging fires well inside one run without
#: adding a sidecar write per round.
_CURATE_STARVED: dict[str, int] = {}
# Papers booked TERMINAL by this process (denied, review_failed, packed,
# pack_failed), keyed to WHEN. The claim set guards work in flight; once a
# round books and releases, the only guard left is the on-disk status -- and a
# sibling lane's `databank` argument is a snapshot it read seconds earlier,
# before that booking landed. Measured 2026-09-01: twice in one hour a lane
# selected a paper 4-8 s BEFORE its sibling's round ended, on a snapshot taken
# before the sibling booked, and curated it again (one pack record overwritten
# by the second run). Selection skips anything booked here regardless of what
# the snapshot says -- for _CURATE_BOOKED_SHADOW_S, not for the life of the
# process.
#
# WHY TIMED. The first cut was process-lifetime, reasoning that "a terminal
# booking is never undone by the pipeline, only by hand" -- and by hand is
# exactly what happened. Measured 2026-09-05: a paper booked pack_failed
# (envelope: missing title) 77 min into a run had its title filled and was
# re-armed needs_repack on disk; it was the SMALLEST eligible paper in the
# queue (8.4k-token floor) and every lane skipped it for two hours while
# reviewing 10-50k papers around it, because this set still held it. A
# long-running mission is exactly where fixes land while it runs. The race
# this guards is seconds wide; the shadow is 120 s, fifteen times the
# measured window, after which the on-disk status is trusted again.
_CURATE_BOOKED: dict[str, float] = {}
_CURATE_BOOKED_SHADOW_S = 120.0


def _recently_booked(key: str) -> bool:
    """Is `key` still inside the post-booking shadow? Expired entries are
    dropped here so the map cannot grow with every booking a run makes."""
    at = _CURATE_BOOKED.get(key)
    if at is None:
        return False
    if time.monotonic() - at < _CURATE_BOOKED_SHADOW_S:
        return True
    _CURATE_BOOKED.pop(key, None)
    return False


#: Rounds over budget before a paper is promoted to the head of its tier.
_CURATE_AGING_ROUNDS = 25

# Seat-budget derivation (tokens), against the live shared cell:
#   static prefix 1,765 + three sibling lanes at their measured p95
#   (3 x ~6.8k) + review answer 4k + pack answer 8k + prompt bodies ~1.5k.
# Everything left is doc room. Chars/token ~3.3 measured on this corpus's
# admitted markdown (18.25M muse tokens over ~60MB text).
# DEGRADED RUNG ONLY. This number bundles "three sibling lanes at their
# measured p95" into a static reserve — a stand-in for free cells from
# before free cells were knowable. It is correct on the degraded rung
# BECAUSE degradation collapses the pool to DEGRADED_WIDTH = 1
# (capacity_model.py): with no signal exactly one unit runs, so sizing
# against the whole configured cell is not oversubscription. On the live
# rungs it would double-charge for siblings that free_cells already counts.
_CURATE_DEGRADED_RESERVE_TOKENS = 36_000
_CURATE_CHARS_PER_TOKEN = 3.3
# SCRIPT-AWARE SIZING. 3.3 chars/token is a LATIN ratio. CJK text runs
# ~1.4 chars/token (back-solved from the 2026-08-26 incident: a 139,705-char
# doc at 49% CJK admitted at 70,868 tokens against a 65,536-token seat), so a
# char-only fit test under-counts a Japanese doc ~2.4x. One such paper — a
# museum-programming survey mis-tagged as LIBS — burned 208 of 419 curate
# rounds in half a day: selected (fits by chars), refused at admission
# (over-seat in tokens), declined as a transient, re-selected. Sizes, ladder
# fits and claim reservations therefore all go through _estimate_doc_tokens;
# comparisons stay in char units via _effective_chars (tokens x 3.3), so a
# Latin doc sizes exactly as before.
_CURATE_CJK_CHARS_PER_TOKEN = 1.4
_CURATE_CYRILLIC_CHARS_PER_TOKEN = 2.2  # estimate; refine when measured
_CJK_CHAR_RE = re.compile(r"[\u3000-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")
_CYRILLIC_CHAR_RE = re.compile(r"[\u0400-\u04ff]")

# Mirrors the engine's per-stream seat (n_ctx_seq). Read ONLY by the
# oversize park rule; a wrong value cannot corrupt anything — too small
# parks papers a human can un-park (clear the status), too large leaves the
# admission-fault backstop in the drain to catch what selection lets through.
_CURATE_SEAT_TOKENS = int(os.environ.get("OUROBOROS_CURATE_SEAT_TOKENS", "") or 65_536)
# Park only when the floor is CLEARLY over the seat. A corpus dry-run
# (2026-08-26, 155 pending) found ~100 papers with floors over the seat —
# mostly big Latin theses/monographs already starving silently under the
# char model. For docs within estimator noise of the boundary, a wrong park
# loses a paper while a wrong starve merely keeps the status quo — so the
# unsure band starves (recoverable any time budgets or geometry grow) and
# only the sure band parks. Same authority principle as pre-OCR triage:
# never a terminal verdict on an unsure judgement.
_CURATE_OVERSIZE_PARK_MARGIN = 1.1

# The engine's admission refusal for a prompt larger than one seat. This
# text arriving as a transport fault is DETERMINISTIC — replaying the same
# doc refuses the same way — so it must never take the decline-and-reselect
# path built for transients (which is otherwise correct: every one-time
# transport faulter from the 2026-08-25 run was later accepted).
_CURATE_OVERSIZE_FAULT_MARKER = "exceeds the model's per-stream context limit"


def _estimate_doc_tokens(text: str) -> int:
    """Script-aware token estimate for a curator doc."""
    cjk = len(_CJK_CHAR_RE.findall(text))
    cyr = len(_CYRILLIC_CHAR_RE.findall(text))
    other = len(text) - cjk - cyr
    return int(
        cjk / _CURATE_CJK_CHARS_PER_TOKEN
        + cyr / _CURATE_CYRILLIC_CHARS_PER_TOKEN
        + other / _CURATE_CHARS_PER_TOKEN
    )


def _effective_chars(text: str) -> int:
    """Doc size in the char units a char-denominated budget compares against.

    For Latin text this equals len(text) to within rounding; for CJK-heavy
    text it inflates to the doc's true token cost x 3.3.
    """
    return int(_estimate_doc_tokens(text) * _CURATE_CHARS_PER_TOKEN)


# Non-doc cost of ONE curate turn, in tokens. The review and pack turns are
# SEQUENTIAL and stateless, so the peak is one of them: the pack answer
# (8,192 — the ENGINE charges max_tokens at admission, not what is actually
# generated), the pack prompt with its key-registry block, and the tokenizer
# margin the client cannot measure.
_CURATE_TURN_OVERHEAD_TOKENS = 14_000
_CURATE_MIN_DOC_TOKENS = 4_000


class _CurateTransportFault(Exception):
    """Server/transport failure — decline the round, book NOTHING."""


def _curate_drain_budget() -> int:
    raw = os.environ.get("OUROBOROS_CURATE_PAPERS", "").strip()
    try:
        return max(0, int(raw)) if raw else 1
    except ValueError:
        return 1


async def _curate_doc_budget_chars(effects, claim_tokens: int = 0) -> int:
    """Doc budget for THIS lane, THIS round, sized against LIVE free cells.

    Rung 1  operator override (env)
    Rung 2  the dispatcher's reservation-adjusted claim   <- the normal path
    Rung 2b the lane's REMOTE seat (llmvp_domains[domain].seat_tokens)
    Rung 3  a live capacity snapshot  (single-lane callers, no worker pool)
    Rung 4  the legacy static cell    (feed degraded => width 1 => sole consumer)

    0 means "don't run" — never "run something smaller than a paper".

    WHY NOT kvPoolTokens. That field is the STATIC CONFIGURED n_ctx
    (llama_cpp_backend: info["kv_pool_tokens"] = int(n_ctx)), not what is
    free. Every curate lane therefore computed the same budget as though it
    were the only consumer, and four lanes oversubscribed the pool ~4x. That
    is what produced the 31,244-token prompt behind the 2026-08-24/25 wedge.

    n_ctx_seq is load-bearing, not decoration: a SEAT's window can be smaller
    than the pool, and the engine rejects a prompt against the seat
    (_admit: "Prompt (N tokens) exceeds context window"). "Up to the max
    context" is bounded by the seat, not the cell.
    """
    raw = os.environ.get("OUROBOROS_CURATE_DOC_CHARS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass

    cells = int(claim_tokens or 0)  # rung 2

    # Rung 2b: a lane whose inference runs on ANOTHER engine. It holds no
    # local claim (the worker pool does not admit it against the local
    # model), so without this rung it fell to rung 3 and sized its document
    # against the LOCAL server's free cells -- cells the five local lanes had
    # already spent. Measured 2026-09-01: 40 of 46 remote rounds declined
    # "nothing unclaimed fits the seat budget" while the remote engine sat
    # idle. The remote seat is whatever the domain declares (`seat_tokens`,
    # the engine's per-stream window -- for kv_unified pools the health
    # field nCtxSeq reports the POOL and cannot be used); undeclared, it is
    # assumed to be shaped like ours.
    domain = str(getattr(effects, "_inference_domain", "") or "")
    if cells <= 0 and domain:
        routes = getattr(effects, "_llmvp_domains", None) or {}
        route = routes.get(domain) if isinstance(routes, dict) else None
        seat = int((route or {}).get("seat_tokens") or 0) or _CURATE_SEAT_TOKENS
        cells = seat

    if cells <= 0:  # rung 3
        snap = None
        fn = getattr(effects, "capacity_snapshot", None)
        if fn is not None:
            try:
                snap = await fn()
            except Exception:  # noqa: BLE001 — capacity never breaks a caller
                snap = None
        if snap is not None and getattr(snap, "knows_kv", False):
            # Parked, latched, or head-blocked: adding load cannot help, and
            # a queued oversized prompt is what head-blocks everything else.
            if (
                not getattr(snap, "serving", True)
                or getattr(snap, "engine_fatal", None)
                or int(getattr(snap, "waiting", 0) or 0) > 0
            ):
                return 0
            free = int(getattr(snap, "free_cells", 0) or 0)
            seat = int(getattr(snap, "n_ctx_seq", 0) or 0)
            cells = min(free, seat) if seat else free
    if cells <= 0:  # rung 4
        try:
            pool = await effects.inference_pool_health()
        except Exception:  # noqa: BLE001 — unreachable server just declines
            return 0
        cell = int((pool or {}).get("kvPoolTokens") or 0)
        # Written so this rung reproduces the pre-2026-08-25 number exactly:
        # cell - 36,000 doc tokens, once the overhead below is subtracted.
        cells = cell - _CURATE_DEGRADED_RESERVE_TOKENS + _CURATE_TURN_OVERHEAD_TOKENS

    doc_tokens = cells - _CURATE_TURN_OVERHEAD_TOKENS
    if doc_tokens < _CURATE_MIN_DOC_TOKENS:
        return 0
    return int(doc_tokens * _CURATE_CHARS_PER_TOKEN)


# ── coverage priority ────────────────────────────────────────────────
#
# THE THIN BINS, measured over the 995 packed papers (2026-08-23):
# LIBS appears in 6% of the accepted corpus and Raman/FTIR-on-heritage
# in 31%, against XRD 50% and microscopy 58%. Both thin bins are the
# best-represented aspects in the PENDING pool (172 LIBS, 112 heritage
# of 546) — i.e. the marginal pending paper closes a real gap while the
# average one deepens a bin that is already thick.
#
# Two aspect-naming generations coexist on records (the goal-phrase form
# and a later snake_case form); both map here, because a record written
# under either must sort the same.
_PRIORITY_ASPECTS = frozenset(
    {
        "libs mineral spectra",
        "emission_spectroscopy",
        "raman ftir cultural heritage",
        "vibrational_spectroscopy",
        # Foundations goals (2026-08-29): technique physics + mineral
        # formation/chemistry — added to help the trained model generalize
        # (the OLMo probe found polymorphs unsolved for want of INPUT, not
        # capacity), so their papers should be worked early, not queued
        # behind the survey bins.
        "spectroscopy technique physics",
        "mineral formation and chemistry",
    }
)


#: Technique-level mirror of _PRIORITY_ASPECTS, for content bins the
#: pre-OCR triage assigns from the page itself.
_PRIORITY_TECHNIQUES = frozenset({"libs", "raman", "ftir"})


def _aspect_priority(record: dict) -> int:
    """0 = closes a thin coverage bin, 1 = everything else.

    Deliberately NOT starvation-free, unlike the lane fairness rule: the
    priority pool is finite and drains, after which every paper is tier 1
    again. Retune by re-measuring bin coverage, not by taste.
    """
    # THE CONTENT BIN WINS WHEN PRESENT. `source_aspects` records which
    # search query FOUND the paper, which is not the same claim as what the
    # paper is about — measured, it binned a coffee-classification study and
    # a single-cell Raman imaging paper into mineral spectroscopy. The
    # pre-OCR triage read the actual first page, so its verdict is the better
    # evidence, exactly as extraction's `language` beats the catalog's.
    #
    # Absent (untriaged, or triage unavailable) falls through to the aspect,
    # so this is additive and a corpus with no triage behaves as before.
    bin_ = str(record.get("content_bin") or "").strip().lower()
    if bin_:
        if bin_ in _PRIORITY_TECHNIQUES:
            return 0
        if bin_ in ("off_topic", "review"):
            return 1
    aspects = record.get("source_aspects")
    if not isinstance(aspects, list):
        return 1
    for a in aspects:
        if str(a).strip().lower() in _PRIORITY_ASPECTS:
            return 0
    return 1


async def _curate_doc_sizes(effects, paper_key: str) -> tuple[int, int]:
    """(raw_chars, floor_chars) for one paper — independent of any budget.

    `raw` is the uncompressed curator doc; `floor` is the deepest rung of the
    compression ladder. Between them they answer both questions selection
    asks — "can this ever fit?" and "how big will it be at this budget?" —
    without the answer depending on the budget it was measured under.
    """
    raw = _effective_chars(await _build_doc_for(effects, paper_key, 0))  # raw form
    if raw <= 0:
        return 0, 0
    # An unmeetable budget forces the walk to the deepest rung.
    floor = _effective_chars(await _build_doc_for(effects, paper_key, 1))
    return raw, min(raw, floor)


def _largest_seat_tokens(effects) -> int:
    """The biggest per-stream seat any curate lane can run on: the local seat
    or the largest `seat_tokens` a declared inference domain advertises. The
    oversize park compares against THIS, so a paper is parked only when no
    lane at all could take it."""
    seats = [_CURATE_SEAT_TOKENS]
    routes = getattr(effects, "_llmvp_domains", None) or {}
    if isinstance(routes, dict):
        for route in routes.values():
            try:
                seats.append(int((route or {}).get("seat_tokens") or 0))
            except (TypeError, ValueError):
                continue
    return max(seats)


async def select_curate_paper(
    effects, databank: dict, budget_chars: int
) -> tuple[str, str]:
    """Claim the smallest fitting pending paper, thin-bin aspects first.

    Ordered by (coverage priority, doc chars, key). Smallest-first within
    a tier remains the throughput policy — the drain eats each tier from
    the short end — and papers over the budget are still left rather than
    truncated (the compression ladder in `_build_doc_for` is what gets
    most of them under it).
    """
    sized: list[tuple[int, int, int, int, str]] = []
    pack_only: set[str] = set()
    remote_lane = bool(getattr(effects, "_inference_domain", "") or "")
    local_usable_chars = int(
        (_CURATE_SEAT_TOKENS - _CURATE_TURN_OVERHEAD_TOKENS)
        * _CURATE_OVERSIZE_PARK_MARGIN
        * _CURATE_CHARS_PER_TOKEN
    )
    for key, rec in databank.items():
        if key in _CURATE_CLAIMS or _recently_booked(key):
            continue
        if not _curation_pending(rec):
            continue
        sizes = _CURATE_DOC_CACHE.get(key)
        if sizes is None:
            sizes = await _curate_doc_sizes(effects, key)
            _CURATE_DOC_CACHE[key] = sizes
        raw_chars, floor_chars = sizes
        # A floor over EVERY seat can never run at any budget: park it in a
        # visible review queue instead of letting it starve (or worse,
        # select-fault-reselect — the 2026-08-26 poison-pill loop).
        #
        # "Every seat" is the largest seat ANY lane can offer, not this
        # lane's. Measured 2026-09-02: 222 papers sat parked against the
        # 65k local seat while a declared 256k remote seat fit 210 of them
        # -- the remote lanes never saw those papers, because whichever lane
        # sized the doc first parked it for all of them. A doc that fits
        # some other lane's seat is skipped here (starved, aged like any
        # other over-budget paper) and left for the lane that fits.
        floor_tokens = int(floor_chars / _CURATE_CHARS_PER_TOKEN)
        largest_seat = _largest_seat_tokens(effects)
        over_every_seat = floor_chars > 0 and floor_tokens > int(
            (largest_seat - _CURATE_TURN_OVERHEAD_TOKENS) * _CURATE_OVERSIZE_PARK_MARGIN
        )
        if over_every_seat and rec.get("review_status") == "accepted":
            # PACK-ONLY OVER RAW TEXT. The review needs the whole document
            # under one seat; the pack does not -- _pack_windowed cuts
            # section-bounded windows, and a window is seat-independent. Until
            # now the pack windowed the COMPRESSED review doc, so an accepted
            # paper whose deepest compression still exceeded every seat was
            # parked as if it could never be judged. Measured 2026-09-06: six
            # spectroscopy-rich theses (278k-443k-token floors, 107-677
            # in-scope technique mentions) sat parked for exactly this reason.
            # An operator accept now routes them here: no review turn, windows
            # cut from the uncompressed curator doc, sorted LAST within the
            # lane's tier (the most expensive work there is) and never
            # remote-first (any lane can window raw; the local ones are faster).
            pack_only.add(key)
            sized.append((1, _aspect_priority(rec), 1, 10**12, key))
            continue
        if over_every_seat:
            await _book_curate_oversize(
                effects,
                key,
                f"curation: doc floor ~{floor_tokens:,} tokens exceeds the "
                f"{largest_seat:,}-token seat even at deepest "
                "compression; review by hand",
            )
            continue
        # Eligible if the DEEPEST compression fits; ordered by what this
        # budget will actually produce.
        if not (0 < floor_chars <= budget_chars):
            _CURATE_STARVED[key] = _CURATE_STARVED.get(key, 0) + 1
            continue
        chars = raw_chars if 0 < raw_chars <= budget_chars else floor_chars
        # Aging: a paper repeatedly passed over for being too big is promoted
        # to the head of its tier as soon as a claim covers it. Smallest-first
        # is still the throughput policy; this only bounds the tail's latency,
        # which is otherwise unbounded when discovery keeps feeding small docs.
        aged = 0 if _CURATE_STARVED.get(key, 0) >= _CURATE_AGING_ROUNDS else 1
        # A REMOTE lane takes the papers ONLY IT can take first. Smallest-first
        # hands every lane the same small paper, so the 256k remote lane spent
        # its rounds on documents the five local lanes could serve while the
        # tail only it can serve barely moved: measured 2026-09-06, 30 verdicts
        # in 4 h on local-band papers, ONE on a remote-only paper, 88 waiting.
        # Tier 0 = beyond the local seat; local lanes are always tier 1, so
        # their ordering is untouched, and a remote lane with no remote-only
        # work left falls through to the same smallest-first as everyone else.
        remote_first = 0 if (remote_lane and floor_chars > local_usable_chars) else 1
        sized.append((remote_first, _aspect_priority(rec), aged, chars, key))
    if not sized:
        return "", ""

    # CLAIM BEFORE THE AWAIT. ClaimSet's safety argument is "no awaits between
    # check and claim", and this function had many: the loop above awaits doc
    # sizing for every uncached key, and _build_doc_for awaits again, with
    # the claim taken only at the very end. Every selector that entered that
    # window computed the same smallest-fitting key and claimed it after the
    # fact. Measured 2026-09-01: four lanes (three remote, one local) started
    # within 24 s of each other on a cold size cache and all curated
    # doi_10.34321_22063 -- 2.5 lane-hours on a paper one lane finished in
    # seven minutes, booked five times, dataset record taken by whichever
    # finished last. The race was latent for the local lanes all along; the
    # remote lanes, launching together with identical budgets, made it
    # near-certain.
    #
    # So: re-check the claim set NOW, take the claim synchronously, and only
    # then build. A key claimed by a sibling during our awaits is skipped for
    # the next candidate rather than returned empty -- an empty return costs
    # the lane a 30 s idle backoff for a queue that has work in it.
    key = ""
    for _, _, _, _, cand in sorted(sized):
        if cand not in _CURATE_CLAIMS:
            _CURATE_CLAIMS.add(cand)
            key = cand
            break
    if not key:
        return "", ""
    if key in pack_only:
        return key, ""  # the drain reads an empty doc on an accepted paper as pack-only
    try:
        doc = await _build_doc_for(effects, key, budget_chars)
    except BaseException:
        _CURATE_CLAIMS.release([key])  # a failed build must not pin the paper
        raise
    if _effective_chars(doc) > budget_chars:  # doc changed since caching
        _CURATE_CLAIMS.release([key])
        _CURATE_DOC_CACHE[key] = await _curate_doc_sizes(effects, key)
        return "", ""
    _CURATE_STARVED.pop(key, None)
    return key, doc


def release_curate_keys(keys: list[str]) -> None:
    _CURATE_CLAIMS.difference_update(keys)


def _booked_terminal(out, state: dict) -> bool:
    """Did action_curate_book_result leave this paper in a state the pipeline
    will never revisit? Mirrors _curation_pending's terminal set exactly:
    review denied/review_failed, or pack packed/pack_failed. A booking whose
    own status is 'failed' wrote nothing durable and must not count."""
    res = (getattr(out, "result", None) or {}) if out is not None else {}
    if str(res.get("status") or "") == "failed":
        return False
    review = str((state.get("review") or {}).get("status") or "")
    pack = str((state.get("pack") or {}).get("status") or "")
    if review in ("denied", "review_failed"):
        return True
    return review == "accepted" and pack in ("packed", "pack_failed")


async def _book_curate_oversize(effects, paper_key: str, reason: str) -> None:
    """Park a paper the curate seat can never hold — a REVIEW QUEUE, not a
    rejection, following the extract_off_topic precedent: status + reason in
    the extraction sidecar, listable and clearable by hand. A booking
    failure must never break selection or the drain."""
    from agent.actions.scholarly_actions import append_extraction_records

    try:
        # FULL ROW, NOT A STUB. The sidecar is last-row-wins on read, so a
        # three-field park row shadowed md_path, figure_count,
        # extraction_quality -- and translated/md_en_path on translated papers.
        # Measured 2026-09-06: 0 of 28 parked rows still carried
        # extraction_quality; the history beneath each still did. Same incident
        # class as 2026-08-22 (never append partial databank rows). Read the
        # key's current row and change only what the park owns.
        from agent.actions.scholarly_actions import EXTRACTION_PATH, _read_jsonl_records

        try:
            current = (await _read_jsonl_records(effects, EXTRACTION_PATH)).get(
                paper_key
            ) or {}
        except Exception:  # noqa: BLE001 -- enrichment only; a park must still land
            current = {}
        row = dict(current)
        row.update(
            paper_key=paper_key,
            extraction_status="curate_oversize",
            failure_reason=reason[:300],
        )
        await append_extraction_records(effects, [row])
        logger.warning("curate oversize: parked %s — %s", paper_key, reason[:160])
    except Exception:  # noqa: BLE001 — a park must not break the lane
        logger.exception("failed to book curate_oversize for %s", paper_key)
    _CURATE_DOC_CACHE.pop(paper_key, None)
    _CURATE_STARVED.pop(paper_key, None)


async def _curate_turn(effects, prompt: str, max_tokens: int):
    # No `domain` here ON PURPOSE. Which engine a curate turn runs on is a
    # property of the LANE, not of the action: worker_pool stamps it on the
    # branch's ChildEffects, so curate..curate5 stay local while curate_r1-4
    # run on the remote host, from this one code path. Setting it here would
    # force every lane onto one server and re-create the no-op it replaced.
    result = await effects.run_inference(
        prompt, {"max_tokens": max_tokens, "temperature": "t*0.4"}
    )
    if getattr(result, "error", None):
        raise _CurateTransportFault(str(result.error))
    text = result.text or ""
    if not text.strip():
        # An empty response is an infrastructure symptom (contention,
        # reasoning-swallowed budget), never a verdict — replaying the same
        # doc parsed cleanly. Booking it as review_failed would burn the
        # paper permanently; deferring re-selects it next round.
        raise _CurateTransportFault("empty response text")
    return text


async def _curate_stateless(effects, paper_key: str, doc: str) -> dict:
    """Review + pack via stateless turns; returns book_result-shaped state.

    Model-quality failures (unparseable review, gates failed twice) come
    back as bookable review_failed/pack_failed states. Transport faults
    raise — the caller declines the round without burning the paper (the
    fig_review transport-burn lesson).
    """
    from agent.llm_json import parse_llm_json

    state: dict = {"paper_key": paper_key, "session_id": ""}
    review_prompt = await _render_prompt(
        "curator/review_paper", {"corpus_subject": await _corpus_subject(effects)}
    )
    review = None
    for nudge in (
        "",
        '\n\nReturn ONLY the fenced JSON verdict object with "verdict" '
        '("accept" or "deny"), "document_form", "summary", and "issues".',
    ):
        text = await _curate_turn(
            effects, doc + "\n\n---\n\n" + review_prompt + nudge, 4096
        )
        review = parse_llm_json(text)
        if isinstance(review, dict) and review.get("verdict") in ("accept", "deny"):
            break
        review = None
    if review is None:
        state["review"] = {"status": "review_failed", "summary": "", "issues": []}
        return state

    state["review"] = review_state_from(review)
    verdict = state["review"]["status"]
    if verdict == "denied":
        return state

    registry = await _load_registry(effects)
    # THE PACK READS THE WHOLE PAPER. The review is a judgement and may see a
    # compressed doc; the pack is an extraction, and a fact in a paragraph
    # the compression dropped can never be packed (operator ruling
    # 2026-09-06: "we cannot pack facts the LLM never sees -- even with the
    # cost, the loss of facts is far more detrimental"). Windows are
    # seat-independent, so the raw doc is always available to them; the
    # price is 2-4x more pack turns on the largest papers and page furniture
    # reaching the packer, where the grounding gate and coinage guard hold.
    state["pack"] = await _pack_windowed(
        effects, await _raw_curator_doc(effects, paper_key), registry
    )
    state["pack_doc_form"] = "raw"
    return state


async def _raw_curator_doc(effects, paper_key: str) -> str:
    """The UNCOMPRESSED curator doc: raw markdown (the gated English
    translation when one exists) with figtext anchored -- what the pack
    reads. Kept apart from _build_doc_for so building it never overwrites
    the review form recorded in _DOC_FORMS."""
    fc = await effects.read_file(f"databank/markdown/{paper_key}.en.md")
    if not getattr(fc, "exists", False):
        fc = await effects.read_file(f"databank/markdown/{paper_key}.md")
    md = fc.content if getattr(fc, "exists", False) else ""
    return build_curator_doc(md, await _load_figtext(effects, paper_key))


async def _pack_only_raw(effects, paper_key: str, rec: dict) -> dict:
    """Pack an already-accepted paper from its UNCOMPRESSED curator doc.

    No review turn: the verdict on record is carried through verbatim so the
    booking rewrites it unchanged and books only the pack. The doc is
    _build_doc_for at budget 0 -- the raw markdown (English translation when
    one exists) with figtext anchored -- which _pack_windowed cuts into
    section-bounded windows exactly as it does for a compressed doc. The
    form is recorded as "raw-oversize" so the artifact says which text it was
    cut from; every other pack form is unchanged.

    What this trades, deliberately: a compressed doc is 2-4x smaller on the
    largest theses, so raw windows cost that many more pack turns, and
    reference lists and page furniture reach the packer, where the coinage
    guard and the grounding gate -- not compression -- have to hold the line.
    """
    doc = await _raw_curator_doc(effects, paper_key)
    _DOC_FORMS[paper_key] = "raw-oversize"
    registry = await _load_registry(effects)
    return {
        "paper_key": paper_key,
        "session_id": "",
        "review": {
            "status": "accepted",
            "summary": str(rec.get("review_summary") or ""),
            "issues": list(rec.get("review_issues") or []),
            "deny_category": "",
            "document_form": str(rec.get("review_document_form") or ""),
        },
        "pack": await _pack_windowed(effects, doc, registry),
        "pack_doc_form": "raw-oversize",
    }


# Document forms that are not scientific works. A relevant figure inside one
# does not carry the document (operator ruling 2026-09-04: a faculty newsletter
# with one LIBS spectrum was accepted, its review described a LIBS paper the
# document does not contain, and the pack coined 227 keys of admission dates
# and figure captions). The model names the form; this rule makes the
# consequence deterministic instead of hoping the verdict follows.
NON_PAPER_FORMS = frozenset(
    {
        "newsletter",
        "magazine",
        "brochure",
        "press_release",
        "event_programme",
        "event_program",
        "annual_report",
        "advertisement",
        "catalogue",
        "catalog",
        "website",
        "slides",
    }
)


def review_state_from(review: dict) -> dict:
    """The booked review from a parsed verdict object, composition rule applied.

    Shared by the stateless and the session review paths so the two cannot
    drift. "deny_category" is kept verbatim when the model gave one (an
    unexpected category is a signal about the prompt); a composition
    downgrade sets it to corpus_fit and records why in "issues".
    """
    verdict = "accepted" if review.get("verdict") == "accept" else "denied"
    form = (
        str(review.get("document_form") or "")
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )
    issues = [str(i) for i in (review.get("issues") or [])][:20]
    deny_category = (
        str(review.get("deny_category") or "").strip().lower()
        if verdict == "denied"
        else ""
    )
    if verdict == "accepted" and form in NON_PAPER_FORMS:
        verdict = "denied"
        deny_category = "corpus_fit"
        issues = (
            issues
            + [
                f"document form '{form}' is not a scientific work; an isolated "
                "relevant figure or item does not carry the document "
                "(composition rule)"
            ]
        )[:20]
    return {
        "status": verdict,
        "summary": str(review.get("summary") or "").strip(),
        "issues": issues,
        "deny_category": deny_category,
        "document_form": form,
    }


# ── Windowed packing ─────────────────────────────────────────────────
#
# THE ONLY PACKING PATH (operator ruling 2026-09-03: "no reason to maintain
# the un-windowed strategy"). Pack success collapses with document length on
# every model -- 96% at 10-25k tokens, 58% at 50-100k, 36% over 100k,
# measured over 1,941 accepted papers -- and the failures are the grounding
# gate rejecting numbers the paper never states: the model stops attending
# and starts completing the schema. Section-bounded windows recovered 12/12
# papers whole-document packing had failed (6,865 grounded values, one of
# them a 387k-token document no seat could hold).
#
# Small papers are unchanged BY CONSTRUCTION: a document under the window
# target is exactly one window, a single window carries no part-of-N
# preface, and its gates run once against the whole document -- so the
# prompt and the turn count are byte-for-byte what they were.

# THE WORDING IS THE ORIGINAL, ON PURPOSE (operator ruling 2026-09-04). fb801b5
# softened it to "pack this part as you would the whole paper, with the same
# selectivity and the registry's vocabulary" after the first windowed packs
# coined a median 25 new registry keys (p90 209), and added the prior_keys
# block in the same commit. A four-arm A/B on the two worst papers
# (dev/bench_preface_ab.py; dev/CURATE_LANE_BENCH_2026-08-31.md) separated
# the two: the soft wording LOST grounded values with and without the block
# (289 -> 66, 403 -> 263) and passed fewer windows, while the block GAINED
# values both times (289 -> 403, 66 -> 263) at no coinage cost. The August
# coinage was the wording's, before the registry had absorbed those keys;
# coinage is only comparable arm-to-arm on one day. So: original wording +
# prior_keys block -- the best arm on both papers (243 and 160 values). The
# first production pack under it coined 227 keys on a mis-accepted
# newsletter; coinage is now the COINAGE GUARD's job (fold_pack_into_registry),
# not the wording's, and the wording stays.
_PACK_PREFACE = (
    "[This is part {n} of {total} of one paper — {sections} consecutive "
    'section(s) starting at "{heading}". Pack ONLY values stated in THIS '
    "part; other parts are packed separately and merged.]\n\n"
)


def _pack_window_sizes() -> tuple[int, int]:
    """(target, cap) tokens; env overrides for ops and tests."""
    from agent.actions.pack_windows import DEFAULT_MAX_TOKENS, DEFAULT_TARGET_TOKENS

    def _env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        try:
            return max(1, int(raw)) if raw else default
        except ValueError:
            return default

    target = _env("OUROBOROS_PACK_WINDOW_TOKENS", DEFAULT_TARGET_TOKENS)
    cap = max(target, _env("OUROBOROS_PACK_WINDOW_CAP", DEFAULT_MAX_TOKENS))
    return target, cap


async def _pack_windowed(effects, doc: str, registry: dict) -> dict:
    """Pack a curator doc window by window; returns the book_result pack state.

    Per window: the production pack prompt (registry block + the previous
    attempt's gate findings), two attempts, output canonicalised then
    re-shaped (repair_shapes: a grounded list in the wrong wrapper is a
    wrapper problem, not a data problem), gates run against THE WINDOW --
    stricter than the whole-document check, and the one that stops a number
    invented while reading the methods being excused by text in the
    appendix. Passed windows merge (lists concatenate, dicts merge, scalars
    first-wins with every disagreement recorded); the merge then faces the
    production gates against the whole document. A window that fails costs
    that window, not the paper.
    """
    from agent.actions.pack_windows import (
        MergeReport,
        merge_packs,
        repair_shapes,
        window_sections,
    )
    from agent.llm_json import parse_llm_json

    target, cap = _pack_window_sizes()
    windows = window_sections(doc, target, cap)
    if not windows:
        return {
            "status": "pack_failed",
            "reason": "empty document",
            "attempts": 0,
            "quality": {"parse_attempts": 0},
        }
    aliases = await load_key_aliases(effects)
    multi = len(windows) > 1
    attempts = 0
    passed_packs: list[dict] = []
    outcomes: list[dict] = []
    repairs_all: list[dict] = []
    last_gates: dict = {"passed": False, "feedback": ""}
    for w in windows:
        preface = (
            _PACK_PREFACE.format(
                n=w.index + 1,
                total=len(windows),
                sections=w.section_count,
                heading=w.first_heading,
            )
            if multi
            else ""
        )
        feedback = ""
        gates: dict = {"passed": False, "feedback": ""}
        data = None
        w_attempts = 0
        # The paper's own vocabulary so far: keys the earlier windows packed.
        # Absent on the first window (and so on every single-window paper),
        # which keeps that prompt byte-identical to the pre-windowing one.
        prior_keys = sorted({k for pk in passed_packs for k in pk})
        for _ in range(2):  # attempt 2 renders with attempt 1's gate findings
            w_attempts += 1
            ctx = {
                "key_registry_block": format_key_registry(registry),
                "gate_feedback": feedback,
            }
            if prior_keys:
                ctx["prior_keys"] = ", ".join(prior_keys[:120])
            pack_prompt = await _render_prompt("curator/pack_data", ctx)
            text = await _curate_turn(
                effects, preface + w.text + "\n\n---\n\n" + pack_prompt, 8192
            )
            parsed = parse_llm_json(text)
            data = parsed if isinstance(parsed, dict) and parsed else None
            repairs: list[dict] = []
            if data is not None:
                data = canonicalize_pack_keys(data, aliases)
                data, repairs = repair_shapes(data, registry)
            gates = (
                _run_pack_gates(data, w.text, registry)
                if data is not None
                else {"passed": False, "feedback": "output was not a JSON object"}
            )
            if gates["passed"]:
                break
            feedback = gates["feedback"]
        attempts += w_attempts
        last_gates = gates
        g = gates.get("grounding") or {}
        outcomes.append(
            {
                "window": w.index,
                "tokens": w.tokens,
                "sections": w.section_count,
                "oversize": w.oversize,
                "passed": bool(gates["passed"]),
                "attempts": w_attempts,
                "grounding_rate": g.get("grounding_rate"),
                "numeric_leaves": g.get("numeric_leaves"),
                "feedback": (
                    "" if gates["passed"] else str(gates.get("feedback") or "")[:300]
                ),
            }
        )
        if gates["passed"] and data:
            passed_packs.append(data)
            repairs_all.extend(repairs)

    if not passed_packs:
        return {
            "status": "pack_failed",
            "reason": (
                f"gates failed twice: {last_gates.get('feedback', '')[:300]}"
                if not multi
                else f"no window passed ({len(windows)} windows): "
                f"{last_gates.get('feedback', '')[:240]}"
            ),
            "attempts": attempts,
            "quality": {
                "grounding_rate": (last_gates.get("grounding") or {}).get(
                    "grounding_rate"
                ),
                "parse_attempts": attempts,
                "windows": len(windows),
                "windows_passed": 0,
                "window_outcomes": outcomes,
            },
        }

    report = MergeReport()
    if multi:
        merged = merge_packs(passed_packs, report)
        final = _run_pack_gates(merged, doc, registry)
    else:
        merged = passed_packs[0]
        final = last_gates  # one window IS the document; do not re-run the gates
    if not final["passed"]:
        return {
            "status": "pack_failed",
            "reason": f"merged pack failed the whole-document gates: {final['feedback'][:300]}",
            "attempts": attempts,
            "quality": {
                "grounding_rate": (final.get("grounding") or {}).get("grounding_rate"),
                "parse_attempts": attempts,
                "windows": len(windows),
                "windows_passed": len(passed_packs),
                "window_outcomes": outcomes,
            },
        }
    return {
        "status": "packed",
        "data": merged,
        "attempts": attempts,
        "quality": {
            "grounding_rate": final["grounding"]["grounding_rate"],
            "numeric_leaves": final["grounding"]["numeric_leaves"],
            "ungrounded": final["grounding"]["ungrounded"],
            "new_keys": len(final["registry"]["new_keys"]),
            "reused_keys": len(final["registry"]["reused_keys"]),
            "near_duplicate_flags": final["near_dups"],
            "parse_attempts": attempts,
            "windows": len(windows),
            "windows_passed": len(passed_packs),
            "window_conflicts": report.conflicts[:25],
            "shape_repairs": repairs_all[:25],
            "window_outcomes": outcomes,
        },
    }


async def action_curate_drain_batch(step_input):
    """Curate up to OUROBOROS_CURATE_PAPERS seat-sized papers per round —
    the curate_drain flow's work step, built to ride as a parallel branch
    on an idle batched text seat. Papers run SERIALLY (one seat, one doc
    in the cell at a time); the budget exists for long network-bound
    windows where one paper leaves the seat idle for the back half.

    Stateless review+pack (context peak = doc + one answer), booked through
    action_curate_book_result so the envelope, registry update and
    tag_review_agreement ride the production path. Transport faults end
    the round with nothing booked for that paper; claims release either
    way.

    Inputs: working_directory. Result: attempted, outcomes[].
    """
    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepInput, StepOutput

    effects = step_input.effects
    budget = _curate_drain_budget()

    def _summary_out(outcomes: list, reason: str = "") -> StepOutput:
        summary = {"attempted": len(outcomes), "outcomes": outcomes}
        if reason:
            summary["reason"] = reason
        obs = (
            "curate drain: "
            + "; ".join(f"{o['paper_key']} → {o['outcome']}" for o in outcomes)
            if outcomes
            else f"curate drain idle ({reason})"
        )
        return StepOutput(
            result=summary,
            observations=obs,
            context_updates={"curate_drain_summary": summary},
        )

    if budget <= 0:
        return _summary_out([], "disabled")
    if effects is None:
        return _summary_out([], "no effects")
    # The dispatcher admitted this lane against a live reading of the pool
    # and claimed it; size the document against THAT, not against the static
    # configured cell. None outside the worker pool, where rung 3/4 apply.
    claim = current_claim()
    budget_chars = await _curate_doc_budget_chars(
        effects, claim_tokens=(claim.tokens if claim else 0)
    )
    if budget_chars <= 0:
        return _summary_out(
            [], "cell below whole-paper threshold or server unreachable"
        )

    outcomes: list[dict] = []
    for _ in range(budget):
        databank = await read_databank(effects)
        key, doc = await select_curate_paper(effects, databank, budget_chars)
        if not key:
            return _summary_out(outcomes, "nothing unclaimed fits the seat budget")
        rec = databank.get(key) or {}
        pack_only = not doc and rec.get("review_status") == "accepted"
        if claim is not None:
            # Hand back what this document did not need. A 20k-token paper
            # must not hold a 55k claim for the length of the unit, or the
            # "several small docs give us parallelism" half of the design
            # never happens — the first lane would sit on the whole pool.
            need = _pack_window_sizes()[1] if pack_only else _estimate_doc_tokens(doc)
            claim.resize(need + _CURATE_TURN_OVERHEAD_TOKENS)
        try:
            try:
                state = (
                    await _pack_only_raw(effects, key, rec)
                    if pack_only
                    else await _curate_stateless(effects, key, doc)
                )
            except _CurateTransportFault as e:
                if _CURATE_OVERSIZE_FAULT_MARKER in str(e):
                    # The engine's own verdict that this doc can never fit a
                    # seat — deterministic, so re-selection is a loop, not a
                    # retry. Backstop for docs the estimator under-counts.
                    await _book_curate_oversize(
                        effects,
                        key,
                        "curation: engine refused the prompt as over-seat "
                        f"({str(e)[:160]}); review by hand",
                    )
                    continue
                logger.warning("curate drain transport fault on %s: %s", key, e)
                return _summary_out(outcomes, f"transport fault ({str(e)[:120]})")
            except Exception:  # noqa: BLE001 — code faults must not burn papers
                logger.exception(
                    "curate drain errored on %s — ending round, not booking", key
                )
                return _summary_out(outcomes, "internal error (see log)")
            out = await action_curate_book_result(
                StepInput(effects=effects, context={"curate_state": state})
            )
            _CURATE_DOC_CACHE.pop(key, None)
            # Record the terminal booking BEFORE the claim is released (in
            # `finally` below), so there is no instant at which a sibling can
            # see the key unclaimed, unbooked-in-memory, and pending on a
            # stale snapshot. A booking that itself failed leaves the paper
            # pending and is deliberately NOT recorded.
            if _booked_terminal(out, state):
                _CURATE_BOOKED[key] = time.monotonic()
        finally:
            release_curate_keys([key])
        outcomes.append(
            {
                "paper_key": key,
                "outcome": str(
                    (out.result or {}).get("status")
                    or state["review"].get("status", "")
                ),
                "doc_chars": len(doc),
            }
        )
    return _summary_out(outcomes)


async def action_fig_review_sweep_next(step_input):
    """Dispatch the next fig-review batch; empty worklist completes the goal."""
    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepOutput

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or not effects:
        return StepOutput(
            result={"sweep_complete": True}, observations="No mission/effects"
        )
    goal = next(
        (
            g
            for g in mission.goals
            if g.type == "fig_review" and g.status == "incomplete"
        ),
        None,
    )
    if goal is None:
        return StepOutput(
            result={"sweep_complete": True},
            observations="No incomplete fig_review goal — sweep complete",
        )

    databank = await read_databank(effects)
    batch = _fig_batch(databank)
    if not batch:
        goal.status = "complete"
        await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": True},
            observations="Fig-review worklist empty — corpus goal complete",
        )

    remaining = sum(1 for r in databank.values() if _fig_pending(r))
    return StepOutput(
        result={"needs_fig_review": True},
        observations=(
            f"Fig-review sweep: dispatching {len(batch)} of {remaining} "
            f"pending paper(s)"
        ),
        context_updates={
            "dispatch_config": {
                "goal_id": goal.id,
                "paper_keys": batch,
                "flow_directive": (
                    f"VLM figure readings for {len(batch)} paper(s): "
                    + ", ".join(batch)
                ),
            }
        },
    )


async def action_fig_review_batch(step_input):
    """Run the fig_review sidecar over one batch and book the results.

    figtext is a CLAIM (advisory overlap only, no gate here — the
    review pass judges the inlined reading in context). Tool errors
    book figtext_failed directly: the curate pass proceeds md-only for
    those papers rather than looping the sidecar.
    """
    import os

    from agent.actions.scholarly_actions import append_records, read_databank
    from agent.models import StepOutput

    effects = step_input.effects
    keys = list(step_input.inputs.get("paper_keys") or [])
    working_dir = str(step_input.inputs.get("working_directory") or "")
    if not effects or not keys:
        return StepOutput(
            result={"status": "failed"},
            observations="No effects or empty fig batch",
            context_updates={
                "directive_report": {
                    "flow": "fig_review",
                    "status": "failed",
                    "summary": "Empty fig-review batch",
                }
            },
        )

    root = _repo_root()
    cmd = [
        os.path.join(root, _FIG_TOOL_PY),
        os.path.join(root, _FIG_TOOL_SCRIPT),
        "--keys",
        *keys,
        "--figures-root",
        os.path.join(working_dir, "databank", "figures"),
        "--markdown-dir",
        os.path.join(working_dir, "databank", "markdown"),
        "--out-dir",
        os.path.join(working_dir, FIGTEXT_DIR),
        "--vl-backend",
        _fig_backend(),
    ]
    max_figures = int(step_input.inputs.get("max_figures") or 0)
    if max_figures > 0:
        cmd += ["--max-figures", str(max_figures)]
    if _fig_backend() == "mlx" and _fig_mlx_model():
        cmd += ["--model", _fig_mlx_model()]
    result = await effects.run_command(cmd, timeout=FIG_TIMEOUT_S)

    reports: dict[str, dict] = {}
    for line in (result.stdout or "").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("paper_key"):
            reports[r["paper_key"]] = r

    from agent.actions.extraction_actions import is_toolchain_fault

    if not reports:
        # ZERO reports is the shape of a DEAD SERVER, not of bad figures:
        # the tool exits with nothing when every vision call fails to
        # connect. Booking "no report from tool" here mass-marked 965
        # papers figtext_failed on the 08-22 port day and 437 more during
        # the 2026-08-26 server fault — both mass-heals. Probe the server:
        # if it is unreachable, decline the round with NOTHING booked
        # (claims release in the caller's finally) and let the sweep
        # retry when the server returns. A reachable server with zero
        # reports still books below — a permanently absent tool must not
        # spin forever (the curator e2e pins that).
        if not await server_alive(effects):
            summary = {"status": "failed", "reason": "server unreachable — declined"}
            return StepOutput(
                result=summary,
                observations="Fig review declined: server unreachable, nothing booked",
                context_updates={"figtext_summary": summary},
            )

    databank = await read_databank(effects)
    updates, done, failed, skipped, partial = [], 0, 0, 0, 0
    for k in keys:
        rec = dict(databank.get(k) or {"paper_key": k})
        rep = reports.get(k)
        err = (rep or {}).get("error") or "no report from tool"
        if rep is not None and not rep.get("error"):
            if int(rep.get("remaining") or 0) > 0:
                # Partial round under --max-figures: readings are banked in
                # the sidecar; the record stays fig-PENDING so a later round
                # resumes where this one stopped. Progress is bookkeeping,
                # never a terminal status.
                rec["figtext_progress"] = (
                    f"{int(rep.get('described') or 0)}/{int(rep.get('figs_total') or 0)}"
                )
                partial += 1
            else:
                rec["figtext_status"] = "figtext_done"
                rec["figtext_path"] = f"{FIGTEXT_DIR}/{k}.json"
                rec["figtext_progress"] = ""
                done += 1
        elif rep is not None and is_toolchain_fault(err):
            # TRANSPORT, NOT VERDICT — the same rule the extraction ladder
            # learned from the vlm-500 incident: a reported HTTP 5xx /
            # connection failure says nothing about the paper's figures,
            # and booking figtext_failed on it burned 34 papers terminally
            # during the 2026-08-16 server outage. Leave the record
            # untouched so a later sweep retries it. Deliberately NARROW:
            # a tool that produced NO report (missing venv, misconfigured
            # command) still books figtext_failed below — the curate pass
            # proceeds md-only rather than the sweep spinning forever on a
            # permanently absent tool (the curator e2e pins this).
            skipped += 1
            continue
        else:
            rec["figtext_status"] = "figtext_failed"
            rec["failure_reason"] = f"fig_review: {err}"
            failed += 1
        updates.append(rec)
    await append_records(effects, updates)

    status = "success" if done or partial else "failed"
    summary = (
        f"Fig review: {done} done, {failed} failed of {len(keys)}"
        + (f", {partial} partial" if partial else "")
        + (f", {skipped} deferred (toolchain fault)" if skipped else "")
    )
    return StepOutput(
        result={"status": status, "done": done, "failed": failed, "partial": partial},
        observations=summary,
        context_updates={
            "directive_report": {
                "flow": "fig_review",
                "status": status,
                "summary": summary,
            }
        },
    )


async def action_curate_sweep_next(step_input):
    """Dispatch the next paper to curate (one paper = one session lifecycle).

    needs_repack (one failed pack gate behind it) takes priority; empty
    worklist completes the corpus goal.
    """
    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepOutput

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or not effects:
        return StepOutput(
            result={"sweep_complete": True}, observations="No mission/effects"
        )
    goal = next(
        (g for g in mission.goals if g.type == "curate" and g.status == "incomplete"),
        None,
    )
    if goal is None:
        return StepOutput(
            result={"sweep_complete": True},
            observations="No incomplete curate goal — sweep complete",
        )

    databank = await read_databank(effects)
    retry = sorted(
        k
        for k, r in databank.items()
        if _curation_pending(r) and r.get("pack_status") == "needs_repack"
    )
    fresh = sorted(
        k
        for k, r in databank.items()
        if _curation_pending(r) and r.get("pack_status") != "needs_repack"
    )
    worklist = retry + fresh
    if not worklist:
        goal.status = "complete"
        await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": True},
            observations="Curation worklist empty — corpus goal complete",
        )

    key = worklist[0]
    return StepOutput(
        result={"needs_curate": True},
        observations=(
            f"Curation sweep: dispatching {key} "
            f"({len(worklist)} pending{', retry first' if retry else ''})"
        ),
        context_updates={
            "dispatch_config": {
                "goal_id": goal.id,
                "paper_key": key,
                "flow_directive": f"Review and pack paper {key}",
            }
        },
    )


# ── Actions: the per-paper session (ingest → review → pack) ──────────


def _snapshot_key(paper_key: str) -> str:
    return f"paper:{paper_key}"


# These templates use real `when:` sections and {context.x} interpolation,
# so they keep the full PromptRenderer rather than moving to
# load_prompt_text. The renderer is now built ONCE — it was reconstructed
# per call, which defeated its own template cache on every invocation.
_curator_renderer = None  # lazily built PromptRenderer


async def _corpus_subject(effects) -> str:
    """What this corpus collects, in the operator's own words.

    The mission objective is the only place the subject is stated, and it is
    stated by the person who chose it. Empty when unavailable — the prompt
    section is `when`-gated, so a missing objective degrades to the previous
    behaviour instead of asserting a domain nobody chose.
    """
    try:
        mission = await effects.load_mission()
    except Exception:  # noqa: BLE001 — a missing mission must not fail curation
        return ""
    return str(getattr(mission, "objective", "") or "").strip()


async def _render_prompt(template_id: str, context: dict) -> str:
    global _curator_renderer
    if _curator_renderer is None:
        from agent.loader import PromptRenderer

        _curator_renderer = PromptRenderer(_prompts_dir())
    return _curator_renderer.render(
        template_id, {"input": {}, "context": context, "meta": {}}
    )


# Which compression rung each paper's doc was built at, for the booking
# stamp (review_doc_form). In-process, like the claims sets: review,
# pack and gate all run in the one mission process, so the form chosen
# at selection is the form every later rebuild of that key sees.
_DOC_FORMS: dict[str, str] = {}


async def _build_doc_for(
    effects, paper_key: str, budget_chars: int | None = None
) -> str:
    """The curator doc, compressed only as far as the budget requires.

    With no budget the doc is raw (legacy callers). With one, the doc
    walks doc_compression.LADDER — raw first, then the lossless table
    conversion, then two squeeze depths — and takes the FIRST rung that
    fits. Blind-checked at 95% verdict agreement with both misses
    conservative (see doc_compression module docstring); the ladder
    exists precisely because the misses clustered at needless depth.
    Figtext is never compressed — the <img> anchors survive every rung,
    so grounding and figure claims work unchanged.
    """
    from agent.actions.doc_compression import LADDER, compress_rung

    # Prefer the gated English translation when the translation drain has
    # produced one (translation preserves numbers and <img> paths verbatim,
    # so figtext anchoring and the grounding gate work unchanged).
    fc = await effects.read_file(f"databank/markdown/{paper_key}.en.md")
    if not getattr(fc, "exists", False):
        fc = await effects.read_file(f"databank/markdown/{paper_key}.md")
    md = fc.content if getattr(fc, "exists", False) else ""
    figtext = await _load_figtext(effects, paper_key)
    if budget_chars is None:
        budget_chars = await _curate_doc_budget_chars(effects)
    doc = build_curator_doc(md, figtext)
    if budget_chars <= 0 or _effective_chars(doc) <= budget_chars:
        _DOC_FORMS[paper_key] = "raw"
        return doc
    import asyncio

    loop = asyncio.get_running_loop()
    for rung in LADDER[1:]:
        # Executor, not inline: the ladder is regex-heavy CPU work and the
        # first selection scan walks ~500 oversized docs — run inline it
        # blocks every lane on the one loop (and the monitor's stale-decode
        # recovery would read the stall as a server wedge).
        compressed = await loop.run_in_executor(None, compress_rung, md, rung)
        doc = build_curator_doc(compressed, figtext)
        if _effective_chars(doc) <= budget_chars:
            _DOC_FORMS[paper_key] = rung
            return doc
    # Still over: return the deepest form; selection skips it (> budget)
    # exactly as it skipped the raw doc before — genuinely parked.
    _DOC_FORMS[paper_key] = "full-over"
    return doc


async def action_curate_ingest_review(step_input):
    """Turn 1 of the paper session: ingest the curator doc + review it.

    Starts the memoryful session, sends doc + review prompt as one
    turn, parses the verdict (one bounded re-ask on unparseable
    output), then pins the post-review snapshot — the pack retry forks
    from it with the review still in context. A stale snapshot under
    this paper's key (crashed prior dispatch) is purged first, so the
    deterministic key self-heals leaks.

    Publishes curate_state {session_id, paper_key, review{...}} for the
    downstream steps of this flow.
    """
    from agent.llm_json import parse_llm_json
    from agent.models import StepOutput

    effects = step_input.effects
    paper_key = str(step_input.inputs.get("paper_key") or "")
    if not effects or not paper_key:
        return StepOutput(
            result={"verdict": "error"},
            observations="No effects or paper_key",
        )

    doc = await _build_doc_for(effects, paper_key)
    if not doc.strip():
        return StepOutput(
            result={"verdict": "error"},
            observations=f"No markdown on disk for {paper_key}",
            context_updates={
                "curate_state": {"paper_key": paper_key, "session_id": ""}
            },
        )

    # Self-heal a leaked snapshot from a crashed prior dispatch.
    await effects.purge_inference_snapshot(_snapshot_key(paper_key))

    # THE CORPUS HAS TO NAME ITSELF. The review prompt asks "is this paper
    # about the corpus's subject matter?" — a question the model cannot answer
    # unless it is told what the corpus collects. Left blank, it falls back on
    # whatever domain the system role happens to mention, and the pilot showed
    # exactly that: on a SPECTROSCOPY corpus, muse denied a planetary Raman
    # database, a powder-diffraction methods review and an IR study of
    # gallstones, each "not materials science" — three of four denials decided
    # by a word in the prompt rather than by the paper.
    review_prompt = await _render_prompt(
        "curator/review_paper", {"corpus_subject": await _corpus_subject(effects)}
    )
    session_id = await effects.start_inference_session(
        config={"ttl_seconds": CURATE_SESSION_TTL}
    )
    state = {"paper_key": paper_key, "session_id": session_id}
    try:
        result = await effects.session_inference(
            session_id,
            doc + "\n\n---\n\n" + review_prompt,
            config_overrides={"max_tokens": 4096, "temperature": "t*0.4"},
        )
        review = parse_llm_json(result.text or "")
        if not (
            isinstance(review, dict) and review.get("verdict") in ("accept", "deny")
        ):
            # One bounded re-ask — same session, tiny turn.
            result = await effects.session_inference(
                session_id,
                'Return ONLY the fenced JSON verdict object with "verdict" '
                '("accept" or "deny"), "document_form", "summary", and "issues".',
                config_overrides={"max_tokens": 2048, "temperature": "t*0.4"},
            )
            review = parse_llm_json(result.text or "")

        if not (
            isinstance(review, dict) and review.get("verdict") in ("accept", "deny")
        ):
            state["review"] = {"status": "review_failed", "summary": "", "issues": []}
            return StepOutput(
                result={"verdict": "review_failed"},
                observations=f"Review unparseable twice for {paper_key}",
                context_updates={"curate_state": state},
            )

        # WHAT COULD BE DONE ABOUT IT lives in deny_category (kept verbatim);
        # the composition rule lives in review_state_from, shared with the
        # stateless path.
        state["review"] = review_state_from(review)
        verdict = state["review"]["status"]
        # Pin the post-review context: pack (turn 2) continues live; the
        # pack RETRY forks from here with the review still in context.
        snap = await effects.session_snapshot(session_id, _snapshot_key(paper_key))
        state["snapshot"] = {
            "resident": bool(snap.get("resident", True)),
            "tokens": int(snap.get("tokens") or 0),
        }
        return StepOutput(
            result={"verdict": verdict},
            observations=(
                f"Review {paper_key}: {verdict} "
                f"({len(state['review']['issues'])} issue(s) noted)"
            ),
            context_updates={"curate_state": state},
        )
    except Exception as e:  # noqa: BLE001 — book the failure, never hang the sweep
        logger.exception("curate ingest/review failed for %s", paper_key)
        state["review"] = {
            "status": "review_failed",
            "summary": f"error: {type(e).__name__}: {e}"[:200],
            "issues": [],
        }
        return StepOutput(
            result={"verdict": "review_failed"},
            observations=f"Review errored for {paper_key}: {type(e).__name__}",
            context_updates={"curate_state": state},
        )


def tag_review_agreement(rec: dict) -> str:
    """Did the curator's full-text verdict confirm the scraper's tag?

    THE TWO REVIEW LAYERS NEVER TALKED. The scraper sorts every paper into
    exact/close/adjacent from ABSTRACT AND METADATA ONLY, in one batched turn,
    before the PDF is even fetched — so the pipeline commits its relevance
    decision on the weakest evidence it will ever hold. The curator then reads
    the FULL TEXT and accepts or denies. When those two disagree, the pipeline
    has learned something specific about its own sorting and, until now,
    silently discarded it.

    This records the comparison. It changes NO decision: the review verdict
    still governs, the tag is not rewritten, nothing is re-sorted. It exists
    so the disagreement is queryable — as a corpus-quality signal now (a rising
    over_rated rate means the tagger is drifting or the aspect definitions are
    too loose), and as the training signal a re-sorting pass would need later.

      over_rated    tagged exact/close, denied on full text
      under_rated   tagged adjacent only, accepted on full text
      confirmed     the two agree
      unknown       no tags, or the review did not reach a verdict

    `adjacent` is the discriminator on purpose: coverage is satisfied by
    exact+close only (check_aspect_coverage), so those are the tags that
    actually admitted the paper to the corpus.
    """
    from agent.actions.scholarly_actions import RELEVANCE_TIERS

    status = rec.get("review_status") or ""
    if status not in ("accepted", "denied"):
        return "unknown"
    # Only RECOGNISED tiers count. Tags are parsed model output, so a dict
    # with no `relevance`, or a junk value, must read as "no evidence about
    # this paper" — not as a weak tier, which would silently score an
    # unparseable tag as agreement.
    tiers = {
        str(t.get("relevance") or "").strip().lower()
        for t in (rec.get("tags") or [])
        if isinstance(t, dict)
    } & set(RELEVANCE_TIERS)
    if not tiers:
        return "unknown"
    strong = bool(tiers & {"exact", "close"})
    if status == "denied" and strong:
        return "over_rated"
    if status == "accepted" and not strong:
        return "under_rated"
    return "confirmed"


def _run_pack_gates(data: dict, doc: str, registry: dict) -> dict:
    """All deterministic pack gates; returns verdict + feedback text."""
    g = grounding_check(data, doc)
    reg = registry_check(data, registry)
    dups = near_duplicate_keys(reg["new_keys"], registry, data)
    problems = []
    if not g["passed"]:
        missing = ", ".join(f"{u['path']}={u['token']}" for u in g["ungrounded"][:8])
        problems.append(
            f"UNGROUNDED VALUES (not stated in the paper — remove or fix): {missing}"
        )
    if reg["type_mismatches"]:
        mm = ", ".join(
            f"{m['key']} (registry: {m['expected']}, yours: {m['actual']})"
            for m in reg["type_mismatches"][:8]
        )
        problems.append(f"TYPE MISMATCHES vs registry: {mm}")
    return {
        "passed": g["passed"] and not reg["type_mismatches"],
        "feedback": "\n".join(problems),
        "grounding": g,
        "registry": reg,
        "near_dups": dups,
    }


async def action_curate_pack_data(step_input):
    """Turn 2: pack the paper's raw data; deterministic gates; one retry.

    Attempt 1 runs in the live session (paper + review in context).
    A gate failure retries ONCE by forking a fresh session from the
    post-review snapshot with the gate findings as feedback — clean
    context, no failed-attempt contamination. Second failure books
    pack_failed (flagged, never silently included).
    """
    from agent.llm_json import parse_llm_json
    from agent.models import StepOutput

    effects = step_input.effects
    state = dict(step_input.context.get("curate_state") or {})
    paper_key = str(state.get("paper_key") or "")
    session_id = str(state.get("session_id") or "")
    if not effects or not paper_key or not session_id:
        state["pack"] = {"status": "pack_failed", "reason": "no session/paper"}
        return StepOutput(
            result={"gate_passed": False},
            observations="Pack skipped — no session state",
            context_updates={"curate_state": state},
        )

    doc = await _build_doc_for(effects, paper_key)
    registry = await _load_registry(effects)
    pack_prompt = await _render_prompt(
        "curator/pack_data",
        {"key_registry_block": format_key_registry(registry), "gate_feedback": ""},
    )

    async def _attempt(sid: str, prompt: str) -> tuple[dict | None, str]:
        result = await effects.session_inference(
            sid,
            prompt,
            config_overrides={"max_tokens": 8192, "temperature": "t*0.4"},
        )
        data = parse_llm_json(result.text or "")
        return (data if isinstance(data, dict) and data else None), (result.text or "")

    attempts = 0
    try:
        attempts = 1
        data, _raw = await _attempt(session_id, pack_prompt)
        gates = (
            _run_pack_gates(data, doc, registry)
            if data is not None
            else {"passed": False, "feedback": "output was not a JSON object"}
        )

        if not gates["passed"]:
            # Retry once from the post-review snapshot: clean context +
            # explicit gate findings. End the contaminated session first.
            attempts = 2
            await effects.end_inference_session(session_id)
            retry_sid = await effects.start_inference_session(
                config={"ttl_seconds": CURATE_SESSION_TTL},
                from_snapshot=_snapshot_key(paper_key),
            )
            state["session_id"] = retry_sid
            retry_prompt = await _render_prompt(
                "curator/pack_data",
                {
                    "key_registry_block": format_key_registry(registry),
                    "gate_feedback": gates["feedback"],
                },
            )
            data, _raw = await _attempt(retry_sid, retry_prompt)
            gates = (
                _run_pack_gates(data, doc, registry)
                if data is not None
                else {"passed": False, "feedback": "output was not a JSON object"}
            )

        if not gates["passed"]:
            state["pack"] = {
                "status": "pack_failed",
                "reason": f"gates failed twice: {gates['feedback'][:300]}",
                "attempts": attempts,
                "quality": {
                    "grounding_rate": gates.get("grounding", {}).get("grounding_rate"),
                    "parse_attempts": attempts,
                },
            }
            return StepOutput(
                result={"gate_passed": False},
                observations=f"Pack gates failed twice for {paper_key}",
                context_updates={"curate_state": state},
            )

        state["pack"] = {
            "status": "packed",
            "data": data,
            "attempts": attempts,
            "quality": {
                "grounding_rate": gates["grounding"]["grounding_rate"],
                "numeric_leaves": gates["grounding"]["numeric_leaves"],
                "ungrounded": gates["grounding"]["ungrounded"],
                "new_keys": len(gates["registry"]["new_keys"]),
                "reused_keys": len(gates["registry"]["reused_keys"]),
                "near_duplicate_flags": gates["near_dups"],
                "parse_attempts": attempts,
                "snapshot_rebuild": not state.get("snapshot", {}).get("resident", True),
            },
        }
        return StepOutput(
            result={"gate_passed": True},
            observations=(
                f"Packed {paper_key}: {len(data)} keys, grounding "
                f"{gates['grounding']['grounding_rate']:.2f} "
                f"(attempt {attempts})"
            ),
            context_updates={"curate_state": state},
        )
    except Exception as e:  # noqa: BLE001 — book the failure, never hang the sweep
        logger.exception("curate pack failed for %s", paper_key)
        state["pack"] = {
            "status": "pack_failed",
            "reason": f"error: {type(e).__name__}: {e}"[:200],
            "attempts": attempts,
        }
        return StepOutput(
            result={"gate_passed": False},
            observations=f"Pack errored for {paper_key}: {type(e).__name__}",
            context_updates={"curate_state": state},
        )


async def action_curate_book_result(step_input):
    """Book the paper's outcome; ALWAYS ends the session + purges the snapshot.

    The single exit step of curate_paper — every path (accepted+packed,
    denied, review_failed, pack_failed) flows through here, so the
    semi-permanent snapshot's explicit release is structural, not
    best-effort. Accepted+packed papers get their dataset envelope
    written and the key registry updated; every outcome lands in the
    databank record with reasons.
    """
    from datetime import datetime, timezone

    from agent.actions.scholarly_actions import append_records, read_databank
    from agent.models import StepOutput

    effects = step_input.effects
    state = dict(step_input.context.get("curate_state") or {})
    paper_key = str(state.get("paper_key") or "")
    session_id = str(state.get("session_id") or "")
    review = dict(state.get("review") or {})
    pack = dict(state.get("pack") or {})
    # Resolved once — both the envelope and curation_method record it, and
    # re-reading the sidecar per field would be two chances to disagree.
    figtext_model = await _figtext_model(effects, paper_key)

    # Structural cleanup FIRST — even a booking error must not leak the
    # pinned instance or the snapshot's context budget.
    if effects and session_id:
        try:
            await effects.end_inference_session(session_id)
        except Exception:  # noqa: BLE001
            logger.warning("end_session failed for %s", session_id)
    if effects and paper_key:
        try:
            await effects.purge_inference_snapshot(_snapshot_key(paper_key))
        except Exception:  # noqa: BLE001
            logger.warning("snapshot purge failed for %s", paper_key)

    if not effects or not paper_key:
        return StepOutput(
            result={"status": "failed"},
            observations="Nothing to book",
            context_updates={
                "directive_report": {
                    "flow": "curate_paper",
                    "status": "failed",
                    "summary": "Booking with no paper/effects",
                }
            },
        )

    databank = await read_databank(effects)
    rec = dict(databank.get(paper_key) or {"paper_key": paper_key})
    rec["review_status"] = review.get("status") or "review_failed"
    # Which compression rung the reviewed doc was built at ("raw" for
    # the untouched form) — provenance for the serializer and for any
    # later audit of compressed-doc verdicts.
    rec["review_doc_form"] = _DOC_FORMS.pop(paper_key, "") or rec.get(
        "review_doc_form", ""
    )
    # Which text the PACK was cut from -- "raw" on the normal path since
    # 2026-09-06, "raw-oversize" for accepted papers beyond every seat. Kept
    # beside review_doc_form because the two no longer describe one doc.
    rec["pack_doc_form"] = str(state.get("pack_doc_form") or "") or rec.get(
        "pack_doc_form", ""
    )
    rec["review_summary"] = review.get("summary") or ""
    rec["review_issues"] = review.get("issues") or []
    rec["deny_category"] = review.get("deny_category") or ""
    rec["review_document_form"] = review.get("document_form") or ""
    rec["tag_review_agreement"] = tag_review_agreement(rec)
    # A DENIAL CLEARS ANY PACK. Live 2026-08-20: two borderline papers
    # were accepted+packed, then re-reviewed ~2 min later by a round that
    # had loaded the databank before the first booking landed; the flip
    # to denied carried the stale pack_status along and two denied
    # papers sat "packed" for two days — headed straight for the
    # training corpus. Whatever the verdict race, a record that says
    # denied must never simultaneously say packed.
    if rec["review_status"] == "denied" and rec.get("pack_status"):
        ds = rec.get("dataset_path") or ""
        rec["pack_status"] = ""
        rec["dataset_path"] = ""
        rec["pack_quality"] = None
        if ds:
            try:
                dp = os.path.join(str(getattr(effects, "working_directory", "")), ds)
                if os.path.isfile(dp):
                    os.remove(dp)
            except OSError:
                logger.warning("could not remove stale pack artifact %s", ds)

    outcome = rec["review_status"]
    if rec["review_status"] == "accepted":
        if pack.get("status") == "packed":
            data = pack.get("data") or {}
            envelope = {
                "paper_key": paper_key,
                "title": rec.get("title", ""),
                "doi": rec.get("doi", ""),
                "arxiv_id": rec.get("arxiv_id", ""),
                **envelope_identity(rec),
                "license": rec.get("license", "") or "unknown",
                "year": rec.get("year", 0),
                "review": {
                    "status": "accepted",
                    "summary": rec["review_summary"],
                },
                "data": data,
                "provenance": {
                    "model": _provenance_model(effects),
                    "figtext_model": figtext_model,
                    "packed_at": datetime.now(timezone.utc).isoformat(),
                    "md_path": rec.get("md_path", ""),
                },
            }
            problems = required_fields_check(envelope)
            if problems:
                # Envelope problems are catalog-side (missing license
                # etc.), not model failures — book pack_failed with the
                # reasons; the record stays flagged, never silent.
                rec["pack_status"] = "pack_failed"
                rec["failure_reason"] = f"envelope: {'; '.join(problems)}"
                outcome = "pack_failed (envelope)"
            else:
                dataset_path = f"{DATASET_DIR}/{paper_key}.json"
                await effects.write_file(
                    dataset_path, json.dumps(envelope, indent=1, ensure_ascii=False)
                )
                registry = await _load_registry(effects)
                coinage = await fold_pack_into_registry(
                    effects, registry, data, paper_key
                )
                rec["pack_status"] = "packed"
                rec["dataset_path"] = dataset_path
                rec["pack_quality"] = {**(pack.get("quality") or {}), **coinage}
                rec["failure_reason"] = ""
                outcome = f"packed ({len(data)} keys)"
        elif pack.get("status") == "needs_repack":
            rec["pack_status"] = "needs_repack"
            outcome = "needs_repack"
        else:
            rec["pack_status"] = "pack_failed"
            rec["failure_reason"] = f"pack: {pack.get('reason') or 'no pack state'}"
            # Keep the window diagnostics on a FAILED pack too: which windows
            # passed, which fabricated, at what grounding. Without this the
            # acceptance check for windowed packing could see only successes.
            rec["pack_quality"] = pack.get("quality") or {}
            outcome = "pack_failed"
    rec["curation_method"] = f"{_provenance_model(effects)}+{figtext_model}"
    await append_records(effects, [rec])

    summary = f"Curated {paper_key}: {outcome}"
    return StepOutput(
        result={"status": "success", "outcome": outcome},
        observations=summary,
        context_updates={
            "directive_report": {
                "flow": "curate_paper",
                "status": "success",
                "summary": summary,
            }
        },
    )


# ── Actions: gate + corpus build ──────────────────────────────────────


async def action_check_curation_complete(step_input):
    """Gate: every extracted record terminal for BOTH curation passes."""
    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepOutput

    effects = step_input.effects
    if not effects:
        return StepOutput(result={"gate_passed": False}, observations="No effects")
    databank = await read_databank(effects)
    pending = sorted(
        k for k, r in databank.items() if _fig_pending(r) or _curation_pending(r)
    )
    if pending:
        return StepOutput(
            result={"gate_passed": False, "pending": len(pending)},
            observations=f"Curation gate: {len(pending)} paper(s) still pending",
            context_updates={"pending_curation": pending[:50]},
        )
    return StepOutput(
        result={"gate_passed": True},
        observations="Curation gate: every extracted paper terminal",
    )


async def action_build_corpus_dataset(step_input):
    """Deterministic merge of accepted dataset envelopes → corpus.json.

    The v3 stage's single input artifact: every packed paper's envelope
    plus the final key registry, with a per-key consistency re-check
    (a registry that disagrees with the envelopes it admitted is a
    booking bug and must fail the gate loudly).
    """
    from datetime import datetime, timezone

    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepOutput

    effects = step_input.effects
    if not effects:
        return StepOutput(result={"built": False}, observations="No effects")
    databank = await read_databank(effects)
    registry = await _load_registry(effects)

    papers, inconsistencies = [], []
    for key, rec in sorted(databank.items()):
        if rec.get("pack_status") != "packed" or not rec.get("dataset_path"):
            continue
        fc = await effects.read_file(rec["dataset_path"])
        if not getattr(fc, "exists", False):
            inconsistencies.append(f"{key}: dataset file missing")
            continue
        try:
            envelope = json.loads(fc.content)
        except json.JSONDecodeError:
            inconsistencies.append(f"{key}: dataset file unparseable")
            continue
        check = registry_check(envelope.get("data") or {}, registry)
        for mm in check["type_mismatches"]:
            inconsistencies.append(f"{key}: {mm['key']} type drift")
        papers.append(envelope)

    if inconsistencies:
        return StepOutput(
            result={"built": False, "inconsistencies": len(inconsistencies)},
            observations=("Corpus build blocked: " + "; ".join(inconsistencies[:5])),
        )

    corpus = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "papers": papers,
        "key_registry": registry,
        "counts": {
            "packed": len(papers),
            "denied": sum(
                1 for r in databank.values() if r.get("review_status") == "denied"
            ),
            "failed": sum(
                1
                for r in databank.values()
                if r.get("review_status") == "review_failed"
                or r.get("pack_status") == "pack_failed"
            ),
        },
    }
    await effects.write_file(
        f"{DATASET_DIR}/corpus.json", json.dumps(corpus, indent=1, ensure_ascii=False)
    )
    return StepOutput(
        result={"built": True, "papers": len(papers)},
        observations=(
            f"Corpus dataset built: {len(papers)} packed, "
            f"{corpus['counts']['denied']} denied, "
            f"{corpus['counts']['failed']} failed"
        ),
    )


async def action_reopen_curation_goal(step_input):
    """Gate failed — reopen incomplete corpus goals for another sweep."""
    from agent.models import StepOutput

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or not effects:
        return StepOutput(result={"reopened": False}, observations="No mission")
    reopened = 0
    for g in mission.goals:
        if g.type in ("fig_review", "curate") and g.status == "complete":
            g.status = "incomplete"
            reopened += 1
    if reopened:
        await effects.save_mission(mission)
    return StepOutput(
        result={"reopened": reopened > 0},
        observations=f"Reopened {reopened} curation goal(s) after gate failure",
    )
