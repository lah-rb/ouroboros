"""The recover lane re-checks the unresolved pool on a calendar (2026-10-02).

Both ways an unresolved paper was looked at again were tied to stages that
finish: acquisition retries rode the catalog sweep (closed 2026-09-06; last
retry 2026-09-01 with 1,276 papers eligible), and the recovery pass stamped
every paper once and never again. The lane now also runs stale acquisition
retries and a monthly re-pass that re-checks the stored links themselves.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from agent.actions.scholarly_actions import (
    action_recover_oa_locations,
    read_databank,
    recovery_repass_due,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import DownloadResult, HttpResult
from agent.models import FlowMeta, StepInput

_WAYBACK = "https://web.archive.org/cdx/search/cdx"
_CORE = "https://api.core.ac.uk/v3/search/works"
_UNPAYWALL = "https://api.unpaywall.org/v2/"
_FAIL = DownloadResult(
    success=False, url="", path="", bytes_written=0, error="HTTP 404"
)


def _ago(days: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()


def _rec(key="p1", **kw):
    base = {
        "paper_key": key,
        "title": "T",
        "status": "cataloged",
        "access_status": "oa_unresolved",
        "oa_pdf_url": f"https://repo.example/{key}.pdf",
        "oa_attempted": [f"https://repo.example/{key}.pdf"],
        "failure_reason": "HTTP 404 (tried 1 location(s))",
        "doi": f"10.1000/{key}",
        "tags": [],
        "updated_at": _ago(40),
    }
    base.update(kw)
    return base


def _si(fx):
    return StepInput(
        context={},
        params={},
        meta=FlowMeta(flow_name="oa_recover_drain", step_id="drain"),
        effects=fx,
    )


def _fx(records, downloads=None):
    return MockEffects(
        files={"databank/papers.jsonl": "".join(json.dumps(r) + "\n" for r in records)},
        http_responses={
            _WAYBACK: HttpResult(
                status=200, url=_WAYBACK, json_data=[["timestamp", "original"]]
            ),
            _CORE: HttpResult(status=200, url=_CORE, json_data={"results": []}),
            _UNPAYWALL: HttpResult(status=404, url=_UNPAYWALL),
        },
        http_downloads=downloads or {},
    )


def _downloads(fx) -> list[str]:
    return [c.args["url"] for c in fx.calls if c.method == "http_download"]


def test_the_repass_calendar(monkeypatch):
    assert recovery_repass_due(_rec(oa_recover_attempted_at=_ago(40)))
    assert not recovery_repass_due(_rec(oa_recover_attempted_at=_ago(10)))
    assert not recovery_repass_due(_rec()), "never walked: the first pass owns it"
    assert not recovery_repass_due(
        _rec(access_status="oa_pdf", oa_recover_attempted_at=_ago(40))
    )
    monkeypatch.setenv("OUROBOROS_OA_REPASS_DAYS", "0")
    assert not recovery_repass_due(_rec(oa_recover_attempted_at=_ago(400)))


@pytest.mark.asyncio
async def test_a_repaired_link_is_found_by_the_monthly_repass(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OA_RECHECK_STALE", "0")
    rec = _rec(oa_recover_attempted_at=_ago(35))  # 404 then; fixed since
    fx = _fx([rec])  # the stored link now downloads (mock default: success)
    out = await action_recover_oa_locations(_si(fx))
    assert out.result["repassed"] == 1
    assert out.result["outcomes"] == [{"paper_key": "p1", "via": "recheck"}]
    done = (await read_databank(fx))["p1"]
    assert done["access_status"] == "oa_pdf" and done["pdf_path"] == "pdfs/p1.pdf"
    assert done["oa_recover_passes"] == 2
    assert not recovery_repass_due(done)


@pytest.mark.asyncio
async def test_a_walled_stored_link_is_not_rechecked(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OA_RECHECK_STALE", "0")
    walled = "https://www.sciencedirect.com/science/article/pii/X/pdf"
    rec = _rec(
        oa_pdf_url=walled, oa_attempted=[walled], oa_recover_attempted_at=_ago(35)
    )
    fx = _fx([rec], downloads={walled: _FAIL})
    await action_recover_oa_locations(_si(fx))
    assert walled not in _downloads(fx), "the same request to the same wall"
    after = (await read_databank(fx))["p1"]
    assert after["access_status"] == "oa_unresolved"
    assert not recovery_repass_due(after), "stamped: next look in a month"


@pytest.mark.asyncio
async def test_a_stale_acquisition_failure_is_retried_and_stays_catalogued(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OA_REPASS_PAPERS", "0")
    rec = _rec(
        failure_reason="response is text/html, not a document (tried 1 location(s))",
        oa_recover_attempted_at=_ago(5),  # recovery walked it; not due again
    )
    fx = _fx([rec])
    out = await action_recover_oa_locations(_si(fx))
    assert out.result["stale_retried"] == 1
    assert out.result["outcomes"] == [{"paper_key": "p1", "via": "retry"}]
    done = (await read_databank(fx))["p1"]
    assert done["access_status"] == "oa_pdf" and done["pdf_path"]
    assert done["status"] == "cataloged", "a catalogued paper stays catalogued"
    assert done["oa_retry_count"] == 1 and done["oa_retried_at"]


@pytest.mark.asyncio
async def test_one_round_does_first_pass_retries_and_repasses_within_budgets(
    monkeypatch,
):
    monkeypatch.setenv("OUROBOROS_OA_RECOVER_PAPERS", "1")
    monkeypatch.setenv("OUROBOROS_OA_RECHECK_STALE", "1")
    monkeypatch.setenv("OUROBOROS_OA_REPASS_PAPERS", "1")
    html = "response is text/html, not a document (tried 1 location(s))"
    recs = [
        _rec("new1"),  # never walked
        _rec("new2"),
        _rec("stale1", failure_reason=html, oa_recover_attempted_at=_ago(5)),
        _rec("stale2", failure_reason=html, oa_recover_attempted_at=_ago(5)),
        _rec("old1", oa_recover_attempted_at=_ago(60)),
        _rec("old2", oa_recover_attempted_at=_ago(31)),
    ]
    fails = {f"https://repo.example/{r['paper_key']}.pdf": _FAIL for r in recs}
    fx = _fx(recs, downloads=fails)
    out = await action_recover_oa_locations(_si(fx))
    assert (
        out.result["attempted"],
        out.result["stale_retried"],
        out.result["repassed"],
    ) == (1, 1, 1)
    bank = await read_databank(fx)
    assert bank["new1"]["oa_recover_attempted_at"] and not bank["new2"].get(
        "oa_recover_attempted_at"
    )
    assert bank["stale1"].get("oa_retry_count") == 1 and not bank["stale2"].get(
        "oa_retry_count"
    )
    assert bank["old1"].get("oa_recover_passes") == 2, "oldest pass first"
    assert not bank["old2"].get("oa_recover_passes")


@pytest.mark.asyncio
async def test_nothing_due_declines():
    fx = _fx([_rec(oa_recover_attempted_at=_ago(3), updated_at=_ago(0.1))])
    out = await action_recover_oa_locations(_si(fx))
    assert (
        out.result["attempted"] == 0
        and "no retry or re-pass due" in out.result["reason"]
    )
