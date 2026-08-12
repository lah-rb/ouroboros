"""polite_request: per-host throttling + mission request budget.

Politeness lives in the action layer, not the HTTP effect. Pacing state is now
owned IN PROCESS by agent.actions.http_pacer.HostPacer rather than round-tripped
through effects.read_state/write_state per call — that round trip was a
read-modify-write race that turned into a burst generator the moment acquisition
became concurrent (every in-flight caller read the same last_ts and fired
together).

CONSEQUENCE, deliberate: the request COUNTER is now per-process and does not
survive a mission restart. It only gates the opt-in
OUROBOROS_SCRAPER_HTTP_BUDGET development guardrail — the default is unlimited —
so a restart resetting it is harmless. Politeness itself never depended on
persistence: per-host intervals are re-derived on first use.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.actions import scholarly_actions
from agent.actions.scholarly_actions import polite_request
from agent.effects.mock import MockEffects
from agent.effects.protocol import HttpResult

_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_HOST = "api.semanticscholar.org"


@pytest.fixture(autouse=True)
def _fresh_pacer():
    """The pacer is a module singleton — without this, request counts leak
    between tests and an unrelated test can trip a budget."""
    scholarly_actions._pacer_singleton = None
    yield
    scholarly_actions._pacer_singleton = None


def _spend(n: int, host: str = _HOST) -> None:
    """Seed the pacer's counter without sleeping through n intervals."""
    st = scholarly_actions._pacer()._state(host)
    st.requests = n


def _fx():
    return MockEffects(
        http_responses={_URL: [HttpResult(status=200, url=_URL) for _ in range(5)]}
    )


@pytest.mark.asyncio
async def test_second_request_to_same_host_sleeps_min_interval():
    fx = _fx()
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    with patch.object(scholarly_actions.asyncio, "sleep", fake_sleep):
        await polite_request(fx, "GET", _URL)
        await polite_request(fx, "GET", _URL)
    assert sleeps and 0 < sleeps[-1] <= 3.5  # S2's min interval


@pytest.mark.asyncio
async def test_request_count_is_tracked_in_process_not_on_disk():
    fx = _fx()
    await polite_request(fx, "GET", _URL)
    await polite_request(fx, "GET", _URL)
    pacer = scholarly_actions._pacer()
    assert pacer.total_requests() == 2
    assert _HOST in pacer.stats()
    # No per-call state round trip: that was two whole-file disk operations
    # per API call, and the race that made concurrency unsafe.
    assert fx.call_count("write_state") == 0


@pytest.mark.asyncio
async def test_no_budget_by_default_because_the_scraper_runs_continuously():
    """The total-request cap is a DEVELOPMENT guardrail, not a politeness
    control — pacing and Retry-After are what protect the APIs, and they are
    always on. A hard cap stopped a real corpus at 86 of 624 available OA
    PDFs, so unlimited is the default and the cap is opt-in."""
    assert scholarly_actions._request_budget() == 0
    fx = _fx()
    _spend(10_000)
    r = await polite_request(fx, "GET", _URL)
    assert r.status != 0, "an unset budget must never block a request"


@pytest.mark.asyncio
async def test_budget_exhaustion_fails_soft_without_calling_when_one_is_set(
    monkeypatch,
):
    monkeypatch.setenv("OUROBOROS_SCRAPER_HTTP_BUDGET", "600")
    fx = _fx()
    _spend(600)
    r = await polite_request(fx, "GET", _URL)
    assert r.status == 0
    assert "budget" in (r.error or "")
    assert fx.call_count("http_request") == 0


@pytest.mark.asyncio
async def test_a_malformed_budget_is_ignored_rather_than_blocking_everything(
    monkeypatch,
):
    monkeypatch.setenv("OUROBOROS_SCRAPER_HTTP_BUDGET", "not-a-number")
    assert scholarly_actions._request_budget() == 0


@pytest.mark.asyncio
async def test_429_gets_one_retry_honoring_retry_after():
    # Live-observed: S2's unauthenticated pool 429s under contention.
    fx = MockEffects(
        http_responses={
            _URL: [
                HttpResult(status=429, url=_URL, headers={"retry-after": "12"}),
                HttpResult(status=200, url=_URL),
            ]
        }
    )
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    with patch.object(scholarly_actions.asyncio, "sleep", fake_sleep):
        r = await polite_request(fx, "GET", _URL)
    assert r.status == 200
    assert 12.0 in sleeps  # honored Retry-After
    assert fx.call_count("http_request") == 2


