"""Translation drain: non-English extracted markdowns → English, gated.

The lingual verdict (extraction_actions.extract_lingual) preserves faithful
non-Latin extractions whose numerics verified but whose span score measured
English prose that wasn't there. This module owns the follow-up: translate
the markdown chunk-by-chunk on muse's TEXT seats (idle through the
network-bound discovery window; the parallel step mounts this as a branch),
then verify the translation DETERMINISTICALLY before booking it back into
the corpus:

  numeric preservation ≥ TRANSLATE_MIN_NUMERIC — numbers are
      language-invariant, so a faithful translation keeps the source's
      numeric tokens (the same anchor extract_batch and the curator's
      grounding_check stand on);
  <img> tag count equality — figtext anchoring must survive;
  repetition run-length guard — the degeneration check, because a looped
      decode preserves every number and would pass the anchor;
  length-ratio sanity — a translation that halved or tripled the text
      did something other than translate.

Pass → extraction_status "extracted" + md_en_path (+ translated flag): the
paper enters the curator like any other, and build_curator_doc prefers the
English markdown. Fail → one retry at a higher temperature next round,
then translate_failed with the gate's reasons. Mission-clean throughout:
markdown + databank writes only.
"""

from __future__ import annotations

import logging
import os
import re

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Mirrors tools/pdf_extract/extract_batch.py::_NUM_RE and
# curation_actions._NUM_RE (separate venvs / shared repo — keep in sync).
_NUM_RE = re.compile(r"-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d{2,}")
_IMG_RE = re.compile(r"<img\b", re.IGNORECASE)

# Chunk target in characters (~3k tokens at ~4 chars/token): fits a turn
# inside the 32k shared cell beside the static head and a sibling seat.
_CHUNK_CHARS = 12_000
# Concurrent translation turns — 2 of the 4 batched seats; discovery's own
# refine_queries turn and the engine's slack keep the rest.
_TRANSLATE_SEATS = 2

TRANSLATE_MIN_NUMERIC = 0.98
TRANSLATE_MAX_ATTEMPTS = 2
# Length-ratio sanity band for translated/source text (whitespace-free).
_RATIO_MIN, _RATIO_MAX = 0.4, 2.5
_MAX_REPEAT_WORDS = 200

_TRANSLATE_CLAIMS: set[str] = set()


def _budget_chunks() -> int:
    raw = os.environ.get("OUROBOROS_TRANSLATE_CHUNKS", "").strip()
    try:
        return max(0, int(raw)) if raw else 8
    except ValueError:
        return 8


def chunk_markdown(md: str, target_chars: int = _CHUNK_CHARS) -> list[str]:
    """Split on paragraph boundaries into ~target_chars chunks.

    Paragraph blocks are never split (tables and fenced blocks live inside
    one block), so reassembly with "\\n\\n" is identity on the source."""
    blocks = md.split("\n\n")
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for block in blocks:
        if cur and size + len(block) > target_chars:
            chunks.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(block)
        size += len(block) + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _numeric_preservation(src: str, out: str) -> float:
    """Fraction of the source's numeric tokens present in the output."""
    src_tokens = _NUM_RE.findall(src)
    if not src_tokens:
        return 1.0
    out_compact = re.sub(r"[\s,]", "", out)
    hit = sum(1 for t in src_tokens if t.lstrip("-") in out_compact)
    return hit / len(src_tokens)


def _max_repeat_words(text: str, max_period: int = 12) -> int:
    """Longest back-to-back repeated word block (degeneration signal).
    Simplified mirror of tools/pdf_extract/extract_batch.py."""
    words = text.split()
    best = 0
    for period in range(1, max_period + 1):
        run = 0
        for i in range(period, len(words)):
            if words[i] == words[i - period]:
                run += 1
                if run + period > best:
                    best = run + period
            else:
                run = 0
    return best


def translation_gate(src: str, out: str) -> dict:
    """Deterministic verdict on one assembled translation."""
    numeric = _numeric_preservation(src, out)
    img_src, img_out = len(_IMG_RE.findall(src)), len(_IMG_RE.findall(out))
    src_len = max(1, len(re.sub(r"\s", "", src)))
    ratio = len(re.sub(r"\s", "", out)) / src_len
    repeats = _max_repeat_words(out)
    problems = []
    if numeric < TRANSLATE_MIN_NUMERIC:
        problems.append(f"numeric preservation {numeric:.3f} < {TRANSLATE_MIN_NUMERIC}")
    if img_out != img_src:
        problems.append(f"img tags {img_out} != source {img_src}")
    if not (_RATIO_MIN <= ratio <= _RATIO_MAX):
        problems.append(
            f"length ratio {ratio:.2f} outside [{_RATIO_MIN}, {_RATIO_MAX}]"
        )
    if repeats > _MAX_REPEAT_WORDS:
        problems.append(
            f"degenerate: {repeats} repeated words (limit {_MAX_REPEAT_WORDS})"
        )
    return {
        "passed": not problems,
        "numeric_preservation": round(numeric, 4),
        "img_tags": [img_src, img_out],
        "length_ratio": round(ratio, 3),
        "max_repeat_words": repeats,
        "problems": problems,
    }


