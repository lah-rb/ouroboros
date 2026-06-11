"""polite_request: per-host throttling + mission request budget.

Politeness lives in the action layer, not the HTTP effect. State
persists via effects.read_state/write_state so it survives across
dispatches within a mission.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.actions import scholarly_actions
from agent.actions.scholarly_actions import _HTTP_STATE_KEY, polite_request
from agent.effects.mock import MockEffects
from agent.effects.protocol import HttpResult

_URL = "https://api.semanticscholar.org/graph/v1/paper/search"


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
async def test_state_persists_request_count():
    fx = _fx()
    await polite_request(fx, "GET", _URL)
    await polite_request(fx, "GET", _URL)
    state = await fx.read_state(_HTTP_STATE_KEY)
    assert state["total_requests"] == 2
    assert "api.semanticscholar.org" in state["hosts"]


@pytest.mark.asyncio
async def test_budget_exhaustion_fails_soft_without_calling():
    fx = _fx()
    await fx.write_state(
        _HTTP_STATE_KEY,
        {"hosts": {}, "total_requests": scholarly_actions._MISSION_REQUEST_BUDGET},
    )
    r = await polite_request(fx, "GET", _URL)
    assert r.status == 0
    assert "budget" in (r.error or "")
    assert fx.call_count("http_request") == 0


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
