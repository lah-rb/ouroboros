"""Degenerate-OCR-loop detection, collapse and page splicing — stdlib only.

Shared by the OCR tool (its own venv), the loop repair driver, the booking
script (root venv) and the root test suite, so every side measures a loop
the same way.

WHAT A LOOP IS. paddle's decode occasionally falls into a cycle and emits one
phrase dozens of times back to back. Its output never passes the server's
generation-time repetition guard (the vision path runs create_chat_completion
inside the vision instance), and the extraction census only rejects runs over
200 words, so 972 of 4,345 extracted markdowns (22 %, 2026-09-19) carried
loops of 40–200 words that a faithful translation then reproduced — and the
translation-side guard killed those at ~50–60 words, on every model.

TWO DETECTORS, ONE FILTER. Word-periodic runs (whitespace tokens, period ≤ 24)
catch alphabetic scripts; a character-periodic pass catches CJK, where a line
has no spaces and the word census sees one 200-character "word". Both keep
only PROSE units: measured over the translation queue at 40 words, 3,249 of
4,664 runs were the repeated cell attribute of ordinary HTML tables and 81 the
figure-removed marker — markup repeats legitimately and is never collapsed.

Marker text is the extraction census's own, so a repaired document reads the
same way as one the census collapsed at extraction time.
"""

from __future__ import annotations

import re

PAGE_SEP = "\n\n---\n\n"  # extract_batch joins pages with this; 40/40 sampled docs split exactly
LOOP_WORDS = 40  # prose run longer than this (words) is a loop
LOOP_CHARS = 6  # character-periodic unit at least this long ...
LOOP_CHAR_REPS = 8  # ... repeated at least this many times back to back
MAX_PERIOD = 24  # longest word phrase treated as a loop unit (extraction's value)

_CHAR_LOOP_RE = re.compile(
    r"(.{%d,80}?)(?:\1){%d,}" % (LOOP_CHARS, LOOP_CHAR_REPS - 1), re.S
)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_MD_NOISE_RE = re.compile(r"[#*_`>|\-]+")
_MARKER_RE = re.compile(r"\*\[degenerate OCR run collapsed:.*?\]\*", re.S)
_FIG_REMOVED = "*[figure removed by extraction filter]*"


def prose_unit(unit: str) -> bool:
    """A repeated unit worth collapsing: carries letters, carries no markup."""
    if "<" in unit or ">" in unit or unit.count("|") >= 2:
        return False
    return re.search(r"[^\W\d_]{2,}", unit) is not None


def split_pages(md: str) -> list[str]:
    return md.split(PAGE_SEP)


def join_pages(pages: list[str]) -> str:
    return PAGE_SEP.join(pages)


def _merge(spans: list[dict]) -> list[dict]:
    spans = sorted(spans, key=lambda s: (s["start"], -s["end"]))
    out: list[dict] = []
    for s in spans:
        if out and s["start"] <= out[-1]["end"]:
            prev = out[-1]
            prev["end"] = max(prev["end"], s["end"])
            prev["words"] = prev.get("words", 0) + s.get("words", 0)
            prev["chars"] = prev.get("chars", 0) + s.get("chars", 0)
        else:
            out.append(dict(s))
    return out


def find_word_loops(
    text: str, limit: int = LOOP_WORDS, max_period: int = MAX_PERIOD
) -> list[dict]:
    """Spans [start, end) of the REPEATS (the first unit is kept) of every
    word-periodic prose run longer than ``limit`` words."""
    toks = list(re.finditer(r"\S+", text))
    if len(toks) <= limit:
        return []
    words = [t.group() for t in toks]
    spans: list[dict] = []
    for period in range(1, max_period + 1):
        run = 0
        for i in range(period, len(words) + 1):
            if i < len(words) and words[i] == words[i - period]:
                run += 1
            else:
                if run + period > limit:
                    start = i - run
                    unit = " ".join(words[start : start + period])
                    if prose_unit(unit):
                        spans.append(
                            {
                                "start": toks[start].start(),
                                "end": toks[i - 1].end(),
                                "words": run,
                                "chars": 0,
                                "unit": unit[:60],
                                "kind": "word",
                            }
                        )
                run = 0
    return _merge(spans)


def find_char_loops(text: str) -> list[dict]:
    """Spans of the repeats of character-periodic prose loops (CJK and other
    space-free scripts; also catches loops the word census under-counts)."""
    out: list[dict] = []
    for m in _CHAR_LOOP_RE.finditer(text):
        unit = m.group(1)
        if not prose_unit(unit):
            continue
        out.append(
            {
                "start": m.start() + len(unit),
                "end": m.end(),
                "words": 0,
                "chars": len(m.group(0)) - len(unit),
                "unit": unit[:60],
                "kind": "char",
            }
        )
    return out


# SCRIPT INJECTION (2026-10-04). paddle also drops a short run of CJK, Thai,
# Tamil, Arabic or Cyrillic into LATIN prose and paraphrases the sentence
# around it ("the data points in Figure 5 demonstrate the重要性 of the
# model"; "Quantification starts by식"). 47 % of accepted Latin-script papers
# carried at least one such paragraph (~2 % of words), and the numeric gate
# cannot see it. The marker is a non-Latin run of 1-8 characters GLUED to a
# Latin letter or a digit ("10%责 impotent"): a legitimately quoted name or
# term is set off by a space or a bracket. Greek is excluded (δ, μ, Ω are
# science). Precision on corpus samples: 40/40 letter-glued hits, and the
# digit-glued ones were hallucinated CJK lists and dates in French prose.
_NONLATIN = "Ѐ-ӿ֐-ۿऀ-෿฀-๿" "぀-ヿ㐀-鿿가-힯"
_NONLATIN_RE = re.compile(f"[{_NONLATIN}]")
_INJECTION_RE = re.compile(
    rf"[A-Za-zÀ-ÿ0-9][^\sA-Za-zÀ-ÿ0-9]?[{_NONLATIN}]{{1,8}}(?=[\sA-Za-zÀ-ÿ.,;:)。、，]|$)"
)
# Above this share of letters the text IS non-Latin (a Chinese abstract, a
# Russian reference block) and glued runs are its own writing, not injection.
INJECTION_MAX_NONLATIN = 0.2


