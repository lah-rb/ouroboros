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
English markdown. Fail → warmer retries on later rounds, then
translate_failed once the paper has spent TRANSLATE_MAX_ATTEMPTS attempts
without converging. An ATTEMPT is any round that ends without a
translation: a gate verdict, OR a chunk the server refused on both
temperatures (its degenerate-generation guard, an empty answer). Only gate
verdicts counted before 2026-09-04, so two unfixable chunks re-selected
their papers for 16 hours -- 297 aborted streams, 23% of the local
server's decode time -- with the counter sitting at zero. Mission-clean
throughout: markdown + databank writes only.
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

# Language-code → name for the prompt's source hint. Codes the corpus has
# actually produced (the extraction-side stopword vote + catalog metadata);
# an unknown code passes through verbatim — a code beats "cyrillic"-by-bug.
_LANGUAGE_NAMES = {
    "es": "Spanish",
    "fr": "French",
    "pt": "Portuguese",
    "de": "German",
    "it": "Italian",
    "ru": "Russian",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
}

TRANSLATE_MIN_NUMERIC = 0.98
# Per-chunk retry threshold — looser than the assembly bar on purpose: a
# chunk is a small sample (a few dozen tokens), so one boundary artifact
# shouldn't force a retry; the assembly gate still holds 0.98 overall.
_CHUNK_MIN_NUMERIC = 0.95
# Three, counting error-ended rounds (operator ruling 2026-09-04): a paper
# that has not converged after its third attempt is marked translate_failed
# with the reason, so a human can clear it, and never re-selected.
TRANSLATE_MAX_ATTEMPTS = 3
# Output budget per source TOKEN, applied when the server's tokenizer
# answers (effects.token_count — the size_request idiom). The honest
# expectation for a translation is ~1.0x source tokens across this
# corpus's scripts; the one large stream observed running to completion
# (2026-08-19) generated 0.70x. 1.5 covers both with real headroom, and
# a truncated output is the worse failure — it drops trailing numbers,
# fails the numeric gate, and burns one of the paper's two attempts.
_TRANSLATE_OUT_MARGIN = 1.5
# Length-ratio sanity band for translated/source text (whitespace-free).
_RATIO_MIN, _RATIO_MAX = 0.4, 2.5
_MAX_REPEAT_WORDS = 200

_TRANSLATE_CLAIMS: set[str] = set()
# Papers whose last round ended in CHUNK failure(s). Selection is
# finish-first by design — "a partially translated paper keeps being
# selected until it completes" — and until 2026-09-04 attempts advanced
# only at ASSEMBLY, so a chunk that failed every retry re-selected its
# paper forever without burning an attempt (live 2026-08-19: one
# degenerating 12.6k-token chunk held the lane for hours while 87 eligible
# papers sat at zero attempts; live 2026-09-03/04: two such chunks, 297
# aborted streams). Deferral ORDERS the pool — deferred papers sort LAST,
# not out, and banked parts survive — while the attempt cap BOUNDS the
# loop: an error-ended round now counts as an attempt too. In-process on
# purpose, like the claims set — a restart forgiving all deferrals is the
# right amnesty.
_TRANSLATE_DEFERRED: set[str] = set()


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


# Reference-section headings across the corpus's languages. Character-level
# \s* because CJK journals typeset headings with inter-character spacing
# ("参 考 文 献", "文 献" — both live in this corpus).
_REFS_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:"
    r"参\s*考\s*文\s*献|引\s*用\s*文\s*献|文\s*献|"
    r"references?|bibliography|literatur(?:verzeichnis)?|"
    r"referencias|références|참\s*고\s*문\s*헌|список\s+литературы"
    r")\s*\.?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def strip_reference_section(md: str) -> str:
    """Body of the document: everything before the reference-list heading.

    THE GATE WAS MEASURING FURNITURE (the extraction-gate lesson, recurred):
    on the live failures, up to 69% of a paper's numeric tokens were
    reference-list years/volumes/pages/DOIs, weighed identically to
    measurement values — so a translator reformatting citations failed the
    0.98 bar while preserving every number the curator will ever ground
    against. The verdict must score the BODY. When no heading matches, the
    full text stands (over-stripping would blind the gate for real)."""
    m = _REFS_HEADING_RE.search(md)
    return md[: m.start()] if m else md


def _numeric_preservation(src: str, out: str) -> float:
    """Fraction of the source BODY's numeric tokens present in the output.

    The source is stripped of its reference section (furniture — see
    strip_reference_section); the output is searched in FULL, so a
    translation keeping its references can only gain, never lose."""
    src_tokens = _NUM_RE.findall(strip_reference_section(src))
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


