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
English markdown.

BANK AND REPAIR (operator ruling 2026-09-17: follow the OCR drain's policy,
which works). The drain is bounded, resumable work that never retires a
document for one bad segment:
  * every chunk that translates is BANKED as it lands (databank/translations/
    <key>.parts.jsonl); every chunk that fails is RECORDED there too, with its
    reason, and retried on later rounds BEHIND the chunks never tried;
  * a chunk past _CHUNK_MAX_FAILURES waits for the SALVAGE pass, which
    re-translates it in _SALVAGE_PIECES smaller pieces; what still fails is
    kept in the source language behind gap markers, and the gaps are listed
    in translation_quality.gaps for a repair pass;
  * the paper's attempt counter advances only on a round that banks NOTHING
    (or on a failed assembly gate → warmer epoch); a paper is retired
    (translate_failed) only when nothing of it ever translated, or when more
    than _GAP_MAX_FRACTION of its chunks would be gaps. The bank is kept in
    both cases — that is what "banked for repair" means.
Before this policy an attempt was any round that hit a chunk error, so a
33-chunk paper died on its third round with 15 chunks never tried; of 144
retired papers, 64 had one failing chunk and 28 were over 90 % banked.
Mission-clean throughout: markdown + databank writes only.
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

# ── Bank-and-repair knobs (see the module docstring) ──────────────────
#: Recorded failures a chunk may collect (across rounds, within one epoch)
#: before it stops being retried whole and waits for the salvage pass.
_CHUNK_MAX_FAILURES = 3
#: SOURCE-LOOP COLLAPSE (2026-09-19). A chunk whose SOURCE carries a degenerate
#: OCR run — one phrase dozens of times back to back — is translated faithfully
#: and then killed by the engine's repetition guard (12 exact repeats of a
#: short period, ~50–60 words), on every model: muse's degenerate aborts sat
#: on sources with a median distinct-n-gram ratio of 0.30 vs 0.89 for banked
#: chunks, and the 27B reproduced both of its failures on such sources. The
#: OCR census leaves runs up to 200 words in the text by design (whole-
#: document ruin was the question there). So collapse PROSE runs longer than
#: this many words into the extraction marker before prompting, and judge
#: the paper against the collapsed source. Chunk boundaries and the parts
#: bank stay bound to the ORIGINAL text. Markup units are never collapsed:
#: measured over the 2026-09-19 queue, 3,249 of 4,664 runs over 40 words were
#: the repeated cell attribute of ordinary HTML tables and 81 the
#: figure-removed marker.
_SOURCE_LOOP_WORDS = 40
#: Scripts without spaces (CJK) are invisible to the word census; a repeated
#: substring of at least this many characters, at least this many times back
#: to back, is the same loop in another alphabet.
_SOURCE_LOOP_CHARS = 6
_SOURCE_LOOP_CHAR_REPS = 8
#: The salvage pass re-cuts a failing chunk into this many smaller pieces.
_SALVAGE_PIECES = 3
#: A paper whose gaps would exceed this share of its chunks is retired
#: instead of booked — the pack must be (mostly) English.
_GAP_MAX_FRACTION = 0.5
_GAP_OPEN = (
    "<!-- translation gap: chunk {idx} of {n} kept in the source language "
    "({reason}) -->"
)
_GAP_CLOSE = "<!-- end translation gap -->"

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


def _prose_unit(unit: str) -> bool:
    """A repeated unit worth collapsing: it carries letters and no markup.
    Table cells and the figure-removed marker repeat legitimately."""
    if "<" in unit or ">" in unit or unit.count("|") >= 2:
        return False
    return re.search(r"[^\W\d_]{2,}", unit) is not None


_CHAR_LOOP_RE = re.compile(
    r"(.{%d,80}?)(?:\1){%d,}" % (_SOURCE_LOOP_CHARS, _SOURCE_LOOP_CHAR_REPS - 1),
    re.S,
)


