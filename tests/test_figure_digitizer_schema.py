"""The artifact contract.

The load-bearing property is where these records DO NOT go: a digitised peak
appears nowhere in the paper's text, so it must never be written where the
curation grounding gate would have to judge it. Every record also says out
loud that it is derived, so no downstream reader can mistake a trace of a
figure for a value the paper stated.
"""

from __future__ import annotations

import json
import math

from tools.figure_digitizer import schema


class _Reloc:
    crop_px = (400, 300)
    ncc = 0.9987
    margin = 0.42
    full_page = False


class _Src:
    tier = "native_raster"
    page = 3
    rect_pt = (10.0, 20.0, 210.0, 170.0)
    limited_by = "native_image_dpi"
    effective_dpi = 300.0
    resolution_gain_vs_crop = 1.875
    xref = 37
    native_px = (886, 768)
    sub_box_px = (7.0, 17.0, 864.0, 759.0)
    native_rect_pt = (10.0, 20.0, 210.0, 170.0)
    rect_covered_by_native = 0.98
    native_ext = "jpeg"
    n_vector_curves = 0
    jpeg_round_trips = 1


def test_the_sidecar_is_not_written_into_the_dataset_directory(tmp_path):
    """The whole reason this layer exists. A digitised peak is absent from the
    paper's text, so routing it into databank/dataset/ would fail the pack's
    grounding check — and inlining it to pass would defeat the check rather
    than satisfy it."""
    path = schema.figdata_path(str(tmp_path), "doi_10.1234_x")
    assert "/databank/figdata/" in path
    assert "/dataset/" not in path


def test_every_record_declares_itself_derived():
    rec = schema.figure_record("fig_01.png", "sourced", reloc=_Reloc(), src=_Src())
    assert rec["provenance"]["derivation"] == "plot_digitisation"
    assert "not a value the paper states" in rec["provenance"]["reading"]


def test_a_source_block_carries_the_evidence_for_its_own_tier():
    rec = schema.figure_record("fig_01.png", "sourced", reloc=_Reloc(), src=_Src())
    src = rec["source"]
    assert src["tier"] == "native_raster"
    assert src["page"] == 3
    assert src["relocation_ncc"] == 0.9987
    assert src["relocation_margin"] == 0.42
    assert src["native_px"] == [886, 768]
    assert src["effective_dpi"] == 300.0
    assert rec["precision"]["limited_by"] == "native_image_dpi"


def test_an_infinite_dpi_survives_json():
    """A vector source has no resolution ceiling, and JSON has no infinity.
    Serialising one as NaN/Infinity produces a file no strict reader accepts."""

    class _Vec(_Src):
        tier = "vector"
        limited_by = "vector_source"
        effective_dpi = math.inf
        resolution_gain_vs_crop = math.inf

    rec = schema.figure_record("fig_02.png", "sourced", reloc=_Reloc(), src=_Vec())
    assert rec["source"]["effective_dpi"] is None
    text = json.dumps(rec, allow_nan=False)  # raises if an inf leaked through
    assert "Infinity" not in text


def test_resolvable_is_the_grid_times_the_pen():
    """A peak narrower than the stroke that drew it cannot be resolved, no
    matter how fine the sampling grid is."""
    p = schema.precision_block(_Src(), grid_unit_per_px=0.031, stroke_px=3.0)
    assert p["resolvable_unit"] == 0.093
    assert p["grid_unit_per_px"] == 0.031


def test_precision_is_null_before_calibration():
    p = schema.precision_block(_Src())
    assert p["grid_unit_per_px"] is None
    assert p["resolvable_unit"] is None
    assert p["limited_by"] == "native_image_dpi"


def test_a_sidecar_round_trips(tmp_path):
    rec = schema.paper_record(
        "doi_10.1234_x",
        [schema.figure_record("fig_01.png", "sourced", reloc=_Reloc(), src=_Src())],
    )
    path = schema.write_sidecar(str(tmp_path), rec)
    with open(path, encoding="utf-8") as fh:
        back = json.load(fh)
    assert back["paper_key"] == "doi_10.1234_x"
    assert back["tool_version"] == schema.SCHEMA_VERSION
    assert back["figs"][0]["source"]["tier"] == "native_raster"


def test_the_run_report_counts_the_yield_story():
    r = schema.RunReport()
    for ncc in (0.999, 0.998, 0.21):
        r.relocation_ncc.append(ncc)
    r.gain.extend([1.0, 1.9, 3.7])
    r.bump("tier", "native_raster")
    out = r.as_dict()
    assert out["relocation"]["n"] == 3
    assert out["relocation"]["located_ge_0.98"] == 2
    assert out["resolution_gain"]["ge_2x"] == 1
    assert out["tier"] == {"native_raster": 1}
