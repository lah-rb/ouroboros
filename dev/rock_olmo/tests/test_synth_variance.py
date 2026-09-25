"""The variance sampler reproduces the measured instrument distributions."""

import os
import random
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from synth_variance import (  # noqa: E402
    FLIP_P,
    OUTLIER_SHARE,
    fmt_peaks,
    instrument_record,
    jitter_raman,
    perturb_bands,
    perturb_libs,
    rng_for,
    sample_libs,
    sample_raman,
)

BANDS = [128.0, 206.3, 264.1, 355.7, 401.2, 464.8]
REL = [0.31, 0.55, 0.12, 0.05, 0.08, 1.0]


def _q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def test_raman_jitter_quantiles_match_rruff_within_species():
    rng = random.Random(1)
    deltas = []
    for _ in range(100_000):
        inst = sample_raman(rng)
        deltas.append(abs(jitter_raman(rng, inst)))
    med, p75, p90, p95 = (_q(deltas, p) for p in (0.5, 0.75, 0.9, 0.95))
    assert 1.0 <= med <= 1.7, med
    assert 2.3 <= p75 <= 3.6, p75
    assert 4.3 <= p90 <= 6.6, p90
    assert 6.5 <= p95 <= 9.6, p95


def test_class_mix_and_outlier_share():
    rng = random.Random(2)
    insts = [sample_raman(rng) for _ in range(50_000)]
    share = {
        k: sum(i.klass == k for i in insts) / len(insts)
        for k in ("lab", "portable", "handheld")
    }
    assert abs(share["lab"] - 0.5) < 0.02 and abs(share["handheld"] - 0.15) < 0.02
    port = [i for i in insts if i.klass != "lab"]
    off = sum(1 for i in port if i.offset_cm1 != 0.0) / len(port)
    # The beryl paper saw 1 unit in 7 sit 4-9 cm-1 off; the RRUFF within-
    # species quantiles (the broader evidence) calibrate the share at ~0.25
    # together with the class sigmas. Pin the constant and its plausible band.
    assert abs(off - OUTLIER_SHARE) < 0.02, off
    assert 0.14 <= OUTLIER_SHARE <= 0.30
    assert all(4.0 <= abs(i.offset_cm1) <= 9.0 for i in port if i.offset_cm1)
    assert all(i.offset_cm1 == 0.0 for i in insts if i.klass == "lab")


def test_perturb_bands_shape_and_rules():
    rng = random.Random(3)
    lab = sample_raman(rng)
    lab.klass, lab.cutoff_cm1, lab.bandwidth_cm1, lab.offset_cm1 = "lab", 50.0, 2.0, 0.0
    out = perturb_bands(rng, BANDS, REL, lab)
    assert len(out) == len(BANDS)  # nothing cut on a sharp lab unit
    assert out == sorted(out) and max(r for _, r in out) == 1.0
    assert all(abs(b - o[0]) < 6 for b, o in zip(BANDS, out))
    hand = sample_raman(rng)
    hand.klass, hand.cutoff_cm1, hand.bandwidth_cm1 = "handheld", 220.0, 15.0
    out2 = perturb_bands(rng, BANDS, REL, hand)
    assert all(b >= 210 for b, _ in out2)  # 128 and 206 cut
    assert len(out2) < len(BANDS)  # weak bands (0.05, 0.08) lost on the wide slit
    text = fmt_peaks(out)
    assert text.count("(") == len(out) and "1.00" in text


def test_strongest_flip_rate_on_near_equal_pairs():
    rng = random.Random(4)
    flips = 0
    n = 20_000
    for _ in range(n):
        inst = sample_raman(rng)
        inst.cutoff_cm1, inst.bandwidth_cm1, inst.offset_cm1 = 50.0, 2.0, 0.0
        out = perturb_bands(rng, [200.0, 400.0], [0.95, 1.0], inst)
        # after renormalisation the flipped case puts 1.00 on 200
        if out[0][1] == 1.0 and out[1][1] < 1.0:
            flips += 1
    rate = flips / n
    # the 0.85–1.15 gain jitter alone flips a 0.95/1.00 pair often; the
    # explicit flip adds FLIP_P on top -- the rate must sit clearly above the
    # gain-only floor and below 1
    assert 0.35 < rate < 0.75, rate


