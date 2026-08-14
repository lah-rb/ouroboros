"""The five OA-acquisition repairs, and the guards that keep them cheap.

Sized from live measurement on 2026-08-13 (dev/SCRAPER_BACKLOG.md):
`oa_unresolved` splits 51% hard_wall / 41% landing_page, and a plain re-fetch
weeks later recovers 35% of the landing_page pile (Springer 6/6) against 5% of
the walled one. That asymmetry is what every test below is protecting — the
cheap repair must run first and the expensive one must be tightly bounded.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.actions import scholarly_actions as SA
from agent.actions.research_plan_actions import _oa_first, _stale_retry_keys


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _unresolved(**kw) -> dict:
    rec = {
        "paper_key": kw.pop("key", "p1"),
        "access_status": "oa_unresolved",
        "oa_attempted": ["https://pub.example/a.pdf"],
        "failure_reason": "response is text/html, not a document",
        "updated_at": _iso(30),
    }
    rec.update(kw)
    return rec


# ── classify_failure: the vocabulary everything else keys off ─────────


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("HTTP 403", "hard_wall"),
        ("HTTP 403 (tried 1 location(s))", "hard_wall"),
        ("response is text/html, not a document", "landing_page"),
        ("response is not a PDF (magic b'\\xff\\xd8')", "wrong_asset"),
        ("HTTP 202", "async_pending"),
        ("HTTP 404", "gone"),
        ("", "other"),
    ],
)
def test_classify_failure(reason, expected):
    assert SA.classify_failure(reason) == expected


def test_403_wins_over_html():
    """A walled response is ALSO served as text/html. If html were checked
    first, half the hard_wall pile would be misfiled as tractable and the
    nav lane would burn inference on 403s."""
    assert SA.classify_failure("HTTP 403 text/html") == "hard_wall"


# ── item 1: stale re-arm ──────────────────────────────────────────────


def test_fresh_failure_is_not_retried(monkeypatch):
    """Immediate retry was measured to yield nothing — only age makes it work."""
    monkeypatch.delenv("OUROBOROS_OA_RETRY_AFTER_DAYS", raising=False)
    assert SA.is_stale_retry_candidate(_unresolved(updated_at=_iso(0.2))) is False


def test_old_failure_is_retried(monkeypatch):
    monkeypatch.delenv("OUROBOROS_OA_RETRY_AFTER_DAYS", raising=False)
    assert SA.is_stale_retry_candidate(_unresolved(updated_at=_iso(30))) is True


def test_recent_rearm_blocks_another(monkeypatch):
    """oa_retried_at is what stops the same handful refilling the batch
    reservation forever, which would make the corpus goal uncompletable."""
    monkeypatch.delenv("OUROBOROS_OA_RETRY_AFTER_DAYS", raising=False)
    rec = _unresolved(updated_at=_iso(30), oa_retried_at=_iso(0.2))
    assert SA.is_stale_retry_candidate(rec) is False


def test_hard_wall_is_never_re_armed(monkeypatch):
    """5% yield does not deserve a slot the 35% bucket could use."""
    monkeypatch.delenv("OUROBOROS_OA_RETRY_AFTER_DAYS", raising=False)
    assert SA.is_stale_retry_candidate(_unresolved(failure_reason="HTTP 403")) is False


def test_zero_days_disables_the_lane(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OA_RETRY_AFTER_DAYS", "0")
    assert SA.is_stale_retry_candidate(_unresolved()) is False


def test_horizon_accepts_fractions(monkeypatch):
    """Integer days made the knob unusable on the corpus it was built for: a
    live workspace is re-worked daily, so its whole retryable population sits
    under 24h old and an int floor of 1 could only ever fire on a workspace
    left alone."""
    monkeypatch.setenv("OUROBOROS_OA_RETRY_AFTER_DAYS", "0.5")
    assert SA.retry_after_days() == 0.5
    rec = _unresolved(updated_at=_iso(0.7))  # 0.7d old: stale at 0.5, fresh at 1
    assert SA.is_stale_retry_candidate(rec) is True
    monkeypatch.setenv("OUROBOROS_OA_RETRY_AFTER_DAYS", "1")
    assert SA.is_stale_retry_candidate(rec) is False


def test_garbled_horizon_falls_back(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OA_RETRY_AFTER_DAYS", "soon")
    assert SA.retry_after_days() == SA._RETRY_AFTER_DAYS_DEFAULT


# ── backoff: a flat cadence never gives up ────────────────────────────


def test_backoff_doubles_then_stops(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OA_RETRY_AFTER_DAYS", "1")
    assert [SA.retry_backoff_days(n) for n in range(SA._MAX_OA_RETRIES)] == [
        1.0,
        2.0,
        4.0,
        8.0,
    ]
    assert SA.retry_backoff_days(SA._MAX_OA_RETRIES) == float("inf")
    assert SA.retry_backoff_days(99) == float("inf")


def test_backoff_scales_with_the_base(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OA_RETRY_AFTER_DAYS", "0.5")
    assert SA.retry_backoff_days(0) == 0.5
    assert SA.retry_backoff_days(2) == 2.0


def test_disabled_lane_backs_off_to_never(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OA_RETRY_AFTER_DAYS", "0")
    assert SA.retry_backoff_days(0) == float("inf")


def test_exhausted_record_is_never_re_armed_again(monkeypatch):
    """THE POINT OF THE EXPANSION. The 2026-08-13 run re-armed 26 records and
    recovered none; under a flat horizon those same 26 return every cycle,
    fail again, and crowd out records that have not had a turn."""
    monkeypatch.delenv("OUROBOROS_OA_RETRY_AFTER_DAYS", raising=False)
    spent = _unresolved(
        updated_at=_iso(400), oa_retried_at=_iso(400), oa_retry_count=SA._MAX_OA_RETRIES
    )
    assert SA.is_stale_retry_candidate(spent) is False


def test_a_second_attempt_waits_longer_than_the_first(monkeypatch):
    monkeypatch.setenv("OUROBOROS_OA_RETRY_AFTER_DAYS", "1")
    # Re-armed 1.5 days ago: fine for attempt 0 (wait 1d), too soon after
    # attempt 1 (wait 2d).
    rec = _unresolved(updated_at=_iso(30), oa_retried_at=_iso(1.5))
    assert SA.is_stale_retry_candidate({**rec, "oa_retry_count": 0}) is True
    assert SA.is_stale_retry_candidate({**rec, "oa_retry_count": 1}) is False


def test_undated_record_is_NOT_re_armed(monkeypatch):
    """Unknown age must not read as stale.

    Every real write stamps `updated_at`, so a record without one is a
    fixture or a hand-made row. Treating unknown as old re-armed papers whose
    locations were deliberately burned — caught by
    `test_exhausted_locations_are_not_retried_forever`, which is the older and
    more important invariant. Staleness has to be positively established.
    """
    monkeypatch.delenv("OUROBOROS_OA_RETRY_AFTER_DAYS", raising=False)
    assert SA.is_stale_retry_candidate(_unresolved(updated_at="")) is False
    assert SA.is_stale_retry_candidate(_unresolved(updated_at="not-a-date")) is False


def test_never_re_armed_record_IS_eligible(monkeypatch):
    """The opposite default, on the other field: a missing `oa_retried_at`
    means nothing has ever re-armed this paper, so a first attempt is due."""
    monkeypatch.delenv("OUROBOROS_OA_RETRY_AFTER_DAYS", raising=False)
    rec = _unresolved(updated_at=_iso(30))
    rec.pop("oa_retried_at", None)
    assert SA.is_stale_retry_candidate(rec) is True


def test_stale_keys_sort_best_yield_first(monkeypatch):
    monkeypatch.delenv("OUROBOROS_OA_RETRY_AFTER_DAYS", raising=False)
    db = {
        "async": _unresolved(key="async", failure_reason="HTTP 202"),
        "land": _unresolved(key="land"),
        "walled": _unresolved(key="walled", failure_reason="HTTP 403"),
    }
    keys = _stale_retry_keys(db)
    assert keys[0] == "land", "landing_page (35%) must precede async (unmeasured)"
    assert "walled" not in keys


# ── item 2: OA-likely ordering ────────────────────────────────────────


def test_oa_first_puts_hopeless_last():
    items = [
        ("hopeless", {"openalex_id": "W1", "oa_pdf_urls": []}),
        ("likely", {"openalex_id": "W2", "oa_pdf_urls": ["https://x/y.pdf"]}),
    ]
    assert _oa_first(items) == ["likely", "hopeless"]


def test_record_never_seen_by_openalex_is_not_hopeless():
    """THE LOAD-BEARING GUARD. S2- and CORE-sourced records also carry no
    oa_pdf_urls; treating them as hopeless would deprioritise — and under the
    opt-in skip, silently close — papers nobody ever checked."""
    assert SA._openalex_found_nothing({"oa_pdf_urls": []}) is False
    assert SA._openalex_found_nothing({"openalex_id": "W1", "oa_pdf_urls": []}) is True


def test_hopeless_skip_is_off_by_default(monkeypatch):
    monkeypatch.delenv("OUROBOROS_SKIP_HOPELESS_UNPAYWALL", raising=False)
    assert SA._skip_hopeless() is False
    monkeypatch.setenv("OUROBOROS_SKIP_HOPELESS_UNPAYWALL", "1")
    assert SA._skip_hopeless() is True


# ── item 3: alt-host tier ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,walled",
    [
        ("https://www.sciencedirect.com/x.pdf", True),
        ("https://agupubs.onlinelibrary.wiley.com/doi/pdf/10.1/x", True),
        ("https://www.mdpi.com/1/2/3/pdf", True),
        ("https://core.ac.uk/download/1.pdf", False),
        ("https://repository.university.edu/a.pdf", False),
    ],
)
def test_walled_host_detection(url, walled):
    assert SA._is_walled(url) is walled


def test_springer_is_NOT_walled():
    """It was on the list and should not have been.

    It got there from a table of failure COUNTS (93 failures) without asking
    whether those failures were durable. They were transient: Springer serves
    a PDF on plain re-fetch at 4/6, 9/9 and 4/4 across three samples — the
    best-recovering host in the corpus. Counting failures is not measuring
    refusal, and listing it here silently excluded the one publisher worth
    retrying from both the re-arm queue and the alt-host tier.
    """
    assert SA._is_walled("https://link.springer.com/content/pdf/10.1/x.pdf") is False


def test_doi_org_is_walled():
    """doi.org resolves to a meta-refresh stub that forwards into the
    publisher (measured: linkinghub.elsevier.com -> sciencedirect), so an
    unresolved record still pointing at it is a wall one hop away."""
    assert SA._is_walled("https://doi.org/10.1016/j.sab.2024.107003") is True


def test_walled_host_record_is_not_re_armed(monkeypatch):
    """HOST OVER BUCKET — the fix for a live 0/6. Bucket-only ordering drew
    six Wiley DOIs and recovered nothing; Wiley re-fetches at 0/6 where
    Springer runs 4/6."""
    monkeypatch.delenv("OUROBOROS_OA_RETRY_AFTER_DAYS", raising=False)
    wiley = _unresolved(oa_pdf_url="https://onlinelibrary.wiley.com/doi/pdf/10.1/x")
    springer = _unresolved(oa_pdf_url="https://link.springer.com/content/pdf/10.1/x")
    assert SA.is_stale_retry_candidate(wiley) is False
    assert SA.is_stale_retry_candidate(springer) is True


# ── item 4: LLM navigation, and its blast radius ──────────────────────


def test_link_extraction_reduces_the_page():
    """A publisher page is hundreds of KB of chrome; only plausible links may
    reach the prompt."""
    html = """
      <a href="/nav/home">Home</a>
      <a href="/doi/pdf/10.1/x">Download PDF</a>
      <a href="#cite">Cite</a>
      <a href="javascript:void(0)">Menu</a>
      <a href="/suppl/data.pdf">Supplementary PDF</a>
    """
    links = SA._extract_links(html, "https://pub.example/doi/10.1/x")
    urls = [u for u, _ in links]
    assert "https://pub.example/doi/pdf/10.1/x" in urls
    assert "https://pub.example/nav/home" not in urls
    assert not any("javascript" in u for u in urls)


def test_link_extraction_is_capped():
    html = "".join(f'<a href="/p{i}.pdf">PDF {i}</a>' for i in range(200))
    assert len(SA._extract_links(html, "https://x.example/")) <= SA._NAV_MAX_CANDIDATES


@pytest.mark.parametrize(
    "candidate,ok",
    [
        ("https://pub.example/doi/pdf/1", True),
        ("https://sub.pub.example/pdf/1", True),
        ("https://core.ac.uk/download/1.pdf", True),
        ("https://evil.example/steal", False),
        ("file:///etc/passwd", False),
        ("javascript:alert(1)", False),
        ("http://169.254.169.254/latest/meta-data/", False),
    ],
)
def test_model_cannot_send_us_anywhere(candidate, ok):
    """The model's output is free text and the page it read is untrusted — a
    prompt-injected landing page must not be able to point the crawler at an
    arbitrary host."""
    assert SA._nav_url_is_allowed(candidate, "https://pub.example/doi/10.1/x") is ok


class _FakeEffects:
    def __init__(self, page_text="", answer="", download_ok=False):
        self.page_text = page_text
        self.answer = answer
        self.download_ok = download_ok
        self.inference_calls = 0
        self.downloads: list = []

    async def http_request(self, method, url, **kw):
        return SimpleNamespace(status=200, url=url, text=self.page_text, json_data=None)

    async def run_inference(self, prompt, cfg=None):
        self.inference_calls += 1
        return SimpleNamespace(text=self.answer, error=None)

    async def http_download(self, url, path, **kw):
        self.downloads.append(url)
        return SimpleNamespace(
            success=self.download_ok, url=url, path=path, status=200, error=None
        )

    async def read_state(self, *a, **k):
        return {}

    async def write_state(self, *a, **k):
        return None


def _run_nav(records, effects, monkeypatch, enabled="1"):
    # The feature is OFF by default (30 live attempts, 0 recoveries), so a
    # test of its behaviour has to switch it on deliberately. setdefault, not
    # a hard set: the kill-switch test passes enabled="0" and must win.
    monkeypatch.setenv("OUROBOROS_LLM_NAV", enabled)

    # polite_request is a MODULE function taking effects first — not the
    # effect's own bound method. Patching it with the bound method silently
    # shifts every argument by one.
    async def _fake_polite(fx, method, url, **kw):
        return await fx.http_request(method, url, **kw)

    monkeypatch.setattr(SA, "polite_request", _fake_polite)
    si = SimpleNamespace(
        effects=effects,
        context={"catalog_batch": records},
        params={},
        inputs={},
        model_copy=lambda **kw: si,
    )
    return asyncio.run(SA.action_navigate_landing_page(si))


PAGE = '<a href="/doi/pdf/10.1/x">Download PDF</a>'


def test_nav_skips_hard_walled_records(monkeypatch):
    """No content came back for these — there is nothing to reason about, and
    spending inference on them is the failure this whole design avoids."""
    fx = _FakeEffects(PAGE, "https://pub.example/doi/pdf/10.1/x", True)
    recs = [_unresolved(failure_reason="HTTP 403", oa_pdf_url="https://pub.example/a")]
    out = _run_nav(recs, fx, monkeypatch)
    assert out.result["attempted"] == 0
    assert fx.inference_calls == 0


def test_nav_respects_the_per_dispatch_cap(monkeypatch):
    fx = _FakeEffects(PAGE, "NONE")
    recs = [
        _unresolved(key=f"p{i}", oa_pdf_url="https://pub.example/a") for i in range(5)
    ]
    out = _run_nav(recs, fx, monkeypatch)
    assert out.result["attempted"] == SA._NAV_PER_DISPATCH
    assert fx.inference_calls == SA._NAV_PER_DISPATCH


def test_nav_kill_switch(monkeypatch):
    fx = _FakeEffects(PAGE, "https://pub.example/doi/pdf/10.1/x", True)
    out = _run_nav(
        [_unresolved(oa_pdf_url="https://pub.example/a")], fx, monkeypatch, enabled="0"
    )
    assert out.result["attempted"] == 0


def test_nav_is_OFF_unless_explicitly_enabled(monkeypatch):
    """The default flipped to off after 30 live attempts and 0 recoveries.

    Re-measured through the production download path (n=30 unresolved): 23%
    recover outright with no inference at all (Springer 7/7), 40% are hard
    walls, and 6 of the 8 remaining `landing_page` records are doi.org
    meta-refresh stubs forwarding INTO a wall. Nothing in that sample was the
    case this action exists for. Flipping the default back is a decision that
    should follow a fresh measurement, so it is pinned here.
    """
    monkeypatch.delenv("OUROBOROS_LLM_NAV", raising=False)

    async def _fake_polite(fx, method, url, **kw):
        return await fx.http_request(method, url, **kw)

    monkeypatch.setattr(SA, "polite_request", _fake_polite)
    fx = _FakeEffects(PAGE, "https://pub.example/doi/pdf/10.1/x", True)
    si = SimpleNamespace(
        effects=fx,
        context={"catalog_batch": [_unresolved(oa_pdf_url="https://pub.example/a")]},
        params={},
        inputs={},
    )
    si.model_copy = lambda **kw: si
    out = asyncio.run(SA.action_navigate_landing_page(si))
    assert out.result["attempted"] == 0
    assert fx.inference_calls == 0
    assert fx.inference_calls == 0


def test_nav_records_one_attempt_ever(monkeypatch):
    """No loops: a record that has been navigated is never navigated again,
    however many sweeps pass over it."""
    fx = _FakeEffects(PAGE, "NONE")
    rec = _unresolved(oa_pdf_url="https://pub.example/a")
    _run_nav([rec], fx, monkeypatch)
    assert rec.get("nav_attempted_at")
    fx2 = _FakeEffects(PAGE, "NONE")
    out = _run_nav([rec], fx2, monkeypatch)
    assert out.result["attempted"] == 0
    assert fx2.inference_calls == 0


def test_nav_success_marks_provenance(monkeypatch):
    fx = _FakeEffects(PAGE, "https://pub.example/doi/pdf/10.1/x", download_ok=True)
    rec = _unresolved(oa_pdf_url="https://pub.example/doi/10.1/x")
    out = _run_nav([rec], fx, monkeypatch)
    assert out.result["navigated"] == 1
    assert rec["access_status"] == "oa_pdf"
    assert rec["retrieval_method"] == "llm_nav"
    assert rec["pdf_path"].endswith(".pdf")


def test_nav_rejects_offdomain_before_fetching(monkeypatch):
    """The rejection must happen BEFORE the download — otherwise the guard is
    decoration and the request has already gone out."""
    fx = _FakeEffects(PAGE, "https://evil.example/x.pdf", download_ok=True)
    rec = _unresolved(oa_pdf_url="https://pub.example/doi/10.1/x")
    out = _run_nav([rec], fx, monkeypatch)
    assert out.result["navigated"] == 0
    assert fx.downloads == []
    assert "off-domain" in rec["failure_reason"]


def test_nav_handles_none_answer(monkeypatch):
    fx = _FakeEffects(PAGE, "NONE")
    rec = _unresolved(oa_pdf_url="https://pub.example/doi/10.1/x")
    out = _run_nav([rec], fx, monkeypatch)
    assert out.result["navigated"] == 0
    assert fx.downloads == []


# ── item 5: discovery OA filter ───────────────────────────────────────


def test_oa_filter_pairs_is_oa_with_fulltext():
    """is_oa alone admits bronze OA (free to read, no PDF url), which is most
    of what it would keep — the pairing is what moved 22% -> 92%."""
    assert "is_oa:true" in SA._OPENALEX_OA_FILTER
    assert "has_fulltext:true" in SA._OPENALEX_OA_FILTER