_EN_FUNCTION_WORDS = frozenset(
    "the and of to in is for with that this from were was are which by an be as on at "
    "or these have has not also can between".split()
)
TRANSLATE_MIN_EN_RATIO = (
    0.05  # calibrated on 1,890 English packs: p1 of true English ~0.07
)
_LATIN_WORD_RE = re.compile(r"[a-zA-Z]+")


def _output_language_problem(out: str) -> str:
    """'' when the translation reads as English; otherwise why not."""
    letters = sum(1 for ch in out if ch.isalpha()) or 1
    cjk = sum(
        1 for ch in out if "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff"
    )
    cyr = sum(1 for ch in out if "\u0400" <= ch <= "\u04ff")
    hangul = sum(1 for ch in out if "\uac00" <= ch <= "\ud7af")
    for name, n in (("CJK", cjk), ("Cyrillic", cyr), ("Hangul", hangul)):
        if n / letters >= 0.15:
            return f"output not English: {name} is {n / letters:.0%} of letters"
    words = _LATIN_WORD_RE.findall(out)
    if len(words) < 200:
        return ""  # too short to judge by function words
    tokens = re.findall(r"\S+", out)
    digit_tokens = sum(1 for t in tokens if any(ch.isdigit() for ch in t))
    if tokens and digit_tokens / len(tokens) > 0.5:
        return ""  # a table; function words are legitimately scarce
    ratio = sum(1 for w in words if w.lower() in _EN_FUNCTION_WORDS) / len(words)
    if ratio < TRANSLATE_MIN_EN_RATIO:
        return f"output not English: function-word ratio {ratio:.3f} < {TRANSLATE_MIN_EN_RATIO}"
    return ""


