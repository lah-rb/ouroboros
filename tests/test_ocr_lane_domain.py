"""The ocr lane follows the mission's routing: local paddle by default, a
REMOTE lane iff `llmvp_domains` carries an `ocr` key.

WHY THE SHAPE MATTERS. A lane served by another engine must be gated on THAT
engine (its own resource, est_kv=0, seats=0) or it is admitted against local
cells it never spends — the no-op the first remote curate attempt produced.
And the ocr lane's local shape is a measured configuration (paddle on its
own device, one subprocess at a time) that must not move when no domain is
configured.
"""

from __future__ import annotations

from agent.scheduler import worker_pool as wp

REMOTE = {
    "ocr": {
        "endpoint": "http://192.168.1.209:8008/graphql",
        "model": "paddle-ocr-vl-mac",
    }
}


def _ocr(lanes):
    return next(ln for ln in lanes if ln.name == "ocr")


def test_no_domain_map_keeps_the_local_paddle_lane():
    lane = _ocr(wp.lanes_for_scraper(None))
    assert (lane.resource, lane.est_kv, lane.seats, lane.domain) == ("paddle", 0, 0, "")


def test_a_curate_only_domain_map_does_not_touch_the_ocr_lane():
    lane = _ocr(
        wp.lanes_for_scraper({"curate_remote": {"endpoint": "http://x/graphql"}})
    )
    assert lane.resource == "paddle" and lane.domain == ""


def test_an_ocr_domain_builds_a_remote_lane_gated_on_its_own_resource():
    lane = _ocr(wp.lanes_for_scraper(REMOTE))
    assert lane.domain == "ocr"
    assert lane.resource == "remote_vision_seat"
    assert (lane.est_kv, lane.seats) == (0, 0), "never admitted against local cells"
    assert lane.flow == "ocr_drain", "same drain flow; only the engine moves"


def test_the_remote_vision_resource_has_an_inflight_cap():
    assert wp.DEFAULT_LANE_MAX_INFLIGHT["remote_vision_seat"] == 1


def test_disable_lanes_still_removes_a_remote_ocr_lane(monkeypatch):
    monkeypatch.setenv("OUROBOROS_DISABLE_LANES", "ocr")
    assert not [ln for ln in wp.lanes_for_scraper(REMOTE) if ln.name == "ocr"]


def test_the_other_lanes_are_unchanged_by_the_ocr_domain():
    a = [
        (ln.name, ln.resource, ln.domain)
        for ln in wp.lanes_for_scraper(None)
        if ln.name != "ocr"
    ]
    b = [
        (ln.name, ln.resource, ln.domain)
        for ln in wp.lanes_for_scraper(REMOTE)
        if ln.name != "ocr"
    ]
    assert a == b