def collapse_source_loops(text: str) -> tuple[str, dict]:
    """Collapse degenerate OCR loops in a SOURCE chunk before translation.

    Two passes: the extraction census for word-periodic prose runs over
    ``_SOURCE_LOOP_WORDS`` (markup excluded via ``_prose_unit``), then a
    character-periodic pass for the runs the word census cannot see (CJK).
    Returns (text, {"words": n, "chars": n}) — what was cut, for provenance.
    """
    from agent.actions.extraction_actions import collapse_degenerate_runs

    out, words = collapse_degenerate_runs(
        text, limit=_SOURCE_LOOP_WORDS, unit_filter=_prose_unit
    )
    chars = 0

    def _sub(m: re.Match) -> str:
        nonlocal chars
        unit = m.group(1)
        if not _prose_unit(unit):
            return m.group(0)
        reps = len(m.group(0)) // len(unit)
        chars += len(m.group(0)) - len(unit)
        return unit + (
            f"*[degenerate OCR run collapsed: {reps - 1} repeats of "
            f"{unit[:40]!r} — content at this location was not read]*"
        )

    out = _CHAR_LOOP_RE.sub(_sub, out)
    return out, {"words": words, "chars": chars}


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


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_FIG_NUM_RE = re.compile(r"(?:Fig(?:ure)?\.?|図|Table|Tab\.|表)\s*(\d+)", re.IGNORECASE)


def _strip_img_tags(text: str) -> str:
    """Image tags carry the paper KEY in their path; a Cyrillic or kanji key
    counted as letters made clean chunks read 6–10 % 'not English'
    (2026-09-19, the NdF3 Raman paper). Language is judged on prose."""
    return _IMG_TAG_RE.sub(" ", text)


def reinsert_missing_imgs(src: str, out: str) -> tuple[str, int]:
    """Put back the ``<img>`` blocks a translator dropped. Returns (text, n).

    Measured 2026-09-19 over the retired papers: every one of 20 dropped tags
    sat in the same structure — a centred ``<div>`` holding only the image,
    followed by a centred ``<div>`` holding "Fig. N …" — and the caption was
    translated while the image-only div vanished, on muse and on the 27B
    alike. The census only counts tags, so the fix is mechanical: each
    missing tag goes back as its own centred block, placed before the
    translated caption that carries the same figure/table number when one
    is found within 400 characters after the tag in the source, else at the
    end of the chunk. Never removes anything; idempotent when nothing is
    missing. Best-of-bank reassembly rescued 7 of 11 retired papers with
    this alone (6 of them purely on tags)."""
    src_tags = _IMG_TAG_RE.findall(src)
    out_tags = _IMG_TAG_RE.findall(out)
    if not src_tags or len(out_tags) >= len(src_tags):
        return out, 0
    have: dict[str, int] = {}
    for t in out_tags:
        have[t] = have.get(t, 0) + 1
    added = 0
    for tag in src_tags:
        if have.get(tag, 0) > 0:
            have[tag] -= 1
            continue
        pos = src.find(tag)
        after = src[pos + len(tag) : pos + len(tag) + 400]
        block = f'<div style="text-align: center;">{tag}</div>'
        placed = False
        m = _FIG_NUM_RE.search(after)
        if m:
            num = re.escape(m.group(1))
            cap = re.search(
                r"(?:Fig(?:ure)?\.?|図|Table|Tab\.|表)\s*%s(?!\d)" % num,
                out,
                re.IGNORECASE,
            )
            if cap:
                ls = out.rfind("\n", 0, cap.start()) + 1
                out = out[:ls] + block + "\n\n" + out[ls:]
                placed = True
        if not placed:
            out = out.rstrip() + "\n\n" + block + "\n"
        added += 1
    return out, added


def _span_problem(text_src: str, text: str) -> str:
    """The per-span verdict the two-temperature loop retries on: image
    census, numeric recall, and — since 2026-09-19 — the source script. An
    echoed chunk (the Russian NdF3 paper came back 70 % Cyrillic on two of
    six chunks) preserves every number and every tag, so the old checks let
    it bank and the paper only failed at assembly, where the warmer retry
    re-translates everything. Judged on prose (tag paths stripped)."""
    if len(_IMG_RE.findall(text)) != len(_IMG_RE.findall(text_src)):
        return "img tags"
    if _numeric_preservation(text_src, text) < _CHUNK_MIN_NUMERIC:
        return "numeric"
    return _output_language_problem(_strip_img_tags(text))


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
    lang = _output_language_problem(_strip_img_tags(out))
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