def _render_translate_prompt(chunk: str, script_hint: str) -> str:
    from agent.runtime import _get_prompt_renderer

    namespaces = {
        "input": {},
        "context": {"chunk": chunk, "script_hint": script_hint or "non-Latin"},
        "meta": {"flow_name": "translate_drain", "step_id": "drain"},
    }
    static_prefix, dynamic = _get_prompt_renderer().render_with_cache_split(
        "scraper/translate_chunk", namespaces
    )
    return static_prefix + dynamic


# Partial-progress store (the book-segment pattern): a paper larger than
# one round's chunk budget accumulates translated chunks here across
# rounds and books only when complete. Without this, deterministic
# selection + a whole-paper budget check head-of-line-blocked the lane:
# the first over-budget paper was re-selected and re-declined every
# window, and `translated` froze while the lingual queue tripled.
_PARTS_DIR = "databank/translations"


def _parts_path(key: str) -> str:
    return f"{_PARTS_DIR}/{key}.parts.jsonl"


async def _load_parts(
    effects, key: str, n_chunks: int, src_len: int, attempt: int
) -> dict[int, str]:
    """Valid persisted chunk translations for THIS source and attempt.

    A part is valid only if it was cut from the same chunking (n, src_len)
    and the same attempt (a warmer retry re-translates everything —
    mixing temperatures inside one assembly would blur the gate's verdict).
    """
    import json

    fc = await effects.read_file(_parts_path(key))
    if not getattr(fc, "exists", False) or not fc.content.strip():
        return {}
    parts: dict[int, str] = {}
    for line in fc.content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            n_ok = int(d.get("n", -1)) == n_chunks
            src_ok = int(d.get("src_len", -1)) == src_len
            # `.get(...) or -1` would turn a legitimate attempt 0 into -1
            # and silently invalidate every first-attempt part.
            att_ok = int(d.get("attempt", -1)) == attempt
        except (TypeError, ValueError):
            continue
        if n_ok and src_ok and att_ok and str(d.get("text") or "").strip():
            parts[int(d["idx"])] = str(d["text"])
    return parts


async def _append_parts(
    effects, key: str, new: dict[int, str], n_chunks: int, src_len: int, attempt: int
) -> None:
    import json

    lines = "".join(
        json.dumps(
            {
                "idx": i,
                "n": n_chunks,
                "src_len": src_len,
                "attempt": attempt,
                "text": t,
            },
            ensure_ascii=False,
        )
        + "\n"
        for i, t in sorted(new.items())
    )
    if lines:
        await effects.append_file(_parts_path(key), lines)


def select_translation_paper(databank: dict) -> str | None:
    """One unclaimed extract_lingual paper with retry budget left
    (deterministic order — which doubles as finish-first: a partially
    translated paper keeps being selected until it completes)."""
    from agent.actions.extraction_actions import _translation_pending

    for key in sorted(databank):
        r = databank[key]
        if (
            _translation_pending(r)
            and key not in _TRANSLATE_CLAIMS
            and int(r.get("translate_attempts") or 0) < TRANSLATE_MAX_ATTEMPTS
        ):
            return key
    return None


