"""OA recovery: the third-door routes for the oa_unresolved pool.

The pool (2,244 papers when built) is not retryable by re-fetching — 44%
publisher bot-walls, 37% landing pages whose declared PDF target also
refuses clients. Recovery asks SOMEONE ELSE: Wayback snapshots, the
page's own citation_pdf_url declaration, CORE's aggregated copy by DOI.
These tests pin the route ladder, the per-record durability, and the
one-pass stamp that keeps the lane from re-walking misses forever.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.scholarly_actions import (
    _meta_pdf_url,
    action_recover_oa_locations,
    read_databank,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import DownloadResult, HttpResult
from agent.models import FlowMeta, StepInput

_WAYBACK = "https://archive.org/wayback/available"
_CORE = "https://api.core.ac.uk/v3/search/works"


def _si(fx, params=None):
    return StepInput(
        context={},
        params=params or {},
        meta=FlowMeta(flow_name="oa_recover_drain", step_id="drain"),
        effects=fx,
    )


def _rec(key="p1", **kw):
    base = {
        "paper_key": key,
        "title": "T",
        "access_status": "oa_unresolved",
        "oa_pdf_url": f"https://pub.example/{key}/pdf",
        "failure_reason": "HTTP 403 (tried 1 location(s))",
        "doi": f"10.1000/{key}",
        "tags": [],
    }
    base.update(kw)
    return base


def _fx(records, http=None, downloads=None):
    return MockEffects(
        files={"databank/papers.jsonl": "".join(json.dumps(r) + "\n" for r in records)},
        http_responses=http or {},
        http_downloads=downloads or {},
    )


def _wb(snapshot_url):
    return HttpResult(
        status=200,
        url=_WAYBACK,
        json_data={
            "archived_snapshots": {
                "closest": {
                    "available": True,
                    "status": "200",
                    "url": snapshot_url,
                }
            }
        },
    )


_WB_MISS = HttpResult(status=200, url=_WAYBACK, json_data={"archived_snapshots": {}})


# ── the route ladder ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wayback_snapshot_recovers_a_walled_pdf():
    snap = "http://web.archive.org/web/2024/https://pub.example/p1/pdf"
    fx = _fx(
        [_rec()],
        http={_WAYBACK: _wb(snap)},
        downloads={
            "https://web.archive.org/web/2024/https://pub.example/p1/pdf": DownloadResult(
                success=True, url="u", path="pdfs/p1.pdf", bytes_written=9999
            )
        },
    )
    out = await action_recover_oa_locations(_si(fx))
    assert out.result["recovered"] == 1
    assert out.result["outcomes"][0]["via"] == "wayback"
    bank = await read_databank(fx)
    assert bank["p1"]["access_status"] == "oa_pdf"
    assert bank["p1"]["pdf_path"] == "pdfs/p1.pdf"
    # http, not https: the API's scheme is normalized before download
    assert bank["p1"]["oa_pdf_url"].startswith("https://web.archive.org/")


@pytest.mark.asyncio
async def test_meta_declaration_recovers_when_the_landing_page_serves_us():
    landing = (
        '<html><meta name="citation_pdf_url" '
        'content="https://cdn.example/real.pdf"></html>'
    )
    fx = _fx(
        [_rec()],
        http={
            _WAYBACK: _WB_MISS,
            "https://pub.example/p1/pdf": HttpResult(
                status=200, url="https://pub.example/p1/pdf", text=landing
            ),
        },
        downloads={
            "https://cdn.example/real.pdf": DownloadResult(
                success=True, url="u", path="pdfs/p1.pdf", bytes_written=9999
            )
        },
    )
    out = await action_recover_oa_locations(_si(fx))
    assert out.result["recovered"] == 1
    assert out.result["outcomes"][0]["via"] == "meta"
    # The declared target is CROSS-HOST and that is allowed: it is the
    # publisher's structured self-declaration, not a model's guess.


@pytest.mark.asyncio
async def test_core_by_doi_is_the_last_rung():
    fx = _fx(
        [_rec()],
        http={
            _WAYBACK: _WB_MISS,
            "https://pub.example/p1/pdf": HttpResult(
                status=403, url="https://pub.example/p1/pdf"
            ),
            _CORE: HttpResult(
                status=200,
                url=_CORE,
                json_data={
                    "results": [{"downloadUrl": "https://core.example/dl/1.pdf"}]
                },
            ),
        },
        downloads={
            "https://core.example/dl/1.pdf": DownloadResult(
                success=True, url="u", path="pdfs/p1.pdf", bytes_written=9999
            )
        },
    )
    out = await action_recover_oa_locations(_si(fx))
    assert out.result["recovered"] == 1
    assert out.result["outcomes"][0]["via"] == "core"


# ── misses, stamps, and durability ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_total_miss_is_stamped_and_never_rewalked():
    """One pass per record: aggregators lag months for recent papers, so
    a later pass is worth it — on a calendar, not a loop. Without the
    stamp this lane would burn the polite-request budget re-walking the
    same 2,244 misses every backoff interval."""
    fx = _fx(
        [_rec()],
        http={
            _WAYBACK: _WB_MISS,
            _CORE: HttpResult(status=200, url=_CORE, json_data={"results": []}),
        },
    )
    out1 = await action_recover_oa_locations(_si(fx))
    assert out1.result == {**out1.result, "attempted": 1, "recovered": 0}
    bank = await read_databank(fx)
    assert bank["p1"]["access_status"] == "oa_unresolved"
    assert bank["p1"]["oa_recover_attempted_at"]

    out2 = await action_recover_oa_locations(_si(fx))
    assert out2.result["attempted"] == 0
    assert "nothing unrecovered" in out2.result["reason"]


@pytest.mark.asyncio
async def test_each_record_books_before_the_next_is_walked():
    """Per-record append: a recovered PDF must survive whatever stops the
    round mid-walk (the OCR batch-booking lesson, ~350 pages redone)."""
    snap = "http://web.archive.org/web/2024/x"
    recs = [_rec("p1"), _rec("p2")]

    class _Boom(MockEffects):
        async def http_download(self, url, path, **kw):
            if "p2" in json.dumps(url) or self._dl_calls > 0:
                raise RuntimeError("network died mid-round")
            self._dl_calls += 1
            return DownloadResult(success=True, url=url, path=path, bytes_written=1)

    fx = _Boom(
        files={"databank/papers.jsonl": "".join(json.dumps(r) + "\n" for r in recs)},
        http_responses={
            _WAYBACK: _wb(snap),
            _CORE: HttpResult(status=200, url=_CORE, json_data={"results": []}),
        },
    )
    fx._dl_calls = 0
    with pytest.raises(RuntimeError):
        await action_recover_oa_locations(_si(fx))
    bank = await read_databank(fx)
    assert bank["p1"]["access_status"] == "oa_pdf", "p1's recovery was lost"


@pytest.mark.asyncio
async def test_strong_tagged_papers_are_walked_first_and_budget_binds():
    recs = [
        _rec("zzz_strong", tags=[{"aspect": "a", "relevance": "exact"}]),
        _rec("aaa_weak"),
    ]
    fx = _fx(
        recs,
        http={
            _WAYBACK: _WB_MISS,
            _CORE: HttpResult(status=200, url=_CORE, json_data={"results": []}),
        },
    )
    out = await action_recover_oa_locations(_si(fx, params={"budget": 1}))
    assert out.result["attempted"] == 1
    bank = await read_databank(fx)
    assert bank["zzz_strong"].get("oa_recover_attempted_at"), "strong tag must go first"
    assert not bank["aaa_weak"].get("oa_recover_attempted_at")
    assert out.result["remaining"] == 1


@pytest.mark.asyncio
async def test_already_attempted_urls_are_not_retried():
    """A URL in oa_attempted already failed http_download's document
    check once; hitting it again is the beat-on-the-wall pattern this
    action exists to replace."""
    snap_http = "http://web.archive.org/web/2024/x"
    snap_https = "https://web.archive.org/web/2024/x"
    fx = _fx(
        [_rec(oa_attempted=[snap_https])],
        http={
            _WAYBACK: _wb(snap_http),
            _CORE: HttpResult(status=200, url=_CORE, json_data={"results": []}),
        },
        downloads={
            snap_https: DownloadResult(
                success=True, url="u", path="pdfs/p1.pdf", bytes_written=1
            )
        },
    )
    out = await action_recover_oa_locations(_si(fx))
    assert out.result["recovered"] == 0, "re-tried a URL that already failed"


def test_meta_parser_handles_both_attribute_orders_and_relative_urls():
    a = _meta_pdf_url(
        '<meta name="citation_pdf_url" content="https://x.org/a.pdf">',
        "https://x.org/p",
    )
    b = _meta_pdf_url(
        '<meta content="/rel/b.pdf" name="citation_pdf_url">',
        "https://x.org/p/q",
    )
    assert a == "https://x.org/a.pdf"
    assert b == "https://x.org/rel/b.pdf"
    assert _meta_pdf_url("<html>no tag</html>", "https://x.org") == ""