#: TRANSPORT, NOT TRANSLATION (2026-09-29). A dead or unreachable server fails
#: every chunk of every round. In run v50c LLMVP died on an NVLink fault and
#: stayed down five hours; the lanes kept translating against it and
#: - banked 533 connection errors in 100 parts files,
#: - spent an attempt on ~100 papers,
#: - retired two to translate_failed, which then failed their packs.
#: None of it says anything about a paper. Such failures are never banked,
#: never spend an attempt, and are ignored where already banked, so they
#: cannot gap a chunk either. A watchdog abort for NO PROGRESS is the server
#: stalling, not the model looping (loops abort as "cycle period" /
#: "long-cycle", which still count).
_TRANSPORT_MARKERS = (
    "all connection attempts failed",
    "cannot connect to llmvp",
    "connection refused",
    "server disconnected without sending a response",
    "all inference instances are busy",
    "aborted by watchdog (no progress)",
)


def _is_transport_failure(reason: str) -> bool:
    r = str(reason or "").lower()
    return any(m in r for m in _TRANSPORT_MARKERS)


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


async def _load_all_parts(
    effects, key: str, n_chunks: int, src_len: int
) -> dict[int, list[str]]:
    """Every banked translation of every chunk, ACROSS epochs, for this
    source (same n, same src_len) — the best-of-bank pass chooses among them
    per chunk. Epoch discipline still governs the normal assembly; this is
    the last look before a retirement."""
    import json

    fc = await effects.read_file(_parts_path(key))
    if not getattr(fc, "exists", False) or not fc.content.strip():
        return {}
    parts: dict[int, list[str]] = {}
    for line in fc.content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            if int(d.get("n", -1)) != n_chunks or int(d.get("src_len", -1)) != src_len:
                continue
        except (TypeError, ValueError):
            continue
        text = str(d.get("text") or "")
        if d.get("failed") or not text.strip():
            continue
        parts.setdefault(int(d["idx"]), []).append(text)
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


async def _load_failures(
    effects, key: str, n_chunks: int, src_len: int, attempt: int
) -> dict[int, list[str]]:
    """Recorded chunk failures for THIS chunking and epoch: idx -> reasons.

    Failure lines share the parts file (`failed: true`, no text), so
    _load_parts ignores them and a pre-policy parts file reads unchanged."""
    import json

    fc = await effects.read_file(_parts_path(key))
    if not getattr(fc, "exists", False) or not fc.content.strip():
        return {}
    out: dict[int, list[str]] = {}
    for line in fc.content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not d.get("failed"):
            continue
        try:
            if (
                int(d.get("n", -1)) != n_chunks
                or int(d.get("src_len", -1)) != src_len
                or int(d.get("attempt", -1)) != attempt
            ):
                continue
            idx = int(d["idx"])
        except (TypeError, ValueError, KeyError):
            continue
        reason = str(d.get("reason") or "")
        if _is_transport_failure(reason):
            continue  # banked before 2026-09-29; says nothing about the chunk
        out.setdefault(idx, []).append(reason)
    return out


