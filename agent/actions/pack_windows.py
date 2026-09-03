"""Section-bounded windows for staged data packing, and the merge of their packs.

WHY. Pack success collapses with document length -- measured 2026-09-02 over
1,941 accepted papers: 96% packed at 10-25k tokens, 87% at 25-50k, 58% at
50-100k, 36% over 100k, and the failures over 50k are dominated by the
grounding gate rejecting numbers the paper never states. Every model shows
it; the parked pool merely put qwen3-next exclusively in the failing regime.
A pack turn over a window the model can actually attend to is the lever.

THE CUTTING RULE (operator, 2026-09-02): a window ends on a SECTION boundary,
never at a length. Then a fact split across two windows requires the author
to have split it across two sections -- a failure of the paper's organisation,
not of our cutter. Sections are markdown headings; figure readings are inlined
as blockquotes below the paragraph that cites them, so they ride inside the
section that references them.

SIZING. Windows are filled with whole sections up to a target, sized from the
same measurement: the 10-25k band is where packing works. A single section
larger than the hard cap becomes a window of its own -- oversize, flagged, and
still never cut.

MERGE. Packs are flat-ish JSON objects with list-valued keys carrying the
corpus's most valuable content (peak tables). Lists concatenate with exact-
duplicate removal; dicts merge recursively; scalars keep the FIRST value and
record every disagreement, so a conflict is visible rather than silently
resolved. Grounding of the merged pack against the whole document holds by
construction when each window's pack was grounded against its own window --
a strictly stricter check than the whole-document one, and the one that
closes the fabrication mode directly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from agent.actions.curation_actions import _estimate_doc_tokens

# Sized from the corpus measurement: 10-25k tokens is the 96% band.
DEFAULT_TARGET_TOKENS = 18_000
DEFAULT_MAX_TOKENS = 25_000

_HEADING = re.compile(r"^#{1,6} +\S", re.M)


@dataclass
class Window:
    index: int
    text: str
    tokens: int
    section_count: int
    first_heading: str
    oversize: bool = False  # a single section over the hard cap: never cut


@dataclass
class MergeReport:
    conflicts: list[dict] = field(default_factory=list)
    list_keys: list[str] = field(default_factory=list)
    scalar_keys: list[str] = field(default_factory=list)


def split_sections(doc: str) -> list[str]:
    """The document as a list of sections, each starting at a markdown heading.

    Text before the first heading (title block, catalogue lines) is its own
    leading section. Sections are returned verbatim and in order, so
    ``"".join(split_sections(d)) == d``.
    """
    if not doc:
        return []
    starts = [m.start() for m in _HEADING.finditer(doc)]
    if not starts:
        return [doc]
    bounds = ([0] if starts[0] > 0 else []) + starts + [len(doc)]
    out = [doc[a:b] for a, b in zip(bounds, bounds[1:])]
    return [s for s in out if s]


def _heading_of(section: str) -> str:
    m = _HEADING.search(section)
    if not m:
        return section.strip().splitlines()[0][:80] if section.strip() else ""
    line = section[m.start() :].splitlines()[0]
    return line.lstrip("#").strip()[:80]


def window_sections(
    doc: str,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[Window]:
    """Group whole sections into windows of about ``target_tokens``.

    A section is appended to the open window while the window stays within
    ``target_tokens``, or -- when the window is empty -- unconditionally. A
    section that alone exceeds ``max_tokens`` becomes an oversize window of
    its own. No section is ever divided; every section lands in exactly one
    window; order is preserved.
    """
    if target_tokens <= 0 or max_tokens < target_tokens:
        raise ValueError("need 0 < target_tokens <= max_tokens")
    sections = split_sections(doc)
    windows: list[Window] = []
    cur: list[str] = []
    cur_tokens = 0

    def flush() -> None:
        nonlocal cur, cur_tokens
        if not cur:
            return
        text = "".join(cur)
        windows.append(
            Window(
                index=len(windows),
                text=text,
                tokens=cur_tokens,
                section_count=len(cur),
                first_heading=_heading_of(cur[0]),
                oversize=cur_tokens > max_tokens,
            )
        )
        cur, cur_tokens = [], 0

    for sec in sections:
        t = _estimate_doc_tokens(sec)
        if t > max_tokens:
            flush()
            cur, cur_tokens = [sec], t
            flush()
            continue
        if cur and cur_tokens + t > target_tokens:
            flush()
        cur.append(sec)
        cur_tokens += t
    flush()
    return windows


def _dedupe(items: list) -> list:
    seen: set[str] = set()
    out = []
    for it in items:
        key = json.dumps(it, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def merge_packs(
    packs: list[dict], report: MergeReport | None = None, _path: str = ""
) -> dict:
    """Merge per-window packs into one. Lists concatenate (exact duplicates
    dropped), dicts merge recursively, scalars keep the first value and log
    every disagreement to ``report.conflicts``."""
    report = report if report is not None else MergeReport()
    out: dict = {}
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        for k, v in pack.items():
            p = f"{_path}.{k}" if _path else k
            if k not in out:
                out[k] = list(v) if isinstance(v, list) else v
                (
                    report.list_keys if isinstance(v, list) else report.scalar_keys
                ).append(p)
                continue
            cur = out[k]
            if isinstance(cur, list) and isinstance(v, list):
                out[k] = _dedupe(cur + v)
            elif isinstance(cur, list) and v is not None:
                out[k] = _dedupe(cur + [v])
            elif isinstance(v, list) and cur is not None:
                out[k] = _dedupe([cur] + v)
            elif isinstance(cur, dict) and isinstance(v, dict):
                out[k] = merge_packs([cur, v], report, p)
            elif cur is None:
                out[k] = v
            elif v is None or cur == v:
                continue
            else:
                report.conflicts.append({"path": p, "kept": cur, "dropped": v})
    return out


# ── Shape repair ─────────────────────────────────────────────────────────
#
# Measured on the 2026-09-02 staged-pack run: of the windows the gates
# rejected, four failed ONLY on registry shape -- a peak list returned as bare
# numbers where the registry holds list[object] -- and together carried 1,236
# grounded values at grounding >= 0.996. The eight fabrication failures sat at
# 0.35-0.91. The two populations do not overlap, so re-shaping a grounded list
# is safe: the numbers were in the paper; only the wrapper was missing.


def _field_affinity(field: str, key: str) -> int:
    """How strongly a field name belongs to a registry key. Higher is better."""
    f = re.sub(r"[^a-z0-9]+", " ", field.lower()).split()
    k = re.sub(r"[^a-z0-9]+", " ", key.lower()).split()
    shared = len(set(f) & set(k))
    # "peak"/"value"/"wavelength" name the POSITION a bare list carries;
    # a condition field ("sintering_temperature_c") names something else
    # entirely and must never win by being listed first.
    return shared * 10 + (3 if f and f[0] in ("peak", "value", "wavelength") else 0)


def _exemplar_field(entry: dict, key: str = "") -> str | None:
    """The exemplar field a bare list's values belong in.

    CHOSEN BY NAME AFFINITY WITH THE KEY, not by position. Measured
    2026-09-03: the exemplar for `xrd_peaks_2theta_deg` is
    `{"sintering_temperature_c": 850, "phase": ..., "peak_2theta_deg": 34.32}`,
    and taking the first numeric field wrapped 2-theta values as SINTERING
    TEMPERATURES -- a silent corruption, and worse than not repairing at all.
    """
    ex = entry.get("exemplar")
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except Exception:
            # exemplars are TRUNCATED strings; recover the first object
            m = re.search(r"\{[^{}]*\}", ex)
            if not m:
                return None
            try:
                ex = json.loads(m.group(0))
            except Exception:
                return None
    if isinstance(ex, list):
        ex = next((e for e in ex if isinstance(e, dict)), None)
    if not isinstance(ex, dict):
        return None
    numeric = [
        k
        for k, v in ex.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    if not numeric:
        return next(iter(ex), None)
    best = max(numeric, key=lambda f: _field_affinity(f, key))
    if key and _field_affinity(best, key) == 0 and len(numeric) > 1:
        # Several numeric fields and none of them belongs to this key: any
        # choice would be a guess, and a guess here mislabels real data.
        return None
    return best


def repair_shapes(data: dict, registry: dict) -> tuple[dict, list[dict]]:
    """Coerce grounded values into the registry's declared shape where that is
    a pure re-wrapping; never invent or drop a value.

    Handled: registry ``list[object]`` met by a list containing non-object
    items (wrap each scalar as ``{<exemplar field>: value}``), or by a bare
    scalar/object (wrap into a one-element list). Anything else is left for
    the registry check to judge. Returns (repaired data, repairs made)."""
    out = dict(data)
    repairs: list[dict] = []
    for key, value in data.items():
        entry = registry.get(key)
        if not entry or str(entry.get("type") or "") != "list[object]":
            continue
        field = _exemplar_field(entry, key)
        if isinstance(value, list):
            if all(isinstance(v, dict) for v in value):
                continue
            if not field:
                continue
            fixed = [v if isinstance(v, dict) else {field: v} for v in value]
            out[key] = fixed
            repairs.append(
                {"key": key, "from": "list", "to": "list[object]", "n": len(value)}
            )
        elif isinstance(value, dict):
            out[key] = [value]
            repairs.append({"key": key, "from": "object", "to": "list[object]", "n": 1})
        elif value is not None and field:
            out[key] = [{field: value}]
            repairs.append({"key": key, "from": "scalar", "to": "list[object]", "n": 1})
    return out, repairs
