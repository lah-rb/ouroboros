"""The vision resolution floor — why small crops get enlarged before sending.

MEASURED 2026-08-12. The vision tower tokenises by PIXEL AREA at ~715 px per
token, so one token spans ~26.7 px of whatever is sent. A feature smaller than
that is averaged into a neighbouring patch, and the model falls back to
completing the pattern from the rest of the figure — which is how a 13-bar
stacked chart acquired mineral categories it does not contain.

Enlarging a small crop changes the sampling rate relative to the feature. At
1 Mpx one token spans ~15 native px instead of ~27, and phantom categories on
the test figure fell from 5 per run to 1 with no prompt change. A real
improvement, not a fix, and the code says so.

The two properties worth pinning are the ones a future edit would most
plausibly get wrong: it is a FLOOR (never shrink what is already big enough),
and it stops at the long-side ceiling (past which the server resizes back down,
so further enlargement buys tokens and no resolution).
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "fig_review",
    "fig_review.py",
)


def _load():
    spec = importlib.util.spec_from_file_location("_fig_review_floor_test", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_M = _load()
FLOOR = _M._MIN_VISION_PIXELS
CEIL = _M._MAX_VISION_LONG_SIDE


# ── it is a floor, not a target ───────────────────────────────────────


@pytest.mark.parametrize(
    "w,h",
    [(1291, 775), (2000, 1500), (4000, 300), (1500, 1500)],
)
def test_images_already_at_or_above_the_floor_are_left_alone(w, h):
    """Shrinking to hit a number would discard native detail we already have,
    to save a few seconds of prefill. Never worth it."""
    assert w * h >= FLOOR
    assert _M.upscale_factor(w, h) == 1.0


@pytest.mark.parametrize("w,h", [(483, 286), (741, 439), (300, 200), (1000, 600)])
def test_small_crops_are_raised_to_the_floor(w, h):
    f = _M.upscale_factor(w, h)
    assert f > 1.0
    assert (w * f) * (h * f) == pytest.approx(FLOOR, rel=0.02)


# ── the ceiling the server imposes ────────────────────────────────────


@pytest.mark.parametrize("w,h", [(3000, 100), (2800, 200), (1600, 120), (900, 80)])
def test_enlargement_stops_at_the_long_side_ceiling(w, h):
    """Past ~3190px the server resizes back down, so going further costs
    tokens and delivers no resolution — measured: 3x cost more than 2x and
    scored worse."""
    f = _M.upscale_factor(w, h)
    assert max(w, h) * f <= CEIL + 1
    assert f >= 1.0, "a wide thin strip must never be SHRUNK by the ceiling"


def test_a_strip_too_wide_to_help_is_left_essentially_alone():
    """3000x100 cannot reach the floor without blowing the ceiling; it should
    creep to the ceiling rather than being scaled to the floor regardless."""
    f = _M.upscale_factor(3000, 100)
    assert 1.0 <= f < 1.2
    assert 3000 * f <= CEIL + 1


# ── degenerate input must not raise ───────────────────────────────────


@pytest.mark.parametrize("w,h", [(0, 0), (0, 500), (500, 0), (-10, 10)])
def test_degenerate_sizes_return_one_rather_than_raising(w, h):
    """A figure with an unreadable header must cost its own dispatch, not the
    whole batch."""
    assert _M.upscale_factor(w, h) == 1.0


# ── the read path degrades to the original, never to nothing ──────────


def test_unreadable_image_falls_back_to_raw_bytes(tmp_path):
    """Resizing is an optimisation. A small figure still yields usable
    figtext; a crashed dispatch yields none."""
    p = tmp_path / "not-an-image.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage")
    assert _M._read_at_floor(str(p)) == p.read_bytes()


def test_a_real_small_png_comes_back_enlarged(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    p = tmp_path / "small.png"
    Image.new("RGB", (400, 300), "white").save(p)
    out = _M._read_at_floor(str(p))
    assert out != p.read_bytes(), "a 0.12 Mpx crop should have been enlarged"
    import io

    assert Image.open(io.BytesIO(out)).size[0] > 400


def test_a_real_large_png_is_returned_byte_identical(tmp_path):
    """Above the floor the bytes must pass through untouched — no re-encode,
    no quality loss, no wasted work."""
    Image = pytest.importorskip("PIL.Image")
    p = tmp_path / "large.png"
    Image.new("RGB", (1400, 900), "white").save(p)
    assert _M._read_at_floor(str(p)) == p.read_bytes()