def find_injections(text: str) -> list[str]:
    """Each injected non-Latin run (with the Latin letter it is glued to) in
    predominantly Latin text; [] for text that is itself non-Latin."""
    if not text:
        return []
    letters = len(re.findall(r"[^\W\d_]", text))
    if (
        not letters
        or len(_NONLATIN_RE.findall(text)) / letters > INJECTION_MAX_NONLATIN
    ):
        return []
    return [m.group() for m in _INJECTION_RE.finditer(text)]


def find_loops(text: str) -> list[dict]:
    """Merged spans with costs measured on the TEXT, not summed across
    detections: a loop of period 3 is also a loop of period 6, 9, … and the
    two detectors usually both see it. A span with 8 or more whitespace
    tokens is a word loop (cost in words); anything denser is a
    character loop (cost in characters — CJK has no spaces)."""
    spans = _merge(find_word_loops(text) + find_char_loops(text))
    for s in spans:
        seg = text[s["start"] : s["end"]]
        ntok = len(seg.split())
        if ntok >= 8:
            s["words"], s["chars"], s["kind"] = ntok, 0, "word"
        else:
            s["words"], s["chars"], s["kind"] = 0, len(seg), "char"
        s["unit"] = s["unit"].strip()
    return spans


def loop_cost(text: str) -> dict:
    spans = find_loops(text)
    return {
        "words": sum(s["words"] for s in spans),
        "chars": sum(s["chars"] for s in spans),
        "spans": len(spans),
    }


def collapse_loops(text: str) -> tuple[str, dict]:
    """Replace every loop's repeats with the extraction marker (first unit
    kept). Returns (text, cost removed)."""
    spans = find_loops(text)
    out = text
    for s in reversed(spans):
        if s["words"]:
            what = f"{s['words']} repeated words of {s['unit'][:40]!r}"
        else:
            what = f"{s['chars']} repeated characters of {s['unit'][:40]!r}"
        marker = (
            f"*[degenerate OCR run collapsed: {what} — content at this location "
            "was not read]*"
        )
        out = out[: s["start"]] + " " + marker + out[s["end"] :]
    return out, {
        "words": sum(s["words"] for s in spans),
        "chars": sum(s["chars"] for s in spans),
        "spans": len(spans),
    }


def img_tags(text: str) -> list[str]:
    return _IMG_RE.findall(text)


def remap_imgs(new_text: str, old_tags: list[str]) -> tuple[str, str]:
    """Carry the OLD page's figure references into the re-OCR'd page.

    A re-OCR into a scratch databank numbers its crops from fig_01, so its
    <img> tags point nowhere in the real figures directory. Same count →
    substitute in order (the layout found the same figures). Different count
    → drop the new tags and append the old ones, so no figure reference is
    lost and none dangles."""
    new_tags = _IMG_RE.findall(new_text)
    if len(new_tags) == len(old_tags):
        it = iter(old_tags)
        return _IMG_RE.sub(lambda _m: next(it), new_text), "mapped"
    stripped = _IMG_RE.sub(_FIG_REMOVED, new_text)
    if old_tags:
        stripped = stripped.rstrip() + "\n\n" + "\n\n".join(old_tags) + "\n"
    return stripped, f"kept_old_tags({len(old_tags)} old, {len(new_tags)} new)"


def clean_for_compare(text: str) -> str:
    """Prose of a page with loops collapsed, markup and markdown noise
    removed, lower-cased, whitespace squeezed."""
    text, _ = collapse_loops(text)
    text = _MARKER_RE.sub(" ", text)
    text = text.replace(_FIG_REMOVED, " ")
    text = _TAG_RE.sub(" ", text)
    text = _MD_NOISE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _word_shingles(clean: str, k: int = 4) -> set:
    words = clean.split()
    return {" ".join(words[i : i + k]) for i in range(0, max(0, len(words) - k + 1))}


def _char_shingles(clean: str, k: int = 8) -> set:
    s = clean.replace(" ", "")
    return {s[i : i + k] for i in range(0, max(0, len(s) - k))}


def similarity(old_page: str, new_page: str) -> float:
    """Recall of the OLD page's clean content in the NEW page — the guard
    that a re-OCR is the same page and not garbage. The better of word
    4-gram recall and character 8-gram recall: CJK pages have no spaces,
    table-heavy pages have few prose words, and a wrong page scores near
    zero on both. 1.0 when the old page has nothing clean to compare (the
    loop ate it)."""
    oc, nc = clean_for_compare(old_page), clean_for_compare(new_page)
    if len(oc.split()) < 20 and len(oc) < 80:
        return 1.0  # the loop ate the page: nothing left to judge against
    scores = []
    for fn in (_word_shingles, _char_shingles):
        old = fn(oc)
        if old:
            scores.append(len(old & fn(nc)) / len(old))
    return max(scores) if scores else 1.0
