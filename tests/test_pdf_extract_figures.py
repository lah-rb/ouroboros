"""Figure dedup — near-identical spectra panels must SURVIVE.

`_collect_figures` deduped with a 4-bit radius on a 64-bit dHash of an 8x8
grayscale downsample. For a spectroscopy corpus that is far too loose: two
DIFFERENT spectra sharing an overall envelope (the same pattern at successive
delays, the same diffractogram with different indexing) land inside 4 bits, and
one was silently deleted with only a counter to show for it.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "pdf_extract",
    "extract_batch.py",
)


def _load_tool():
    """Import the tool by path — it lives outside the package tree."""
    spec = importlib.util.spec_from_file_location("_extract_batch_under_test", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _spectrum(peaks: list[tuple[int, int]], size=(480, 360)) -> Image.Image:
    """A plot with a baseline and peaks at given (x, height).

    Carries a deterministic gradient so entropy clears _MIN_ENTROPY and the
    PNG clears _MIN_FIG_BYTES — otherwise the size/entropy pre-filters drop
    the fixture before the dedup logic under test ever sees it. The gradient
    is identical across fixtures, so only the PEAKS distinguish them, which is
    exactly the near-identical-panel case this file is about.
    """
    img = Image.new("RGB", size, "white")
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            v = 200 + ((x * 7 + y * 13) % 56)
            px[x, y] = (v, v, v)
    d = ImageDraw.Draw(img)
    base = size[1] - 20
    d.line([(10, base), (size[0] - 10, base)], fill="black", width=2)
    for x, h in peaks:
        d.line([(x, base), (x, base - h)], fill="black", width=3)
    return img


def _write(tmpdir, name, img):
    p = os.path.join(tmpdir, name)
    img.save(p)
    return p


def test_similar_spectra_with_distinct_peaks_both_survive(tmp_path, capsys):
    """THE regression. Same axes and envelope, genuinely different peaks —
    two real figures, not a duplicate. At the old 4-bit radius this pair was
    collapsed to one."""
    mod = _load_tool()
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"

    a = _spectrum([(60, 120), (140, 90), (220, 60)])
    b = _spectrum([(95, 60), (175, 130), (250, 100)])
    _write(str(src), "a.png", a)
    _write(str(src), "b.png", b)

    kept, dropped, _ = mod._collect_figures(str(src), str(dest))
    assert kept == 2, f"kept={kept} dropped={dropped} — a real panel was eaten"


def test_the_confirm_stage_is_what_saves_a_small_peak_shift(tmp_path):
    """Proves the two-stage test earns its keep.

    A 6px peak shift collides at 8x8 INSIDE the old 4-bit radius — the old
    code would have deleted one of these — but separates at the 16x16 confirm
    resolution, which carries the detail that distinguishes near-identical
    panels. Asserting both distances directly pins the mechanism, not just the
    outcome.
    """
    mod = _load_tool()
    a = _spectrum([(60, 120), (140, 90), (220, 60)])
    b = _spectrum([(66, 118), (146, 92), (226, 58)])

    d8 = bin(mod._dhash(a) ^ mod._dhash(b)).count("1")
    d16 = bin(
        mod._dhash(a, mod._DHASH_CONFIRM_SIZE) ^ mod._dhash(b, mod._DHASH_CONFIRM_SIZE)
    ).count("1")

    assert d8 <= 4, f"fixture no longer exercises the old radius (d8={d8})"
    assert d16 > mod._DHASH_CONFIRM_DIST, (
        f"confirm stage does not separate these (d16={d16} <= "
        f"{mod._DHASH_CONFIRM_DIST}) — the pair would still be merged"
    )

    src = tmp_path / "src"
    src.mkdir()
    _write(str(src), "a.png", a)
    _write(str(src), "b.png", b)
    kept, dropped, _ = mod._collect_figures(str(src), str(tmp_path / "dest"))
    assert (kept, dropped) == (2, 0)


def test_true_duplicate_is_still_dropped_and_names_its_accuser(tmp_path, capsys):
    mod = _load_tool()
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"

    img = _spectrum([(60, 120), (140, 90)])
    _write(str(src), "a.png", img)
    _write(str(src), "b.png", img.copy())  # byte-identical content

    kept, dropped, _ = mod._collect_figures(str(src), str(dest))
    assert kept == 1 and dropped == 1

    err = capsys.readouterr().err
    assert "figdrop" in err and "reason=dhash" in err
    # The drop must name the crop it collided with, not just a count.
    assert "matches=fig_00.png" in err
    assert "d8=" in err and "d16=" in err


def test_decode_error_is_logged_as_error_not_as_a_dedup_drop(tmp_path, capsys):
    """Errors used to be folded into the same counter as duplicates, so a
    decoder failure was indistinguishable from a near-duplicate."""
    mod = _load_tool()
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"

    # Large enough to pass the byte-size gate, but not a valid image.
    with open(os.path.join(str(src), "broken.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8192)

    kept, dropped, _ = mod._collect_figures(str(src), str(dest))
    assert kept == 0 and dropped == 1
    err = capsys.readouterr().err
    assert "reason=error:" in err


def test_threshold_is_configurable_for_calibration():
    mod = _load_tool()
    assert mod._DHASH_MAX_DIST == 2, "default radius should be the tightened one"
    assert mod._DHASH_CONFIRM_SIZE == 16
