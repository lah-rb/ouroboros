"""The artifact contract: one place the sidecar's shape is defined.

Records land in ``databank/figdata/<paper_key>.json``, a sidecar parallel to
``figtext/``. They deliberately do NOT go into ``databank/dataset/``: a
digitised peak appears nowhere in the paper's text, so the curation grounding
gate (MIN_GROUNDING_RATE = 0.95) would fail the whole pack.

Inlining the numbers into the curator doc so they appear verbatim would not
pass that gate, it would DEFEAT it — the gate exists to stop values the paper
never stated from entering packs, and inlining makes derived values
indistinguishable from stated ones. A digitised peak is not fabricated, but it
is derived, and every record says so in its own provenance block. The trainer
reads this sidecar separately, with its own source weight.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import time

SCHEMA_VERSION = "figure_digitizer/0.1.0"

PROVENANCE = {
    "derivation": "plot_digitisation",
    "reading": ("our own trace of a published figure, not a value the paper states"),
}


def _finite(x: float) -> float | None:
    """JSON has no infinity, and a vector source has no dpi ceiling."""
    return None if x is None or not math.isfinite(x) else round(float(x), 4)


def source_block(reloc, src) -> dict:
    """Where the pixels came from and what they can resolve."""
    return {
        "tier": src.tier,
        "page": src.page,
        "crop_px": list(reloc.crop_px),
        "crop_rect_pt": [round(v, 2) for v in src.rect_pt],
        "relocation_ncc": round(reloc.ncc, 4),
        "relocation_margin": round(reloc.margin, 4),
        "full_page_render": reloc.full_page,
        "xref": src.xref,
        "native_px": list(src.native_px) if src.native_px else None,
        "sub_box_px": (
            [round(v, 2) for v in src.sub_box_px] if src.sub_box_px else None
        ),
        "native_rect_pt": (
            [round(v, 2) for v in src.native_rect_pt] if src.native_rect_pt else None
        ),
        "rect_covered_by_native": round(src.rect_covered_by_native, 3),
        "native_ext": src.native_ext,
        "effective_dpi": _finite(src.effective_dpi),
        "resolution_gain_vs_crop": _finite(src.resolution_gain_vs_crop),
        "n_vector_curves": src.n_vector_curves,
    }


def precision_block(src, grid_unit_per_px=None, stroke_px=None) -> dict:
    """What this figure can and cannot resolve, in its own units.

    ``resolvable`` is the grid spacing times the stroke width, because a peak
    narrower than the pen that drew it cannot be separated no matter how fine
    the sampling. Until axes are calibrated (Phase 1c) the unit-valued fields
    are null and only ``limited_by`` is known.
    """
    resolvable = None
    if grid_unit_per_px is not None and stroke_px:
        resolvable = round(grid_unit_per_px * stroke_px, 6)
    return {
        "grid_unit_per_px": (
            round(grid_unit_per_px, 6) if grid_unit_per_px is not None else None
        ),
        "resolvable_unit": resolvable,
        "limited_by": src.limited_by,
        "effective_dpi": _finite(src.effective_dpi),
        "stroke_width_px": round(stroke_px, 2) if stroke_px else None,
        "jpeg_round_trips": src.jpeg_round_trips,
    }


def figure_record(
    fig: str,
    status: str,
    *,
    reject_reason: str | None = None,
    technique: str | None = None,
    caption: str = "",
    reloc=None,
    src=None,
    flags: list[str] | None = None,
) -> dict:
    """One figure's entry. ``status`` is digitized / rejected / not_a_plot."""
    rec: dict = {
        "fig": fig,
        "status": status,
        "reject_reason": reject_reason,
        "technique": technique,
        "caption": caption,
        "flags": sorted(flags or []),
        "provenance": dict(PROVENANCE),
    }
    if reloc is not None and src is not None:
        rec["source"] = source_block(reloc, src)
        rec["precision"] = precision_block(src)
    return rec


def paper_record(paper_key: str, figs: list[dict]) -> dict:
    return {
        "paper_key": paper_key,
        "tool_version": SCHEMA_VERSION,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "figs": figs,
    }


def figdata_path(working_dir: str, paper_key: str) -> str:
    return os.path.join(working_dir, "databank", "figdata", f"{paper_key}.json")


def write_sidecar(working_dir: str, record: dict) -> str:
    """Atomically replace a paper's sidecar."""
    path = figdata_path(working_dir, record["paper_key"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


@dataclasses.dataclass
class RunReport:
    """Counts by status, tier and reason — the yield story for one run."""

    started_utc: str = dataclasses.field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    papers: int = 0
    figures: int = 0
    status: dict = dataclasses.field(default_factory=dict)
    tier: dict = dataclasses.field(default_factory=dict)
    reject_reason: dict = dataclasses.field(default_factory=dict)
    relocation_ncc: list = dataclasses.field(default_factory=list)
    gain: list = dataclasses.field(default_factory=list)

    def bump(self, bucket: str, key) -> None:
        d = getattr(self, bucket)
        d[key] = d.get(key, 0) + 1

    def as_dict(self) -> dict:
        def pct(xs, q):
            if not xs:
                return None
            s = sorted(xs)
            return round(s[min(len(s) - 1, int(q * len(s)))], 4)

        located = sum(1 for v in self.relocation_ncc if v >= 0.98)
        return {
            "started_utc": self.started_utc,
            "papers": self.papers,
            "figures": self.figures,
            "status": self.status,
            "tier": self.tier,
            "reject_reason": self.reject_reason,
            "relocation": {
                "n": len(self.relocation_ncc),
                "located_ge_0.98": located,
                "rate": (
                    round(located / len(self.relocation_ncc), 4)
                    if self.relocation_ncc
                    else None
                ),
                "p05_ncc": pct(self.relocation_ncc, 0.05),
                "median_ncc": pct(self.relocation_ncc, 0.5),
            },
            "resolution_gain": {
                "n": len(self.gain),
                "median": pct(self.gain, 0.5),
                "p90": pct(self.gain, 0.9),
                "ge_2x": sum(1 for g in self.gain if g >= 2.0),
            },
        }
