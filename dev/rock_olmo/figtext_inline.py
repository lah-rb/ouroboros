"""Pack a paper's figure descriptions back into its markdown (root venv).

WHY. The figtext sidecars hold ~15.8M tokens of figure descriptions on the
accepted papers and none of it reached the v3 training text. Operator
direction (2026-09-07): stage 1 trains on the paper's MOST COMPLETE
representation, so each figure's reading is inlined at the figure's own
anchor, exactly as the curator's document builder does it.

REUSE, NOT REIMPLEMENTATION. The anchoring is `build_curator_doc`
(agent/actions/curation_actions.py): substring match on
`figures/<key>/<fig>` inside the extractor's `<img src="../figures/...">`
tag, the reading appended to that paragraph as a `> [FIGURE fig_NN.png —
VLM reading]: …` block, unanchored readings collected under
`## Unanchored figures (VLM readings)`. This module only decides WHICH
readings go in and in what order, because `build_curator_doc` has no filter
of its own and would inline prompt echoes and "I cannot see a figure"
verbatim.

THREE FILTERS, measured over all 100,893 figs (2026-09-07):
  * PROMPT ECHO (1.6 %): the model re-emitted the prompt before answering.
    The prompt ends "Be dense and factual; no preamble." — keep the text after
    its LAST occurrence; if "CONTEXT ONLY" still appears, the answer never
    started, drop the fig.
  * JUNK (2.0 %): the figtext says the crop is not a figure — logos, first
    pages, tables-as-images. Two regexes exist in the repo; the vision
    caption pass's JUNK_RE fires on `\\bmap\\b`, which is 8,428 legitimate
    planetary/geological maps, so that alternative is removed here and the
    digitizer's _JUNK is OR'd in. Applied to the IDENTITY ZONE (first 240
    chars), where the model states what the image is — matching the whole
    body rejects real spectra that mention a micrograph in passing.
  * CAP: anchored readings first, orphans last, stop when figtext tokens
    exceed `cap_ratio` x the paper's own text. Guards short papers with many
    crops from becoming mostly VLM prose. 1.0x by default (operator: most
    complete representation); the record of the run says whether to lower it.

`caption` is never passed through: it is context the VLM was shown, not a
reading, and the curator never inlines it either.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent.actions.curation_actions import build_curator_doc  # noqa: E402

#: The vision caption pass's junk vocabulary WITHOUT `\bmap\b`, plus the
#: figure digitizer's self-report classes. Kept as literal text here (not
#: imported) so the two tool modules' own edits cannot silently change what
#: this corpus filter does; tests pin the behaviour.
JUNK_RE = re.compile(
    r"i cannot see a figure|first page of the paper|\blogo\b|graphical abstract"
    r"|photograph of the (?:authors|laboratory)|rows and columns"
    r"|\bfirst page\b|\bcover page\b|\bjournal logo\b|\bpublisher\b|\bQR code\b"
    r"|\btable\b.{0,20}\bimage\b|\bentirely text\b|\bpage of text\b",
    re.IGNORECASE,
)
PROMPT_TAIL = "no preamble."
PROMPT_HEAD = "CONTEXT ONLY"
IDENTITY_CHARS = 240
ANCHOR_RE = re.compile(r'<img\s[^>]*src="[^"]*figures/([^/"]+)/(fig_\d+\.png)"', re.I)


@dataclass
class InlineStats:
    figs_in: int = 0
    echo_stripped: int = 0
    echo_dropped: int = 0
    junk: int = 0
    empty: int = 0
    capped: int = 0
    inlined: int = 0
    anchored: int = 0
    orphans: int = 0
    figtext_chars: int = 0
    notes: list[str] = field(default_factory=list)


def strip_prompt_echo(text: str) -> tuple[str, bool, bool]:
    """(clean_text, stripped, dropped). Keeps the text after the LAST prompt
    tail; drops when the prompt head survives (the answer never started)."""
    if PROMPT_TAIL not in text:
        return text, False, False
    tail = text.rsplit(PROMPT_TAIL, 1)[-1].strip()
    if not tail or PROMPT_HEAD in tail:
        return "", True, True
    return tail, True, False


def is_junk(text: str) -> bool:
    return bool(JUNK_RE.search(text[:IDENTITY_CHARS]))


def anchored_figs(md: str, key: str) -> set[str]:
    """fig names whose `<img>` anchor appears in the markdown of `key`."""
    return {fig for k, fig in ANCHOR_RE.findall(md) if k == key}


def _approx_tokens(text: str) -> int:
    # 3.5 chars/token is this corpus's measured OLMo-2 rate; the cap is a
    # ratio so the constant cancels except at the margin.
    return max(1, len(text) // 3)


def select_figs(
    md: str,
    key: str,
    figs: list[dict],
    *,
    cap_ratio: float = 1.0,
    stats: InlineStats | None = None,
) -> list[dict]:
    """Filter and order a sidecar's figs for inlining."""
    st = stats if stats is not None else InlineStats()
    anchors = anchored_figs(md, key)
    budget = int(cap_ratio * _approx_tokens(md))
    cleaned: list[tuple[bool, dict]] = []
    for entry in figs:
        st.figs_in += 1
        fig = str(entry.get("fig") or "")
        text = str(entry.get("figtext") or "").strip()
        if not text:
            st.empty += 1
            continue
        text, stripped, dropped = strip_prompt_echo(text)
        if stripped:
            st.echo_stripped += 1
        if dropped:
            st.echo_dropped += 1
            continue
        if is_junk(text):
            st.junk += 1
            continue
        cleaned.append((fig in anchors, {"fig": fig, "figtext": text}))
    # anchored first (document order by fig number), then orphans
    cleaned.sort(key=lambda t: (not t[0], t[1]["fig"]))
    out: list[dict] = []
    used = 0
    for is_anchored, entry in cleaned:
        cost = _approx_tokens(entry["figtext"])
        if used + cost > budget and out:
            st.capped += 1
            continue
        used += cost
        out.append(entry)
        st.inlined += 1
        st.figtext_chars += len(entry["figtext"])
        if is_anchored:
            st.anchored += 1
        else:
            st.orphans += 1
    return out


def inline_figtext(
    md: str, key: str, sidecar: dict | None, *, cap_ratio: float = 1.0
) -> tuple[str, InlineStats]:
    """The paper's markdown with its filtered figure readings inlined."""
    st = InlineStats()
    figs = list((sidecar or {}).get("figs") or [])
    if not figs or not md.strip():
        return md, st
    chosen = select_figs(md, key, figs, cap_ratio=cap_ratio, stats=st)
    if not chosen:
        return md, st
    return build_curator_doc(md, {"paper_key": key, "figs": chosen}), st


def read_sidecar(working_dir: str, key: str) -> dict:
    """The stored figtext record for a paper, or {} (plain json, no effects)."""
    import json

    path = os.path.join(working_dir, "databank", "figtext", f"{key}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — a paper without figtext is not an error
        return {}
