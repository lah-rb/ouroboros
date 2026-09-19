"""Pure scorers and prompt builders for probe_recall.py (stdlib; testable
without torch). The probe itself loads models; this is what it judges with."""

from __future__ import annotations

import re

TOL_CM1 = 10.0
TOL_NM = 0.2
_NUM = re.compile(r"\d+(?:\.\d+)?")


def numbers(text: str, limit: int = 24) -> list[float]:
    return [float(x) for x in _NUM.findall(text)[:limit]]


def score(task: str, item: dict, gen: str) -> bool:
    """Did the generation state the fact? One rule per task."""
    g = gen.lower()
    if task == "formula":
        return re.sub(r"\s+", "", item["formula"].lower()) in re.sub(r"\s+", "", g)
    if task == "crystal_system":
        return bool(item.get("system")) and item["system"] in g
    if task in ("inverse", "identification", "libs_inverse", "cross_modal"):
        return item["species"].lower() in g
    if task == "bands":
        nums = numbers(gen, 12)
        hits = sum(1 for b in item["bands"] if any(abs(b - x) <= TOL_CM1 for x in nums))
        return hits >= min(2, len(item["bands"]))
    if task == "libs_lines":
        nums = numbers(gen, 24)
        hits = sum(
            1 for nm in item["libs_top3"] if any(abs(nm - x) <= TOL_NM for x in nums)
        )
        return hits >= min(2, len(item["libs_top3"]))
    return False


def strongest_bands(bands: list, rel: list, n: int = 4) -> list[float]:
    """The n strongest Raman bands BY INTENSITY (ties by position), positions
    in cm-1. `bands_cm1` is position-sorted and `templates.top4` takes the first
    four BY POSITION; the XML records of §22 rank by `rel`. Falls back to the
    first n positions when the intensities are missing or misaligned."""
    if not rel or len(rel) != len(bands):
        return [float(b) for b in bands[:n]]
    ranked = sorted(zip(bands, rel), key=lambda br: (-float(br[1]), float(br[0])))
    return [float(b) for b, _ in ranked[:n]]


def strongest_lines(groups: list[dict], n: int = 3) -> list[float]:
    """The n strongest LIBS lines (nm) across ionisation stages."""
    lines = sorted(
        (ln for g in groups for ln in g.get("lines") or []),
        key=lambda ln: -float(ln.get("rel") or 0),
    )
    return [round(float(ln["nm"]), 2) for ln in lines[:n]]


def identification_prompt(peaks: list[tuple[float, float]], laser_nm: str) -> str:
    """The seeker schema (corpus_inverse.identify_prompt) on a perturbed list."""
    exc = f"{laser_nm} nm excitation" if laser_nm else "excitation unknown"
    body = ", ".join(f"{b:.1f} ({r:.2f})" for b, r in peaks)
    return f"Raman peak list ({exc}; {len(peaks)} peaks, position cm-1 with relative intensity, strongest = 1.00):\n{body}\nPhase:"


def libs_inverse_prompt(top3: list[float]) -> str:
    return (
        "LIBS emission lines at "
        + ", ".join(f"{x:.2f}" for x in top3)
        + " nm point to the mineral"
    )


def cross_modal_prompt(top3_cm1: list[float], libs_top3: list[float]) -> str:
    return (
        "Raman bands at "
        + ", ".join(f"{x:g}" for x in top3_cm1)
        + " cm-1; LIBS lines at "
        + ", ".join(f"{x:.2f}" for x in libs_top3)
        + " nm — the mineral is"
    )