async def action_translate_drain_batch(step_input: StepInput) -> StepOutput:
    """Translate one claimed lingual paper (chunk-budgeted) — the
    translate_drain flow's work step, built to ride as a parallel branch.

    Inputs: working_directory. Result: paper, chunks, gate, status, reason.
    """
    from agent.actions.scholarly_actions import (
        append_extraction_records,
        read_databank,
    )

    effects = step_input.effects
    budget = _budget_chunks()

    def _decline(reason: str) -> StepOutput:
        summary = {"paper": "", "chunks": 0, "reason": reason}
        return StepOutput(
            result=summary,
            observations=f"translate drain idle ({reason})",
            context_updates={"translate_summary": summary},
        )

    if budget <= 0:
        return _decline("disabled")
    if effects is None or not hasattr(effects, "run_inference"):
        return _decline("no inference effects")

    databank = await read_databank(effects)
    key = select_translation_paper(databank)
    if key is None:
        return _decline("nothing unclaimed pending")
    rec = dict(databank[key])

    md_rel = str(rec.get("md_path") or "")
    fc = await effects.read_file(md_rel)
    if not getattr(fc, "exists", False) or not fc.content.strip():
        return _decline(f"markdown unreadable: {md_rel}")
    src = fc.content
    chunks = chunk_markdown(src)

    _TRANSLATE_CLAIMS.add(key)
    try:
        import asyncio

        profile = rec.get("script_profile") or {}
        hint = max(
            (s for s in ("cyrillic", "cjk", "hangul", "greek")),
            key=lambda s: float(profile.get(s) or 0.0),
            default="non-Latin",
        )
        attempts = int(rec.get("translate_attempts") or 0)
        # Retry rounds run warmer: the first failure is often a too-literal
        # decode loop or an omitted passage; temperature is the lever.
        temperature = 0.3 if attempts == 0 else 0.7
        sem = asyncio.Semaphore(_TRANSLATE_SEATS)

        # ROUND SLICE. Papers over the round budget make PROGRESS instead
        # of blocking: translate up to `budget` missing chunks, persist
        # them, and assemble+gate only when every chunk has a valid part.
        done = await _load_parts(effects, key, len(chunks), len(src), attempts)
        todo = [i for i in range(len(chunks)) if i not in done][:budget]

        async def one(idx: int):
            async with sem:
                result = await effects.run_inference(
                    _render_translate_prompt(chunks[idx], hint),
                    {
                        "temperature": temperature,
                        "max_tokens": max(1024, int(len(chunks[idx]) / 2)),
                    },
                )
                if getattr(result, "error", None):
                    raise RuntimeError(str(result.error))
                text = str(getattr(result, "text", "") or "")
                if not text.strip():
                    raise RuntimeError("empty translation text")
                return idx, text

        results = await asyncio.gather(*(one(i) for i in todo), return_exceptions=True)
        fresh = {
            i: t for r in results if not isinstance(r, BaseException) for i, t in [r]
        }
        errors = [r for r in results if isinstance(r, BaseException)]
        await _append_parts(effects, key, fresh, len(chunks), len(src), attempts)
        done.update(fresh)

        if errors:
            # Persisted what succeeded; the round ends without a verdict and
            # the paper resumes next window.
            return _decline(
                f"{key}: {len(errors)} chunk failure(s), "
                f"{len(done)}/{len(chunks)} banked ({str(errors[0])[:100]})"
            )
        if len(done) < len(chunks):
            summary = {
                "paper": key,
                "chunks": len(chunks),
                "status": "progress",
                "banked": len(done),
            }
            return StepOutput(
                result=summary,
                observations=(
                    f"translate drain: {key} — {len(done)}/{len(chunks)} "
                    f"chunk(s) banked, continues next round"
                ),
                context_updates={"translate_summary": summary},
            )

        out_md = "\n\n".join(done[i] for i in range(len(chunks)))
        gate = translation_gate(src, out_md)

        # Any verdict ends this attempt's parts: passed/failed-final leave
        # the pending pool; a warmer retry re-translates everything (the
        # attempt key already invalidates old parts — this just reclaims
        # the space).
        await effects.write_file(_parts_path(key), "")
        rec["translate_attempts"] = attempts + 1
        if gate["passed"]:
            en_rel = (
                md_rel[:-3] + ".en.md" if md_rel.endswith(".md") else md_rel + ".en"
            )
            await effects.write_file(en_rel, out_md)
            rec["extraction_status"] = "extracted"
            rec["md_en_path"] = en_rel
            rec["translated"] = True
            rec["translation_quality"] = {
                k: gate[k] for k in ("numeric_preservation", "img_tags", "length_ratio")
            }
            rec["failure_reason"] = ""
            status = "translated"
        elif rec["translate_attempts"] >= TRANSLATE_MAX_ATTEMPTS:
            rec["extraction_status"] = "translate_failed"
            rec["failure_reason"] = "translation: " + "; ".join(gate["problems"])
            status = "failed"
        else:
            # Stays extract_lingual; the bumped attempt count selects the
            # warmer retry next round.
            rec["failure_reason"] = "translation (will retry warmer): " + "; ".join(
                gate["problems"]
            )
            status = "retry"
        await append_extraction_records(effects, [rec])
    finally:
        release_translation_keys([key])

    summary = {
        "paper": key,
        "chunks": len(chunks),
        "status": status,
        "gate": gate,
    }
    return StepOutput(
        result=summary,
        observations=(
            f"translate drain: {key} — {len(chunks)} chunk(s), {status} "
            f"(numeric {gate['numeric_preservation']})"
        ),
        context_updates={"translate_summary": summary},
    )


def release_translation_keys(keys: list[str]) -> None:
    _TRANSLATE_CLAIMS.difference_update(keys)
