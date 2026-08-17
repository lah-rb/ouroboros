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
from agent.paths import repo_root as _repo_root

import difflib
import json
import os
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
    # Match UNSIGNED: _norm strips '-' from the doc (markdown dash
    # punctuation), so a signed packed token can never match — live,
    # every negative quantity (Curie-Weiss theta, mixing enthalpies,
    # interaction parameters) failed grounding while sitting verbatim
    # in the paper's tables. Magnitude+digits is the grounding anchor;
    # the sign is not a fabrication discriminator.
    ungrounded = [
        {"path": path, "token": tok}
        for path, tok in tokens
        if (u := tok.lstrip("-")) not in doc_n and u not in doc_compact
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


def _types_compatible(expected: str, actual: str) -> bool:
    """T and list[T] are one vocabulary slot, not a mismatch.

    The registry pins a key's type from its FIRST paper; a later paper
    with several phases/samples honestly needs a list of the same
    scalar (live: lattice_parameter_angstrom, one phase then three).
    Real drift (number vs string) still fails.
    """
    return actual == f"list[{expected}]" or expected == f"list[{actual}]"


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
_EXTRACTION_USABLE = ("extracted", "extract_unverified")


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
    figure. It is not safe against the endpoint: muse reads a figure in ~37s
    (measured 2026-08-12, 4 figures in 147.9s), and this corpus has papers
    with 54, 53 and 40 figures. Three of those in one dispatch is ~90 minutes
    against a 3600s timeout — the batch would die mid-flight and every paper
    in it would book figtext_failed, having done the work.

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
_FIGTEXT_CLAIMS: set[str] = set()


def _figtext_drain_budget() -> int:
    raw = os.environ.get("OUROBOROS_FIGTEXT_FIGS", "").strip()
    try:
        return max(0, int(raw)) if raw else 6
    except ValueError:
        return 6


def select_figtext_batch(databank: dict, max_figures: int) -> list[str]:
    """Claimed, figure-budgeted paper selection for the drain."""
    batch: list[str] = []
    figures = 0
    for key in sorted(k for k, r in databank.items() if _fig_pending(r)):
        if key in _FIGTEXT_CLAIMS:
            continue
        n = int((databank.get(key) or {}).get("figure_count") or 0)
        if n > max_figures:
            continue  # dedicated-dispatch material, never drain material
        if figures + n > max_figures:
            break
        batch.append(key)
        figures += n
    _FIGTEXT_CLAIMS.update(batch)
    return batch


def release_figtext_keys(keys: list[str]) -> None:
    _FIGTEXT_CLAIMS.difference_update(keys)


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
        summary = {"attempted_papers": 0, "figures": 0, "reason": reason}
        return StepOutput(
            result=summary,
            observations=f"figtext drain idle ({reason})",
            context_updates={"figtext_summary": summary},
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
    figures = sum(int((databank.get(k) or {}).get("figure_count") or 0) for k in keys)
    try:
        sub = step_input.model_copy(
            update={"inputs": {**dict(step_input.inputs or {}), "paper_keys": keys}}
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
    }
    return StepOutput(
        result=summary,
        observations=(
            f"figtext drain: {figures} figure(s) across {len(keys)} paper(s) — "
            f"{summary['done']} done, {summary['failed']} failed"
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
_CURATE_DOC_CACHE: dict[str, int] = {}  # paper_key -> curator-doc chars

# Seat-budget derivation (tokens), against the live shared cell:
#   static prefix 1,765 + three sibling lanes at their measured p95
#   (3 x ~6.8k) + review answer 4k + pack answer 8k + prompt bodies ~1.5k.
# Everything left is doc room. Chars/token ~3.3 measured on this corpus's
# admitted markdown (18.25M muse tokens over ~60MB text).
_CURATE_RESERVE_TOKENS = 36_000
_CURATE_CHARS_PER_TOKEN = 3.3


class _CurateTransportFault(Exception):
    """Server/transport failure — decline the round, book NOTHING."""


def _curate_drain_budget() -> int:
    raw = os.environ.get("OUROBOROS_CURATE_PAPERS", "").strip()
    try:
        return max(0, int(raw)) if raw else 1
    except ValueError:
        return 1


async def _curate_doc_budget_chars(effects) -> int:
    """Doc budget scoped to the LIVE cell (health, never config).

    0 means "don't run": server unreachable, or the cell is too small for
    whole-paper work beside the sibling lanes — under the 32k cell this
    drain self-gates OFF and costs nothing until the cell is grown.
    """
    raw = os.environ.get("OUROBOROS_CURATE_DOC_CHARS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    try:
        pool = await effects.inference_pool_health()
    except Exception:  # noqa: BLE001 — unreachable server just declines
        return 0
    cell = int((pool or {}).get("kvPoolTokens") or 0)
    doc_tokens = cell - _CURATE_RESERVE_TOKENS
    if doc_tokens < 4_000:
        return 0
    return int(doc_tokens * _CURATE_CHARS_PER_TOKEN)


async def select_curate_paper(
    effects, databank: dict, budget_chars: int
) -> tuple[str, str]:
    """Claim the smallest unclaimed curation-pending paper that fits.

    Smallest-first is the coverage policy: the drain eats the corpus from
    the short end, and papers over the seat budget are simply left for
    dedicated curator dispatches (or a bigger cell) — never truncated.
    """
    sized: list[tuple[int, str]] = []
    for key, rec in databank.items():
        if key in _CURATE_CLAIMS or not _curation_pending(rec):
            continue
        chars = _CURATE_DOC_CACHE.get(key)
        if chars is None:
            doc = await _build_doc_for(effects, key)
            chars = len(doc)
            _CURATE_DOC_CACHE[key] = chars
        if 0 < chars <= budget_chars:
            sized.append((chars, key))
    if not sized:
        return "", ""
    _, key = min(sized)
    doc = await _build_doc_for(effects, key)
    if len(doc) > budget_chars:  # doc changed since caching (e.g. new en.md)
        _CURATE_DOC_CACHE[key] = len(doc)
        return "", ""
    _CURATE_CLAIMS.add(key)
    return key, doc


def release_curate_keys(keys: list[str]) -> None:
    _CURATE_CLAIMS.difference_update(keys)


async def _curate_turn(effects, prompt: str, max_tokens: int):
    result = await effects.run_inference(
        prompt, {"max_tokens": max_tokens, "temperature": "t*0.4"}
    )
    if getattr(result, "error", None):
        raise _CurateTransportFault(str(result.error))
    return result.text or ""


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
        '("accept" or "deny"), "summary", and "issues".',
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

    verdict = "accepted" if review["verdict"] == "accept" else "denied"
    state["review"] = {
        "status": verdict,
        "summary": str(review.get("summary") or "").strip(),
        "issues": [str(i) for i in (review.get("issues") or [])][:20],
        "deny_category": (
            str(review.get("deny_category") or "").strip().lower()
            if verdict == "denied"
            else ""
        ),
    }
    if verdict == "denied":
        return state

    registry = await _load_registry(effects)
    attempts = 0
    gates: dict = {"passed": False, "feedback": ""}
    data = None
    feedback = ""
    for _ in range(2):  # attempt 2 renders with attempt 1's gate findings
        attempts += 1
        pack_prompt = await _render_prompt(
            "curator/pack_data",
            {
                "key_registry_block": format_key_registry(registry),
                "gate_feedback": feedback,
            },
        )
        text = await _curate_turn(effects, doc + "\n\n---\n\n" + pack_prompt, 8192)
        parsed = parse_llm_json(text)
        data = parsed if isinstance(parsed, dict) and parsed else None
        gates = (
            _run_pack_gates(data, doc, registry)
            if data is not None
            else {"passed": False, "feedback": "output was not a JSON object"}
        )
        if gates["passed"]:
            break
        feedback = gates["feedback"]

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
        return state
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
        },
    }
    return state


async def action_curate_drain_batch(step_input):
    """Curate ONE seat-sized paper per round — the curate_drain flow's work
    step, built to ride as a parallel branch on an idle batched text seat.

    Stateless review+pack (context peak = doc + one answer), booked through
    action_curate_book_result so the envelope, registry update and
    tag_review_agreement ride the production path. Transport faults decline
    the round with nothing booked; the claim releases either way.

    Inputs: working_directory. Result: attempted, outcome, paper_key.
    """
    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepInput, StepOutput

    effects = step_input.effects

    def _decline(reason: str) -> StepOutput:
        summary = {"attempted": 0, "reason": reason}
        return StepOutput(
            result=summary,
            observations=f"curate drain idle ({reason})",
            context_updates={"curate_drain_summary": summary},
        )

    if _curate_drain_budget() <= 0:
        return _decline("disabled")
    if effects is None:
        return _decline("no effects")
    budget_chars = await _curate_doc_budget_chars(effects)
    if budget_chars <= 0:
        return _decline("cell below whole-paper threshold or server unreachable")

    databank = await read_databank(effects)
    key, doc = await select_curate_paper(effects, databank, budget_chars)
    if not key:
        return _decline("nothing unclaimed fits the seat budget")

    try:
        try:
            state = await _curate_stateless(effects, key, doc)
        except _CurateTransportFault as e:
            logger.warning("curate drain transport fault on %s: %s", key, e)
            return _decline(f"transport fault ({str(e)[:120]})")
        except Exception:  # noqa: BLE001 — code faults must not burn papers
            logger.exception("curate drain errored on %s — declining, not booking", key)
            return _decline("internal error (see log)")
        out = await action_curate_book_result(
            StepInput(effects=effects, context={"curate_state": state})
        )
        _CURATE_DOC_CACHE.pop(key, None)
    finally:
        release_curate_keys([key])

    outcome = str((out.result or {}).get("status") or state["review"].get("status", ""))
    summary = {
        "attempted": 1,
        "paper_key": key,
        "outcome": outcome,
        "doc_chars": len(doc),
    }
    return StepOutput(
        result=summary,
        observations=f"curate drain: {key} → {outcome} ({len(doc)} chars)",
        context_updates={"curate_drain_summary": summary},
    )


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

    databank = await read_databank(effects)
    updates, done, failed, skipped = [], 0, 0, 0
    for k in keys:
        rec = dict(databank.get(k) or {"paper_key": k})
        rep = reports.get(k)
        err = (rep or {}).get("error") or "no report from tool"
        if rep is not None and not rep.get("error"):
            rec["figtext_status"] = "figtext_done"
            rec["figtext_path"] = f"{FIGTEXT_DIR}/{k}.json"
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

    status = "success" if done else "failed"
    summary = f"Fig review: {done} done, {failed} failed of {len(keys)}" + (
        f", {skipped} deferred (toolchain fault)" if skipped else ""
    )
    return StepOutput(
        result={"status": status, "done": done, "failed": failed},
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


async def _build_doc_for(effects, paper_key: str) -> str:
    # Prefer the gated English translation when the translation drain has
    # produced one (translation preserves numbers and <img> paths verbatim,
    # so figtext anchoring and the grounding gate work unchanged).
    fc = await effects.read_file(f"databank/markdown/{paper_key}.en.md")
    if not getattr(fc, "exists", False):
        fc = await effects.read_file(f"databank/markdown/{paper_key}.md")
    md = fc.content if getattr(fc, "exists", False) else ""
    figtext = await _load_figtext(effects, paper_key)
    return build_curator_doc(md, figtext)


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
                '("accept" or "deny"), "summary", and "issues".',
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

        verdict = "accepted" if review["verdict"] == "accept" else "denied"
        state["review"] = {
            "status": verdict,
            "summary": str(review.get("summary") or "").strip(),
            "issues": [str(i) for i in (review.get("issues") or [])][:20],
            # WHAT COULD BE DONE ABOUT IT. A denial keeps the paper, so the
            # only question that matters afterwards is whether anything can
            # recover it. Unrecognized values are kept verbatim rather than
            # coerced: an unexpected category is a signal about the prompt,
            # and silently rewriting it to "other" would erase that.
            "deny_category": (
                str(review.get("deny_category") or "").strip().lower()
                if verdict == "denied"
                else ""
            ),
        }
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
    rec["review_summary"] = review.get("summary") or ""
    rec["review_issues"] = review.get("issues") or []
    rec["deny_category"] = review.get("deny_category") or ""
    rec["tag_review_agreement"] = tag_review_agreement(rec)

    outcome = rec["review_status"]
    if rec["review_status"] == "accepted":
        if pack.get("status") == "packed":
            data = pack.get("data") or {}
            envelope = {
                "paper_key": paper_key,
                "title": rec.get("title", ""),
                "doi": rec.get("doi", ""),
                "arxiv_id": rec.get("arxiv_id", ""),
                "license": rec.get("license", "") or "unknown",
                "year": rec.get("year", 0),
                "review": {
                    "status": "accepted",
                    "summary": rec["review_summary"],
                },
                "data": data,
                "provenance": {
                    "model": _active_text_model(),
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
                update_key_registry(registry, data, paper_key)
                await _save_registry(effects, registry)
                rec["pack_status"] = "packed"
                rec["dataset_path"] = dataset_path
                rec["pack_quality"] = pack.get("quality") or {}
                rec["failure_reason"] = ""
                outcome = f"packed ({len(data)} keys)"
        elif pack.get("status") == "needs_repack":
            rec["pack_status"] = "needs_repack"
            outcome = "needs_repack"
        else:
            rec["pack_status"] = "pack_failed"
            rec["failure_reason"] = f"pack: {pack.get('reason') or 'no pack state'}"
            outcome = "pack_failed"
    rec["curation_method"] = f"{_active_text_model()}+{figtext_model}"
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