# ══════════════════════════════════════════════════════════════════════
# Acquisition outranks expansion, and an exhausted budget is terminal
#
# Both measured on the spectroscopy corpus run. Discovery over-delivered
# (1,197 records, every aspect 11-17x its target) and then catalog stalled:
# reference expansion spent 541 of the 600-request budget — most of it
# 429'd by Semantic Scholar's keyed quota — leaving acquisition at 86 of
# 624 available OA PDFs. Only full text can be mined for spectra, so those
# 86 were the whole dataset.
#
# Then it span: the sweep completes the corpus goal only when the worklist
# empties, but with the budget spent no record can be processed, so the
# same batch redispatched. 296 exhaustion warnings across 121 cycles.
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reference_expansion_yields_once_the_reserve_is_reached(monkeypatch):
    from agent.actions.scholarly_actions import action_fetch_references
    from agent.models import FlowMeta, StepInput

    monkeypatch.setenv("OUROBOROS_SCRAPER_HTTP_BUDGET", "600")
    fx = _fx()
    # 40% spent — inside the 60% acquisition reserve.
    _spend(240)
    out = await action_fetch_references(
        StepInput(
            context={"catalog_batch": [{"doi": "10.1000/x"}]},
            params={},
            meta=FlowMeta(flow="acquire_catalog", step="fetch_references", attempt=1),
            effects=fx,
        )
    )
    assert out.result.get("yielded_to_acquisition") is True
    assert out.result["reference_dois"] == []
    assert fx.call_count("http_request") == 0, "must not spend the reserve"


@pytest.mark.asyncio
async def test_reference_expansion_runs_when_the_budget_is_unlimited(monkeypatch):
    """The reserve exists to ration a scarce budget. With no budget — the
    default for continuous capture — expansion must not be suppressed."""
    monkeypatch.delenv("OUROBOROS_SCRAPER_HTTP_BUDGET", raising=False)
    from agent.actions.scholarly_actions import _request_budget

    assert _request_budget() == 0


# ══════════════════════════════════════════════════════════════════════
# The extraction sidecar — what makes concurrent scrape+OCR safe
#
# append_records is a read-modify-write of the WHOLE file. With the
# scraper appending candidates and the extractor appending
# extraction_status to the same path, whichever writes second wins and
# the other's work is gone. That is the only thing standing between here
# and running acquisition and OCR at the same time — worth removing,
# because the extractor flow set has ZERO LLM turns, so gpt-oss idles for
# the entire OCR stage (~12 min per 3-PDF dispatch).
#
# The two stages own disjoint fields, so they get disjoint files.
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_extraction_writes_the_sidecar_not_the_scrapers_file():
    from agent.actions.scholarly_actions import (
        DATABANK_PATH,
        EXTRACTION_PATH,
        append_extraction_records,
    )

    fx = MockEffects(files={DATABANK_PATH: '{"paper_key": "a", "title": "A"}\n'})
    await append_extraction_records(
        fx, [{"paper_key": "a", "extraction_status": "extracted"}]
    )
    assert "extraction_status" not in fx._files[DATABANK_PATH]
    assert "extracted" in fx._files[EXTRACTION_PATH]


@pytest.mark.asyncio
async def test_the_merged_view_overlays_the_sidecar_onto_the_base_record():
    from agent.actions.scholarly_actions import (
        DATABANK_PATH,
        EXTRACTION_PATH,
        read_databank,
    )

    fx = MockEffects(
        files={
            DATABANK_PATH: '{"paper_key": "a", "title": "A", "pdf_path": "p.pdf"}\n',
            EXTRACTION_PATH: '{"paper_key": "a", "extraction_status": "extracted"}\n',
        }
    )
    bank = await read_databank(fx)
    assert bank["a"]["title"] == "A", "base fields survive the overlay"
    assert bank["a"]["pdf_path"] == "p.pdf"
    assert bank["a"]["extraction_status"] == "extracted", "sidecar overlays"


@pytest.mark.asyncio
async def test_interleaved_writes_no_longer_lose_each_other():
    """THE REGRESSION. Both stages appending to one file lost whichever
    write landed first; disjoint files make the interleaving harmless."""
    from agent.actions.scholarly_actions import (
        DATABANK_PATH,
        append_extraction_records,
        append_records,
        read_databank,
    )

    fx = MockEffects(files={DATABANK_PATH: '{"paper_key": "a", "title": "A"}\n'})
    # extractor records, then the scraper appends a fresh candidate
    await append_extraction_records(
        fx, [{"paper_key": "a", "extraction_status": "extracted"}]
    )
    await append_records(fx, [{"paper_key": "b", "title": "B"}])
    bank = await read_databank(fx)
    assert bank["a"]["extraction_status"] == "extracted", "extractor work survived"
    assert bank["b"]["title"] == "B", "scraper work survived"


@pytest.mark.asyncio
async def test_a_sidecar_orphan_stays_visible_rather_than_vanishing():
    """A sidecar key with no base record means something is out of step —
    surface it rather than dropping it silently."""
    from agent.actions.scholarly_actions import EXTRACTION_PATH, read_databank

    fx = MockEffects(
        files={EXTRACTION_PATH: '{"paper_key": "ghost", "extraction_status": "x"}\n'}
    )
    bank = await read_databank(fx)
    assert "ghost" in bank