async def _append_failure(
    effects,
    key: str,
    idx: int,
    reason: str,
    n_chunks: int,
    src_len: int,
    attempt: int,
    *,
    salvage: bool = False,
) -> None:
    import json

    line = (
        json.dumps(
            {
                "idx": idx,
                "n": n_chunks,
                "src_len": src_len,
                "attempt": attempt,
                "failed": True,
                "salvage": salvage,
                "reason": str(reason)[:200],
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    await effects.append_file(_parts_path(key), line)


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


def _doc_size_hint(record: dict) -> int:
    """Length proxy from fields the record already carries -- no file read.
    extraction_quality.pages is present on ~7,700 rows; 0 when unknown, which
    sorts unknown-length papers first rather than last (a deliberate bias:
    most rows without pages are small older extractions)."""
    q = record.get("extraction_quality") or {}
    try:
        return int(q.get("pages") or 0)
    except (TypeError, ValueError):
        return 0


def select_translation_paper(databank: dict) -> str | None:
    """One unclaimed ACCEPTED extract_lingual paper.

    Post-acceptance by design (see _translation_pending): curation reads
    originals, so only papers the curator accepted spend translate seats.
    Ordered by (tag strength, size, key): relevance first, then SMALLEST
    first, then deterministic -- which still doubles as finish-first: a
    partially translated paper keeps being selected until it completes.
    Smallest-first is the throughput policy (2026-09-06): translation time
    scales with length, and one 300-450k-token thesis would hold a lane for
    hours while dozens of 12k-token papers waited behind it; recovered packs
    per hour is the objective.

    The attempt counter no longer gates selection (2026-09-17): termination
    is the drain's decision — per-chunk failure caps, the salvage pass and
    the gap booking bound every paper, and a retired paper carries the
    terminal `translate_failed` status that _translation_pending excludes.
    Deferral (a round that hit failures) still sorts a paper LAST."""
    from agent.actions.extraction_actions import _translation_pending

    eligible = [
        key
        for key, r in databank.items()
        if _translation_pending(r) and key not in _TRANSLATE_CLAIMS
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda k: (
            k in _TRANSLATE_DEFERRED,
            _tag_priority(databank[k]),
            _doc_size_hint(databank[k]),
            k,
        ),
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
    # SOURCE-LOOP COLLAPSE (see _SOURCE_LOOP_WORDS): the prompt, the span
    # checks, the salvage pieces and the paper gate all see the collapsed
    # chunk; n and len(src) — the parts-file binding — stay on the original.
    source_collapsed = {"words": 0, "chars": 0}
    for _i, _c in enumerate(chunks):
        _c2, _rep = collapse_source_loops(_c)
        if _rep["words"] or _rep["chars"]:
            chunks[_i] = _c2
            source_collapsed["words"] += _rep["words"]
            source_collapsed["chars"] += _rep["chars"]

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

        # ROUND SLICE (the OCR drain's book-segment pattern). Papers over the
        # round budget make PROGRESS instead of blocking: translate up to
        # `budget` missing chunks, bank each as it lands, and assemble only
        # when every chunk is translated or a recorded gap. Chunks never
        # tried go first; chunks that failed before go after them, fewest
        # failures first; a chunk past _CHUNK_MAX_FAILURES waits for the
        # salvage pass. A round that banks NOTHING is the only kind that
        # spends one of the paper's attempts.
        n = len(chunks)
        done = await _load_parts(effects, key, n, len(src), epoch)
        failed = await _load_failures(effects, key, n, len(src), epoch)

        def _missing() -> tuple[list[int], list[int]]:
            """(tryable, gapped) among the chunks not yet translated."""
            miss = [i for i in range(n) if i not in done]
            untried = [i for i in miss if i not in failed]
            retry = sorted(
                (
                    i
                    for i in miss
                    if i in failed and len(failed[i]) < _CHUNK_MAX_FAILURES
                ),
                key=lambda i: (len(failed[i]), i),
            )
            gapped = [
                i for i in miss if i in failed and len(failed[i]) >= _CHUNK_MAX_FAILURES
            ]
            return untried + retry, gapped

        tryable, _gapped = _missing()
        todo = tryable[:budget]

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

        async def translate_text(text_src: str, out_tokens: int) -> str:
            """One source span → English: the two-temperature per-span loop.

            Span-level defect checks with ONE warmer retry: a dropped <img>
            tag or a truncated/abridged passage in ONE chunk would fail the
            whole-paper gate (live: 7/9 tags; numeric misses down to 0.78 on
            real papers). Both censuses are per-span checkable, so retry at
            the granularity where the defect happens; the assembly gate
            stays the authority on whatever this banks. Reference-list
            chunks strip to zero body tokens and score 1.0, so citation
            reformatting never churns retries. Raises RuntimeError with the
            engine's or the check's reason — the caller banks that."""
            want_imgs = len(_IMG_RE.findall(text_src))
            text = ""
            for temp in (temperature, 0.7):
                result = await effects.run_inference(
                    _render_translate_prompt(text_src, hint),
                    {"temperature": temp, "max_tokens": out_tokens},
                )
                if getattr(result, "error", None):
                    raise RuntimeError(str(result.error))
                text = str(getattr(result, "text", "") or "")
                if not text.strip():
                    raise RuntimeError("empty translation text")
                # Dropped image blocks are put back mechanically before the
                # span is judged (see reinsert_missing_imgs); the retry is
                # for what cannot be repaired — numbers and an echoed script.
                text, _ = reinsert_missing_imgs(text_src, text)
                if not _span_problem(text_src, text):
                    break
            return text

        def _out_budget(idx: int, text_src: str) -> int:
            return (
                max(1024, int(tok_counts[idx] * _TRANSLATE_OUT_MARGIN))
                if idx in tok_counts
                else max(1024, int(len(text_src) / 2))
            )

        async def one(idx: int):
            async with sem:
                try:
                    text = await translate_text(
                        chunks[idx], _out_budget(idx, chunks[idx])
                    )
                except Exception as exc:  # noqa: BLE001 — a chunk failure is data
                    # BANK THE FAILURE: the reason and the chunk, so the next
                    # round tries the untried chunks first and a repair pass
                    # knows exactly what to redo.
                    reason = str(exc)[:200]
                    if not _is_transport_failure(reason):
                        await _append_failure(
                            effects, key, idx, reason, n, len(src), epoch
                        )
                    return idx, None, reason
                # BANK THIS CHUNK NOW, not after the round's gather.
                # A chunk is minutes of decode; under continuous
                # scheduling a round of eight can run 20 minutes against a
                # contended server, and banking at the end means a stop
                # anywhere in that window throws away everything. Writes
                # are serialized per path by append_file's lock, so
                # concurrent chunks appending is safe.
                await _append_parts(effects, key, {idx: text}, n, len(src), epoch)
                return idx, text, None

        results = await asyncio.gather(*(one(i) for i in todo))
        fresh = {i: t for i, t, err in results if err is None}
        errs = {i: err for i, t, err in results if err is not None}
        # Already banked by `one()` as each chunk landed — nothing to
        # write here.
        done.update(fresh)
        for i, err in errs.items():
            if not _is_transport_failure(err):
                failed.setdefault(i, []).append(err)
        tryable, gapped = _missing()

        if errs and not fresh and all(_is_transport_failure(e) for e in errs.values()):
            # The SERVER failed, not the paper: no attempt spent, nothing
            # booked. Deferred, so the lane moves on instead of re-offering it.
            _TRANSLATE_DEFERRED.add(key)
            reason = str(next(iter(errs.values())))[:120]
            summary = {
                "paper": key,
                "chunks": n,
                "status": "deferred",
                "reason": reason,
            }
            return StepOutput(
                result=summary,
                observations=f"translate drain: {key} deferred, server unreachable ({reason})",
                context_updates={"translate_summary": summary},
            )

        what = (
            f"{len(errs)} chunk failure(s) ({str(next(iter(errs.values())))[:100]}), "
            f"{len(done)}/{n} banked"
            if errs
            else f"{len(done)}/{n} banked"
        )
        if errs and not fresh:
            # NO PROGRESS this round: that spends an attempt — the counter is
            # the record of rounds that produced nothing. Progress beside a
            # failure costs nothing: the failure is banked and retried behind
            # the untried chunks. The EPOCH never advances here: banked
            # chunks stay valid, so a fault costs a counter tick, not work.
            attempts += 1
            rec["translate_attempts"] = attempts
            rec["translate_epoch"] = epoch
            if not done and attempts >= TRANSLATE_MAX_ATTEMPTS:
                # Nothing of this paper has ever translated: retire it. The
                # failures stay banked (parts file kept) for a repair pass.
                _TRANSLATE_DEFERRED.discard(key)
                rec["extraction_status"] = "translate_failed"
                rec["failure_reason"] = (
                    f"translation: {what}; nothing banked after {attempts} attempts"
                )
                rec["translation_quality"] = {
                    "chunks": n,
                    "banked": 0,
                    "failed_chunks": sorted(failed),
                }
                await append_extraction_records(effects, [rec])
                await _book_pack_state_after_translation(
                    effects, rec, "failed", rec["failure_reason"]
                )
                summary = {
                    "paper": key,
                    "chunks": n,
                    "status": "failed",
                    "reason": what,
                }
                return StepOutput(
                    result=summary,
                    observations=f"translate failed {key}: {what}",
                    context_updates={"translate_summary": summary},
                )

        if tryable:
            # More to do next round. DEFER after a failure so the lane tries
            # someone else first — finish-first would re-offer this paper.
            if errs:
                _TRANSLATE_DEFERRED.add(key)
                rec["failure_reason"] = (
                    f"translation (chunk failures banked, retrying): {what}"
                )
            else:
                _TRANSLATE_DEFERRED.discard(key)  # a clean round earns the front
                rec["failure_reason"] = ""
            await append_extraction_records(effects, [rec])
            summary = {
                "paper": key,
                "chunks": n,
                "status": "progress",
                "banked": len(done),
                "failing": len(failed),
            }
            return StepOutput(
                result=summary,
                observations=(
                    f"translate drain: {key} — {len(done)}/{n} chunk(s) banked, "
                    f"{len(failed)} with recorded failures, continues next round"
                ),
                context_updates={"translate_summary": summary},
            )

        # This paper reaches a verdict this round: it leaves the deferral set.
        _TRANSLATE_DEFERRED.discard(key)

        # SALVAGE. Every chunk not translated has exhausted its retries. Each
        # gets one pass in smaller pieces — a loop or an empty answer on a
        # 12k-char chunk is often one table or reference block that
        # translates fine a third at a time. What still fails is kept in the
        # source language behind gap markers and recorded for repair.
        gaps: list[dict] = []
        for idx in gapped:
            pieces = chunk_markdown(
                chunks[idx], max(1000, _CHUNK_CHARS // _SALVAGE_PIECES)
            )
            out_pieces: list[str] = []
            salvage_err = ""
            for piece in pieces:
                try:
                    out_pieces.append(
                        await translate_text(piece, max(1024, int(len(piece) / 2)))
                    )
                except Exception as exc:  # noqa: BLE001 — recorded, not raised
                    salvage_err = str(exc)[:120]
                    break
            if not salvage_err:
                text = "\n\n".join(out_pieces)
                done[idx] = text
                await _append_parts(effects, key, {idx: text}, n, len(src), epoch)
                continue
            await _append_failure(
                effects,
                key,
                idx,
                f"salvage: {salvage_err}",
                n,
                len(src),
                epoch,
                salvage=True,
            )
            gaps.append(
                {
                    "idx": idx,
                    "reason": (failed.get(idx) or [salvage_err])[0][:120],
                    "salvage": salvage_err,
                }
            )
        gap_set = {g["idx"] for g in gaps}
        translated_idx = [i for i in range(n) if i not in gap_set]

        # Parts banked before 2026-09-19 never had their dropped image blocks
        # put back; do it here too, so an old bank passes on its own merits.
        img_reinserted = 0
        for i in translated_idx:
            done[i], _added = reinsert_missing_imgs(chunks[i], done[i])
            img_reinserted += _added

        def _assemble(parts_map: dict[int, str]) -> tuple[str, dict]:
            # ASSEMBLY. Gap chunks ride through in the source language,
            # marked. The gate judges the TRANSLATED text alone: gap chunks
            # are source by construction and would only echo the
            # source-language check.
            md = "\n\n".join(
                (
                    parts_map[i]
                    if i not in gap_set
                    else "\n".join(
                        (
                            _GAP_OPEN.format(
                                idx=i + 1,
                                n=n,
                                reason=next(g["reason"] for g in gaps if g["idx"] == i),
                            ),
                            chunks[i],
                            _GAP_CLOSE,
                        )
                    )
                )
                for i in range(n)
            )
            verdict = translation_gate(
                "\n\n".join(chunks[i] for i in translated_idx),
                "\n\n".join(parts_map[i] for i in translated_idx),
            )
            return md, verdict

        out_md, gate = _assemble(done)
        assembled_from = ""
        if not gate["passed"] and attempts + 1 >= TRANSLATE_MAX_ATTEMPTS:
            # BEST OF BANK before retiring. Every epoch's parts are still on
            # disk, and a chunk that failed this pass often passed an earlier
            # one (numbers, tags) or the reverse (language). Choose per chunk
            # on (English, tags, numbers) and gate the result: measured
            # 2026-09-19 on the 11 retired papers, this plus the image
            # reinsertion passed 7 with no model call. Provenance is kept.
            bank = await _load_all_parts(effects, key, n, len(src))
            best: dict[int, str] = dict(done)
            for i in translated_idx:
                scored = []
                for t in bank.get(i, []) + [done[i]]:
                    t2, _ = reinsert_missing_imgs(chunks[i], t)
                    scored.append(
                        (
                            not _output_language_problem(_strip_img_tags(t2)),
                            len(_IMG_RE.findall(t2)) == len(_IMG_RE.findall(chunks[i])),
                            _numeric_preservation(chunks[i], t2),
                            t2,
                        )
                    )
                scored.sort(key=lambda s: s[:3], reverse=True)
                best[i] = scored[0][3]
            alt_md, alt_gate = _assemble(best)
            if alt_gate["passed"]:
                done, out_md, gate = best, alt_md, alt_gate
                assembled_from = "best_of_bank"
                logger.info(
                    "translate drain: %s assembled from the best banked parts", key
                )
        too_gappy = len(gaps) / n > _GAP_MAX_FRACTION
        gap_quality = {"chunks": n, "gap_chunks": len(gaps), "gaps": gaps[:20]}
        if source_collapsed["words"] or source_collapsed["chars"]:
            gap_quality["source_collapsed"] = dict(source_collapsed)
        if img_reinserted:
            gap_quality["img_reinserted"] = img_reinserted
        if assembled_from:
            gap_quality["assembled_from"] = assembled_from

        rec["translate_attempts"] = attempts + 1  # an assembly is an attempt
        rec["translate_epoch"] = epoch
        if gate["passed"] and not too_gappy:
            en_rel = (
                md_rel[:-3] + ".en.md" if md_rel.endswith(".md") else md_rel + ".en"
            )
            await effects.write_file(en_rel, out_md)
            rec["extraction_status"] = "extracted"
            rec["md_en_path"] = en_rel
            rec["translated"] = True
            rec["translation_quality"] = {
                **{
                    k: gate[k]
                    for k in ("numeric_preservation", "img_tags", "length_ratio")
                },
                **gap_quality,
            }
            rec["failure_reason"] = ""
            status = "translated"
            if not gaps:
                # Nothing left to repair: reclaim the bank.
                await effects.write_file(_parts_path(key), "")
            await _book_pack_state_after_translation(effects, rec, "translated", "")
        elif gate["passed"]:
            # Translated text is fine but too little of the paper is English.
            # Retired, with the bank and the gap list kept for repair.
            rec["extraction_status"] = "translate_failed"
            rec["failure_reason"] = (
                f"translation: {len(gaps)} of {n} chunks would be gaps "
                f"(limit {_GAP_MAX_FRACTION:.0%}); bank kept for repair"
            )
            rec["translation_quality"] = gap_quality
            status = "failed"
            await _book_pack_state_after_translation(
                effects, rec, "failed", rec["failure_reason"]
            )
        elif rec["translate_attempts"] >= TRANSLATE_MAX_ATTEMPTS:
            rec["extraction_status"] = "translate_failed"
            rec["failure_reason"] = "translation: " + "; ".join(gate["problems"])
            rec["translation_quality"] = {**gap_quality, "problems": gate["problems"]}
            status = "failed"
            await _book_pack_state_after_translation(
                effects, rec, "failed", rec["failure_reason"]
            )
        else:
            # Stays extract_lingual; the bumped attempt count selects the
            # warmer retry next round, and the bumped EPOCH retires this
            # pass's parts so the retry re-translates everything (the old
            # epoch's lines stay in the bank as the record of what happened).
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
        # A prior pack_failed booked by a translation that later completed
        # (the bank-and-repair policy re-arms such papers) re-enters the pack
        # queue the same way a stale original-language pack does.
        if outcome == "translated" and rec.get("pack_status") in (
            "packed",
            "pack_failed",
        ):
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
