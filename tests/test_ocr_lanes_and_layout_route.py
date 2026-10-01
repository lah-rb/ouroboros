"""Remote OCR scaling (2026-09-30): several OCR lanes against a remote paddle, and the
layout model served beside it (layout_server.py), selected by the mission's ocr route.
"""

from __future__ import annotations

import importlib.util
import os
import types

import numpy as np
import pytest

from agent.actions.extraction_actions import _ocr_route_argv
from agent.scheduler import worker_pool as wp

REMOTE = {
    "ocr": {"endpoint": "http://192.168.1.76:8008/graphql", "model": "paddle-ocr-vl"}
}

_WIRE = os.path.join(
    os.path.dirname(__file__), "..", "tools", "pdf_extract", "layout_wire.py"
)
_spec = importlib.util.spec_from_file_location("_layout_wire", _WIRE)
wire = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wire)


def _names(lanes):
    return [ln.name for ln in lanes if ln.flow == "ocr_drain"]


def test_remote_ocr_gets_the_configured_number_of_lanes(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OCR_LANES", "2")
    monkeypatch.delenv("OUROBOROS_DISABLE_LANES", raising=False)
    lanes = [ln for ln in wp.lanes_for_scraper(REMOTE) if ln.flow == "ocr_drain"]
    assert [ln.name for ln in lanes] == ["ocr", "ocr2"]
    assert {(ln.resource, ln.domain) for ln in lanes} == {("remote_vision_seat", "ocr")}


def test_local_paddle_always_gets_one_lane(monkeypatch):
    """A second in-process paddle is the pairing that wedges tensor-split muse."""
    monkeypatch.setenv("OUROBOROS_OCR_LANES", "3")
    monkeypatch.delenv("OUROBOROS_DISABLE_LANES", raising=False)
    assert _names(wp.lanes_for_scraper(None)) == ["ocr"]


def test_disabling_ocr_disables_the_whole_family(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OCR_LANES", "3")
    monkeypatch.setenv("OUROBOROS_DISABLE_LANES", "ocr")
    assert _names(wp.lanes_for_scraper(REMOTE)) == []


def test_the_remote_seat_cap_follows_the_lane_count(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OCR_LANES", "2")
    monkeypatch.delenv("OUROBOROS_DISABLE_LANES", raising=False)
    pool = wp.WorkerPool(
        effects=types.SimpleNamespace(),
        lanes=wp.lanes_for_scraper(REMOTE),
        capacity_model=None,
    )
    assert pool.max_inflight["remote_vision_seat"] == 2
    assert wp.DEFAULT_LANE_MAX_INFLIGHT["remote_vision_seat"] == 1  # default untouched


def test_the_route_hands_the_tool_its_layout_server():
    fx = types.SimpleNamespace(
        _inference_domain="ocr",
        _llmvp_domains={
            "ocr": {**REMOTE["ocr"], "layout_endpoint": "http://192.168.1.76:8010/"}
        },
    )
    assert _ocr_route_argv(fx) == [
        "--llmvp-url", "http://192.168.1.76:8008",
        "--model", "paddle-ocr-vl",
        "--layout-url", "http://192.168.1.76:8010",
    ]  # fmt: skip
    fx._llmvp_domains = REMOTE
    assert "--layout-url" not in _ocr_route_argv(fx)


def test_the_wire_is_lossless_for_what_the_pipeline_sends():
    img = (np.arange(2 * 3 * 3) % 256).astype(np.uint8).reshape(2, 3, 3)
    assert np.array_equal(wire.decode_image(wire.encode_image(img)), img)
    kw = {
        "threshold": {0: 0.3, 7: 0.5},
        "layout_unclip_ratio": (1.0, 1.2),
        "layout_nms": True,
        "boxes": [
            {"score": np.float32(0.9), "polygon_points": np.ones((4, 2), np.float32)}
        ],
    }
    back = wire.decode_value(wire.encode_value(kw))
    assert back["threshold"] == {0: 0.3, 7: 0.5} and back["layout_unclip_ratio"] == (
        1.0,
        1.2,
    )
    pp = back["boxes"][0]["polygon_points"]
    assert pp.dtype == np.float32 and np.array_equal(pp, np.ones((4, 2)))