def translation_gate(src: str, out: str) -> dict:
    """Deterministic verdict on one assembled translation."""
    numeric = _numeric_preservation(src, out)
    img_src, img_out = len(_IMG_RE.findall(src)), len(_IMG_RE.findall(out))
    src_len = max(1, len(re.sub(r"\s", "", src)))
    ratio = len(re.sub(r"\s", "", out)) / src_len
    repeats = _max_repeat_words(out)
    problems = []
    # THE OUTPUT MUST BE ENGLISH. Measured 2026-09-06: nine packs had been cut
    # from an .en.md that was still Russian, Japanese or Spanish -- the gate
    # checked numbers, image tags, length and repetition, never the language,
    # so a model that echoed its source passed. Two tests: the source script
    # must not dominate the output, and Latin output must carry English
    # function words. Numeric-dense outputs (tables) legitimately have few
    # function words, so the ratio test is skipped when digits dominate.
    lang = _output_language_problem(out)
    if lang:
        problems.append(lang)
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
    """Valid persisted chunk translations for THIS source and epoch.

    A part is valid only if it was cut from the same chunking (n, src_len)
    and the same epoch (a warmer retry after a FAILED GATE re-translates
    everything — mixing temperatures inside one assembly would blur the
    gate's verdict). The JSON field is still named "attempt": until
    2026-09-04 attempt and epoch were the same number, and every part on
    disk carries that name.
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


def _tag_priority(record: dict) -> int:
    """0 = tagged exact/close (corpus-bound), 1 = adjacent-only/untagged.

    The multilingual wave sweeps in humanities/pedagogy strays (a
    translation-studies paper, a design paper — live finds) that the
    curator will deny after we spend seats translating them. Strong-tagged
    papers translate first; the strays still get their turn, just last."""
    tiers = {
        str(t.get("relevance") or "").strip().lower()
        for t in (record.get("tags") or [])
        if isinstance(t, dict)
    }
    return 0 if tiers & {"exact", "close"} else 1


def select_translation_paper(databank: dict) -> str | None:
    """One unclaimed ACCEPTED extract_lingual paper with retry budget left.

    Post-acceptance by design (see _translation_pending): curation reads
    originals, so only papers the curator accepted spend translate seats.
    Ordered by (tag strength, key): relevance first, then deterministic —
    which doubles as finish-first: a partially translated paper keeps
    being selected until it completes."""
    from agent.actions.extraction_actions import _translation_pending

    eligible = [
        key
        for key, r in databank.items()
        if _translation_pending(r)
        and key not in _TRANSLATE_CLAIMS
        and int(r.get("translate_attempts") or 0) < TRANSLATE_MAX_ATTEMPTS
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda k: (k in _TRANSLATE_DEFERRED, _tag_priority(databank[k]), k),
    )


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

        # SOURCE HINT. The record's `language` (the extraction-side vote,
        # present on ~88% of the lingual cohort) beats any script guess —
        # the old profile-max fired unconditionally and its default was
        # dead code (max over a non-empty literal tuple), so every
        # Latin-script Spanish/French/German paper was prompted "source
        # script: cyrillic". Language first; a real non-Latin script
        # second (only when actually present); the honest unknown last.
        lang = str(rec.get("language") or "").strip().lower()
        profile = rec.get("script_profile") or {}
        if lang and lang != "en":
            hint = _LANGUAGE_NAMES.get(lang, lang)
        else:
            present = [
                s
                for s in ("cyrillic", "cjk", "hangul", "greek")
                if float(profile.get(s) or 0.0) >= 0.05
            ]
            hint = (
                max(present, key=lambda s: float(profile.get(s) or 0.0))
                if present
                else "non-Latin"
            )
        attempts = int(rec.get("translate_attempts") or 0)
        # Parts are keyed by EPOCH, not attempt. An epoch is one pass over
        # the source at one temperature regime; it advances only when the
        # assembly gate FAILS, because the warmer retry re-translates
        # everything and parts from a colder pass must not mix into it. An
        # attempt that ends in a chunk ERROR keeps its epoch: the chunks
        # that banked are good, and re-translating them would spend decode
        # on work the server already did. Records from before 2026-09-04
        # carry no epoch; their attempt count IS their epoch (the two were
        # the same number then).
        raw_epoch = rec.get("translate_epoch")
        epoch = int(raw_epoch if raw_epoch is not None else attempts)
        # Retry rounds run warmer: the first failure is often a too-literal
        # decode loop or an omitted passage; temperature is the lever.
        temperature = 0.3 if attempts == 0 else 0.7
        sem = asyncio.Semaphore(_TRANSLATE_SEATS)

        # ROUND SLICE. Papers over the round budget make PROGRESS instead
        # of blocking: translate up to `budget` missing chunks, persist
        # them, and assemble+gate only when every chunk has a valid part.
        done = await _load_parts(effects, key, len(chunks), len(src), epoch)
        todo = [i for i in range(len(chunks)) if i not in done][:budget]

        # EXACT-TOKEN OUTPUT BUDGETS, one batched call for the round's
        # slice. The char heuristic it replaces (len/2) was a moving
        # target that erred in BOTH directions by script: ~1.75x source
        # tokens for Latin/Cyrillic (measured live: a 13,236-token chunk
        # entitled max_gen=22,686 and starved the pool), and BELOW the
        # expected English output length for dense CJK (~1.7 chars/tok),
        # i.e. silent truncation — which drops trailing numbers and reads
        # as a numeric-gate failure. [] or a short answer means "the
        # server would not say": every chunk falls back to the heuristic,
        # never a mix (a half-zipped dict would misbudget silently).
        tok_counts: dict[int, int] = {}
        counter = getattr(effects, "token_count", None)
        if counter is not None and todo:
            try:
                counts = await counter([chunks[i] for i in todo])
            except Exception:  # noqa: BLE001 — sizing never fails the round
                counts = []
            if counts and len(counts) == len(todo):
                tok_counts = {i: int(c) for i, c in zip(todo, counts)}

        async def one(idx: int):
            async with sem:
                # Chunk-level defect checks with ONE warmer retry: a dropped
                # <img> tag or a truncated/abridged passage in ONE chunk
                # fails the whole-paper gate and burns a paper attempt (live:
                # 7/9 tags; numeric misses down to 0.78 on real papers). Both
                # censuses are per-chunk checkable, so retry at the
                # granularity where the defect happens; the assembly gate
                # stays the authority on whatever this banks. Reference-list
                # chunks strip to zero body tokens and score 1.0, so citation
                # reformatting never churns retries.
                want_imgs = len(_IMG_RE.findall(chunks[idx]))
                text = ""
                for temp in (temperature, 0.7):
                    result = await effects.run_inference(
                        _render_translate_prompt(chunks[idx], hint),
                        {
                            "temperature": temp,
                            "max_tokens": (
                                max(1024, int(tok_counts[idx] * _TRANSLATE_OUT_MARGIN))
                                if idx in tok_counts
                                else max(1024, int(len(chunks[idx]) / 2))
                            ),
                        },
                    )
                    if getattr(result, "error", None):
                        raise RuntimeError(str(result.error))
                    text = str(getattr(result, "text", "") or "")
                    if not text.strip():
                        raise RuntimeError("empty translation text")
                    if (
                        len(_IMG_RE.findall(text)) == want_imgs
                        and _numeric_preservation(chunks[idx], text)
                        >= _CHUNK_MIN_NUMERIC
                    ):
                        break
                # BANK THIS CHUNK NOW, not after the round's gather.
                # A chunk is minutes of decode; under continuous
                # scheduling a round of eight can run 20 minutes against a
                # contended server, and banking at the end means a stop
                # anywhere in that window throws away everything. Writes
                # are serialized per path by append_file's lock, so
                # concurrent chunks appending is safe.
                await _append_parts(
                    effects, key, {idx: text}, len(chunks), len(src), epoch
                )
                return idx, text

        results = await asyncio.gather(*(one(i) for i in todo), return_exceptions=True)
        fresh = {
            i: t for r in results if not isinstance(r, BaseException) for i, t in [r]
        }
        errors = [r for r in results if isinstance(r, BaseException)]
        # Already banked by `one()` as each chunk landed — nothing to
        # write here.
        done.update(fresh)

        if errors:
            # Persisted what succeeded; the round ends without a translation,
            # and that COUNTS as an attempt. Before 2026-09-04 only a gate
            # verdict advanced the counter, so a chunk the server refused
            # every time (its degenerate-generation guard: a Cyrillic chunk
            # on a record tagged `en`, an OCR-damaged Spanish one echoing
            # its own repetition) re-selected its paper for 16 hours. The
            # EPOCH does not advance: banked chunks stay valid, so a
            # transient fault costs a counter tick, not the banked work.
            # DEFER the paper so the next round tries someone else first —
            # without this, finish-first re-offers it immediately.
            attempts += 1
            rec["translate_attempts"] = attempts
            rec["translate_epoch"] = epoch
            what = (
                f"{len(errors)} chunk failure(s) ({str(errors[0])[:100]}), "
                f"{len(done)}/{len(chunks)} banked"
            )
            if attempts >= TRANSLATE_MAX_ATTEMPTS:
                _TRANSLATE_DEFERRED.discard(key)
                rec["extraction_status"] = "translate_failed"
                rec["failure_reason"] = (
                    f"translation: {what}; did not converge in {attempts} attempts"
                )
                await effects.write_file(_parts_path(key), "")
                await append_extraction_records(effects, [rec])
                await _book_pack_state_after_translation(
                    effects, rec, "failed", rec["failure_reason"]
                )
                summary = {
                    "paper": key,
                    "chunks": len(chunks),
                    "status": "failed",
                    "reason": what,
                }
                return StepOutput(
                    result=summary,
                    observations=f"translate failed {key}: {what}",
                    context_updates={"translate_summary": summary},
                )
            _TRANSLATE_DEFERRED.add(key)
            rec["failure_reason"] = (
                f"translation (will retry warmer): {what}; "
                f"attempt {attempts} of {TRANSLATE_MAX_ATTEMPTS}"
            )
            await append_extraction_records(effects, [rec])
            return _decline(f"{key}: {what}")
        _TRANSLATE_DEFERRED.discard(key)  # a clean round earns the front again
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
        rec["translate_epoch"] = epoch
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
            await _book_pack_state_after_translation(effects, rec, "translated", "")
        elif rec["translate_attempts"] >= TRANSLATE_MAX_ATTEMPTS:
            rec["extraction_status"] = "translate_failed"
            rec["failure_reason"] = "translation: " + "; ".join(gate["problems"])
            status = "failed"
            await _book_pack_state_after_translation(
                effects, rec, "failed", rec["failure_reason"]
            )
        else:
            # Stays extract_lingual; the bumped attempt count selects the
            # warmer retry next round, and the bumped EPOCH retires this
            # pass's parts so the retry re-translates everything.
            rec["translate_epoch"] = epoch + 1
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


async def _book_pack_state_after_translation(
    effects, rec: dict, outcome: str, why: str
) -> None:
    """Keep the PACK honest about the language it was cut from.

    OPERATOR RULING 2026-09-06: the corpus feeds continued pre-training of
    models too small for multilingual packs, so a pack must be English. Two
    consequences land here, on the papers side of the databank (pack_status
    is not an extraction-owned field, so the extraction-side append the
    translate round already makes cannot carry it):

      translated  -> a paper that was PACKED from its original-language text
                     (every lingual pack before the ruling) goes to
                     needs_repack, so the English pack replaces it.
      failed      -> an ACCEPTED paper whose translation did not converge is
                     booked pack_failed: there is no English text to pack, and
                     an original-language pack already cut must not stay
                     `packed`, which is what the export reads.

    Never raises -- a booking failure must not undo a translation.
    """
    from agent.actions.scholarly_actions import append_records

    try:
        if outcome == "translated" and rec.get("pack_status") == "packed":
            await append_records(
                effects,
                [{**rec, "pack_status": "needs_repack", "failure_reason": ""}],
            )
        elif outcome == "failed" and rec.get("review_status") == "accepted":
            prior = rec.get("pack_status") or ""
            note = (
                "translation did not converge; the existing pack was cut from "
                "original-language text and must not stand"
                if prior == "packed"
                else "translation did not converge, so there is no English text to pack"
            )
            await append_records(
                effects,
                [
                    {
                        **rec,
                        "pack_status": "pack_failed",
                        "failure_reason": f"{why[:160]} | {note}",
                    }
                ],
            )
    except Exception:  # noqa: BLE001 -- see docstring
        logger.exception(
            "pack-state booking after translation failed for %s", rec.get("paper_key")
        )


def release_translation_keys(keys: list[str]) -> None:
    _TRANSLATE_CLAIMS.difference_update(keys)
