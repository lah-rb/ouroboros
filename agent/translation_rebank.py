"""Re-bank a translation onto a changed source: re-translate only what changed.

WHY (2026-10-04). A source markdown changes after it was translated when an OCR
repair re-reads pages: the 09-19 loop repair and the 10-04 injection repair
(paddle's foreign-script injection, 1,434 injected pages under 199 translated
Latin-script papers). Until now every such change cost the WHOLE translation:
the drain's parts bank is bound to (chunk count, source length), so a one-page
edit invalidates every banked chunk, and a finished translation was re-armed
from nothing. Fourteen pending papers sat on ~400 banked chunks made stale by
the loop repair alone.

WHAT. The material already translated is cut into UNITS bound to spans of the
OLD source, and carried onto the NEW source:

  * a pending paper's bank: the old chunking (chunk_markdown of the source the
    bank was bound to) and its banked parts;
  * a finished translation: the .en.md aligned to the old source's PAGES. The
    translator keeps most page separators but drops 5-10 % of them, so the
    English segments between separators are mapped onto runs of consecutive
    source pages by dynamic programming over what survives translation —
    numbers (the gate already demands 95 % of them), image tags (kept exactly)
    and length.

The plan is in CHARACTERS (chunk lengths, one blank line between chunks):
the drain's own cut is at every blank line while pages are cut at the page
separator, and around odd whitespace the two block lists disagree.

A unit is carried over only when every page it covers is UNCHANGED and it is
TRUSTED: it passes the drain's own span check against its source (image tags,
numeric preservation, English judged on prose) and its length is plausible.
Prose with no anchor of its own (no numbers, no images) is fixed by the
anchors around it: a stretch between two anchors maps as a whole, and page by
page only when no separator was dropped inside it (group_runs). Everything
else becomes part of a DIRTY region, re-cut with chunk_markdown and left for
the drain to translate. The result is a chunk PLAN (block counts over the new
source) plus the banked parts, written to the paper's parts file; the drain
chunks by the plan, translates the missing chunks, and assembles and gates the
whole paper as always.

Pure functions; the CLI is tools/translation_rebank.py.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter

from agent.bibliography import bibliography_spans
from agent.actions.translation_actions import (
    _IMG_TAG_RE,
    _NUM_RE,
    _output_language_problem,
    _span_problem,
    _strip_img_tags,
    chunk_markdown,
    strip_table_decoration,
)

PAGE_SEP = "\n\n---\n\n"
BLOCK_SEP = "\n\n"
SEP_BLOCK = "---"
# A run maps at most this many source pages onto at most this many English
# segments: a dropped separator merges two pages into one segment (measured
# 5-10 % of separators); three in a row is rare, more is a misalignment.
MAX_PAGES_PER_RUN = 3
MAX_SEGS_PER_RUN = 2
# A unit is ANCHORED by its numbers when it has at least this many and this
# share of the source's survive (multiset), or by its image tags.
ANCHOR_MIN_NUMBERS = 3
ANCHOR_MIN_NUMERIC = 0.8
# Plausible English/source length per unit, relative to the paper's own ratio.
UNIT_RATIO_BAND = (0.5, 2.0)


def chunks_from_plan(src: str, plan: list[int]) -> list[str]:
    """The source cut into chunks of ``plan[i]`` CHARACTERS each, consecutive
    chunks separated by one blank line ("\\n\\n"), exactly as the drain
    joins them: "\\n\\n".join of the result is the source (plan_fits)."""
    out, o = [], 0
    for n in plan:
        out.append(src[o : o + n])
        o += n + len(BLOCK_SEP)
    return out


def plan_fits(src: str, plan) -> bool:
    """Does ``plan`` (chunk lengths in characters) tile ``src`` exactly?"""
    try:
        lens = [int(n) for n in plan]
    except (TypeError, ValueError):
        return False
    if not lens or any(n <= 0 for n in lens):
        return False
    o = 0
    for i, n in enumerate(lens):
        o += n
        if i < len(lens) - 1:
            if src[o : o + len(BLOCK_SEP)] != BLOCK_SEP:
                return False
            o += len(BLOCK_SEP)
    return o == len(src)


def _numbers(text: str) -> Counter:
    return Counter(t.lstrip("-") for t in _NUM_RE.findall(strip_table_decoration(text)))


def _imgs(text: str) -> list[str]:
    return _IMG_TAG_RE.findall(text)


def _size(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def align_pages(src_pages: list[str], en_segs: list[str]) -> list[dict]:
    """Monotone alignment of English segments onto runs of source pages.

    Returns runs {"pages": (a, b), "segs": (j0, j1), "num_total", "num_hit",
    "imgs_equal", "ratio"} covering both sides completely, or [] when no
    alignment exists within the run limits (a translation that lost or
    invented pages). Cost per run: numeric dissimilarity (weighted by how many
    numbers there are to judge), image-tag mismatch, length deviation from
    the paper's own English/source ratio, and a small price for every merge
    so 1:1 wins a tie.
    """
    n, m = len(src_pages), len(en_segs)
    if not n or not m:
        return []
    s_num = [_numbers(p) for p in src_pages]
    e_num = [_numbers(s) for s in en_segs]
    s_img = [_imgs(p) for p in src_pages]
    e_img = [_imgs(s) for s in en_segs]
    s_len = [_size(p) for p in src_pages]
    e_len = [_size(s) for s in en_segs]
    ratio = (sum(e_len) + 1) / (sum(s_len) + 1)

    def run_stats(a, b, j0, j1):
        sn = sum((s_num[i] for i in range(a, b)), Counter())
        en = sum((e_num[j] for j in range(j0, j1)), Counter())
        total = sum(sn.values())
        hit = sum((sn & en).values())
        si = [t for i in range(a, b) for t in s_img[i]]
        ei = [t for j in range(j0, j1) for t in e_img[j]]
        r = (sum(e_len[j0:j1]) + 50) / ((sum(s_len[a:b]) + 50) * ratio)
        return total, hit, si == ei, bool(si or ei), r

    def cost(a, b, j0, j1):
        total, hit, img_eq, has_img, r = run_stats(a, b, j0, j1)
        c = abs(math.log(r))
        if total:
            c += 2.0 * (1 - hit / total) * min(1.0, total / 5)
        if has_img and not img_eq:
            c += 1.5
        c += 0.3 * ((b - a - 1) + (j1 - j0 - 1))
        return c

    # Band around the diagonal: separators are dropped, not invented, so the
    # English index never runs far from the page index scaled by m/n.
    band = max(8, int(0.12 * max(n, m)))
    inf = float("inf")
    best = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list] = [[None] * (m + 1) for _ in range(n + 1)]
    best[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if best[i][j] == inf:
                continue
            for di in range(1, MAX_PAGES_PER_RUN + 1):
                for dj in range(1, MAX_SEGS_PER_RUN + 1):
                    a, b, j0, j1 = i, i + di, j, j + dj
                    if b > n or j1 > m:
                        continue
                    if abs(j1 - b * m / n) > band:
                        continue
                    c = best[i][j] + cost(a, b, j0, j1)
                    if c < best[b][j1]:
                        best[b][j1] = c
                        back[b][j1] = (i, j)
    if best[n][m] == inf:
        return []
    runs = []
    i, j = n, m
    while (i, j) != (0, 0):
        pi, pj = back[i][j]
        total, hit, img_eq, has_img, r = run_stats(pi, i, pj, j)
        runs.append(
            {
                "pages": (pi, i),
                "segs": (pj, j),
                "num_total": total,
                "num_hit": hit,
                "imgs_equal": img_eq,
                "has_imgs": has_img,
                "ratio": round(r, 3),
            }
        )
        i, j = pi, pj
    runs.reverse()
    return runs


def _anchored(run: dict) -> bool:
    if run["has_imgs"] and run["imgs_equal"]:
        return True
    return (
        run["num_total"] >= ANCHOR_MIN_NUMBERS
        and run["num_hit"] / run["num_total"] >= ANCHOR_MIN_NUMERIC
    )


def group_runs(runs: list[dict]) -> list[tuple[int, int]]:
    """Aligned runs grouped into UNITS the alignment can vouch for, as index
    ranges [i, k) into ``runs``.

    An anchored run is its own unit. A stretch of unanchored runs (plain prose:
    no numbers, no images) between two anchors (or a document edge) is fixed
    AS A WHOLE — its pages and segments sit between the same two anchors — but
    not page by page when a separator was dropped inside it, because nothing
    says which two pages merged. So a 1:1 stretch stays one unit per run, and a
    stretch with a merge becomes one unit."""
    groups: list[tuple[int, int]] = []
    i = 0
    while i < len(runs):
        if _anchored(runs[i]):
            groups.append((i, i + 1))
            i += 1
            continue
        k = i
        while k < len(runs) and not _anchored(runs[k]):
            k += 1
        one_to_one = all(
            r["pages"][1] - r["pages"][0] == 1 and r["segs"][1] - r["segs"][0] == 1
            for r in runs[i:k]
        )
        if one_to_one:
            groups.extend((t, t + 1) for t in range(i, k))
        else:
            groups.append((i, k))
        i = k
    return groups


_TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)


def unit_problem(src: str, en: str, *, in_references: bool = False) -> str:
    """The drain's span check (image tags, numeric preservation, English) with
    the language judged on PROSE: a table's cells and the cell style paddle
    wrote before 2026-10-02 ("margin auto word-wrap break-word") read as a
    page of non-English words and failed English table pages.

    ``in_references``: the unit lies inside a reference-list SECTION of the
    whole document. The numeric check is skipped there, as the paper gate
    skips it (strip_reference_section) — but a unit is a page, and only the
    page carrying the heading would be recognised on its own, so a thesis's
    later bibliography pages failed on reformatted citation years."""
    problem = _span_problem(src, en)
    if in_references and problem == "numeric":
        problem = _output_language_problem(_strip_img_tags(en))
    if not problem.startswith("output not English"):
        return problem
    prose = _TABLE_RE.sub(" ", _strip_img_tags(strip_table_decoration(en)))
    return _output_language_problem(prose)


class _Pages:
    """Character geometry of one markdown's pages (split by PAGE_SEP, the
    split the OCR tool joined them with): page p's content is
    [start[p], start[p] + length[p]); between pages sits the separator."""

    def __init__(self, md: str):
        self.md = md
        self.pages = md.split(PAGE_SEP)
        self.start: list[int] = []
        o = 0
        for p in self.pages:
            self.start.append(o)
            o += len(p) + len(PAGE_SEP)
        self.length = [len(p) for p in self.pages]
        self.n_pages = len(self.pages)

    def locate(self, x: int) -> tuple[int, bool]:
        """(page, inside) for character position x: inside=True when x lies
        in page p's content (ends inclusive), False when it lies in the
        separator just before page p."""
        p = bisect.bisect_right(self.start, x) - 1
        if x <= self.start[p] + self.length[p]:
            return p, True
        return p + 1, False

    def run_bounds(self, a: int, b: int) -> tuple[int, int]:
        """Character span of pages [a, b) with the "---" before page a (so
        consecutive runs tile the document with one blank line between)."""
        s = 0 if a == 0 else self.start[a] - len(SEP_BLOCK + BLOCK_SEP)
        e = self.start[b - 1] + self.length[b - 1]
        return s, e


def page_texts(md: str) -> list[str]:
    return md.split(PAGE_SEP)


def plan_rebank(
    old_src: str,
    new_src: str,
    units: list[tuple[int, int, str | None]],
    *,
    target_chars: int = 12_000,
) -> dict:
    """Carry translated units from ``old_src`` onto ``new_src``.

    ``units``: (old char start, old char end, english or None) tiling the old
    source in order, one blank line between consecutive units; None is a
    unit with nothing to carry (or not trusted). Pages are compared one by
    one, so both sources must have the same page count.

    Returns {"plan": [chunk lengths], "parts": {chunk idx: english},
    "carried", "dirty_chunks", "changed_pages"} — or {"error": ...}.
    """
    old, new = _Pages(old_src), _Pages(new_src)
    if old.n_pages != new.n_pages:
        return {"error": f"page count changed {old.n_pages} -> {new.n_pages}"}
    changed = {p for p in range(old.n_pages) if old.pages[p] != new.pages[p]}

    def map_pos(x: int):
        """Old position -> new position, or None inside a changed page, where
        no character correspondence exists (its two ends still map)."""
        if x >= len(old_src):
            return len(new_src)
        p, inside = old.locate(x)
        if not inside:  # in the separator before page p: separators never change
            return new.start[p] - (old.start[p] - x)
        if p not in changed:
            return new.start[p] + (x - old.start[p])
        if x == old.start[p]:
            return new.start[p]
        if x == old.start[p] + old.length[p]:
            return new.start[p] + new.length[p]
        return None

    def pages_of(s: int, e: int) -> set:
        return {
            p
            for p in range(old.n_pages)
            if old.start[p] < e and s < old.start[p] + old.length[p] + 1
        }

    def clean(u) -> bool:
        return u[2] is not None and not (pages_of(u[0], u[1]) & changed)

    plan: list[int] = []
    parts: dict[int, str] = {}
    carried = dirty_chunks = 0
    i = 0
    while i < len(units):
        s, e, en = units[i]
        ns, ne = map_pos(s), map_pos(e)
        if clean(units[i]) and ns is not None and ne is not None:
            if old_src[s:e] != new_src[ns:ne]:
                return {"error": f"carried unit at {s} differs from its old source"}
            plan.append(ne - ns)
            parts[len(plan) - 1] = en
            carried += 1
            i += 1
            continue
        # DIRTY run: grow until the next unit is clean and starts mappably.
        k = i + 1
        while k < len(units) and not (
            clean(units[k]) and map_pos(units[k][0]) is not None
        ):
            k += 1
        ds, de = map_pos(units[i][0]), map_pos(units[k - 1][1])
        if ds is None or de is None:
            return {"error": "dirty region does not end on a mappable boundary"}
        for chunk in chunk_markdown(new_src[ds:de], target_chars):
            plan.append(len(chunk))
            dirty_chunks += 1
        i = k
    if not plan_fits(new_src, plan):
        return {"error": "plan does not tile the new source"}
    return {
        "plan": plan,
        "parts": parts,
        "carried": carried,
        "dirty_chunks": dirty_chunks,
        "changed_pages": sorted(changed),
    }


def units_from_translation(old_src: str, en_md: str) -> tuple[list, dict]:
    """Units of a FINISHED translation: English segments aligned onto runs of
    old-source pages, grouped into units the alignment vouches for
    (group_runs), each carried only if its length is plausible and it passes
    the span check against its own source (unit_problem)."""
    geo = _Pages(old_src)
    pages, segs = geo.pages, en_md.split(PAGE_SEP)
    runs = align_pages(pages, segs)
    if not runs:
        return [], {"error": "no alignment"}
    ratio = (sum(_size(x) for x in segs) + 1) / (sum(_size(x) for x in pages) + 1)
    bib = bibliography_spans(old_src)
    units = []
    stats = Counter(runs=len(runs), anchored=sum(_anchored(r) for r in runs))
    for gi, gk in group_runs(runs):
        a, b = runs[gi]["pages"][0], runs[gk - 1]["pages"][1]
        j0, j1 = runs[gi]["segs"][0], runs[gk - 1]["segs"][1]
        start, end = geo.run_bounds(a, b)
        src_text = old_src[start:end]
        en = PAGE_SEP.join(segs[j0:j1])
        if a > 0:
            en = SEP_BLOCK + BLOCK_SEP + en  # the unit owns the separator before it
        r = (_size(en) + 50) / ((_size(src_text) + 50) * ratio)
        why = ""
        if not (UNIT_RATIO_BAND[0] <= r <= UNIT_RATIO_BAND[1]):
            why = "length ratio"
        elif problem := unit_problem(
            src_text,
            en,
            in_references=any(bs <= start and end <= be for bs, be in bib),
        ):
            why = problem
        stats["units"] += 1
        stats["merged_stretches"] += gk - gi > 1
        if why:
            stats[f"untrusted: {why.split(':')[0]}"] += 1
        units.append((start, end, None if why else en))
    stats["trusted"] = sum(1 for u in units if u[2] is not None)
    return units, dict(stats)


def units_from_bank(old_src: str, parts: dict[int, str]) -> list:
    """Units of a pending paper's bank: the old chunking, each chunk carried
    with its banked part when it has one."""
    units, o = [], 0
    for i, chunk in enumerate(chunk_markdown(old_src)):
        units.append((o, o + len(chunk), parts.get(i)))
        o += len(chunk) + len(BLOCK_SEP)
    return units