def test_libs_positions_fixed_intensities_move_windows_cut():
    rng = random.Random(5)
    groups = [
        {
            "stage_label": "Fe I",
            "lines": [
                {"nm": 371.99, "rel": 100.0},
                {"nm": 438.35, "rel": 62.0},
                {"nm": 259.94, "rel": 20.0},
            ],
        },
        {
            "stage_label": "Ca II",
            "lines": [{"nm": 393.37, "rel": 90.0}, {"nm": 854.21, "rel": 12.0}],
        },
    ]
    for _ in range(200):
        inst = sample_libs(rng)
        out = perturb_libs(rng, groups, inst)
        lo, hi = inst.window_nm
        for g in out:
            for ln in g["lines"]:
                assert lo - 0.2 <= ln["nm"] <= hi + 0.2
                src = min(
                    (l["nm"] for gg in groups for l in gg["lines"]),
                    key=lambda x: abs(x - ln["nm"]),
                )
                assert abs(src - ln["nm"]) <= 0.1 + 1e-9
        assert max(ln["rel"] for g in out for ln in g["lines"]) == 100.0
    inst = sample_libs(rng)
    inst.window_nm = (350.0, 500.0)
    out = perturb_libs(rng, groups, inst)
    nms = [ln["nm"] for g in out for ln in g["lines"]]
    assert all(350 <= x <= 500 for x in nms) and len(nms) == 3
    assert 8000 <= inst.plasma_t_k <= 12000 and inst.slots()["libs_T"].endswith("00")


def test_determinism_and_records():
    a = perturb_bands(
        rng_for("f", "t", 1), BANDS, REL, sample_raman(rng_for("f", "t", 1, "i"))
    )
    b = perturb_bands(
        rng_for("f", "t", 1), BANDS, REL, sample_raman(rng_for("f", "t", 1, "i"))
    )
    c = perturb_bands(
        rng_for("f", "t", 2), BANDS, REL, sample_raman(rng_for("f", "t", 2, "i"))
    )
    assert a == b and a != c
    rec = instrument_record(sample_raman(random.Random(9)))
    assert set(rec) >= {
        "klass",
        "laser_nm",
        "cutoff_cm1",
        "tol",
        "calibration",
        "offset_cm1",
    }
    lrec = instrument_record(sample_libs(random.Random(9)))
    assert isinstance(lrec["window_nm"], list) and len(lrec["window_nm"]) == 2
    assert FLIP_P == 0.29


def test_vary_spectrum_severity_controls_loss_and_extras():
    import synth_variance as sv

    peaks = list(zip(BANDS, REL))
    lab = sv.RamanInstrument("lab", 532, 3.0, 50.0, "±1 cm-1", "a neon lamp", 0.0)
    pool = [900.0, 1100.0]
    calm = sv.vary_spectrum(random.Random(1), peaks, lab, pool, severity=0.0, sigma=0.0)
    assert len(calm) == len(peaks) and all(abs(a[0] - b) < 6 for a, b in zip(calm, BANDS))
    assert [r for _, r in calm] == REL  # sigma 0: intensities untouched
    wild = sv.vary_spectrum(random.Random(2), peaks, lab, pool, severity=1.0, extra_per_severity=6.0)
    assert all(min(abs(p - x) for x in pool) < 6 for p, _ in wild)  # every canonical band lost; extras only
    lone = sv.vary_spectrum(random.Random(3), peaks, lab, [], severity=1.0)
    assert len(lone) == 1 and abs(lone[0][0] - 464.8) < 6  # nothing survives -> the strongest band is kept
    high_cut = sv.RamanInstrument("handheld", 785, 12.0, 250.0, "±5 cm-1", "a neon lamp", 0.0)
    assert all(p >= 250.0 for p, _ in sv.vary_spectrum(random.Random(4), peaks, high_cut, pool, severity=0.0))
    a = sv.vary_spectrum(sv.rng_for("x", 1), peaks, lab, pool)
    assert a == sv.vary_spectrum(sv.rng_for("x", 1), peaks, lab, pool)  # seeded draws reproduce


def test_strongest_keeps_the_k_most_intense_position_sorted():
    import synth_variance as sv

    pairs = list(zip(BANDS, REL))
    assert sv.strongest(pairs, 3) == [(128.0, 0.31), (206.3, 0.55), (464.8, 1.0)]
    assert sv.strongest(pairs, 10) == sorted(pairs)
