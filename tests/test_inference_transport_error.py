"""A server that dies mid-request is a transport error RESULT, not an exception (2026-10-01).

The 3060 box's LLMVP was OOM-killed under a pack turn; httpx raised
RemoteProtocolError ("Server disconnected without sending a response"), which
the completion path did not catch. It escaped run_inference, so _curate_turn's
transport-fault path (decline the round, keep the paper) never ran and the
caller crashed. ConnectError, timeouts and HTTP status errors already came back
as results; every other httpx transport failure now does too.
"""

from __future__ import annotations

import httpx
import pytest

from agent.actions.translation_actions import _is_transport_failure
from agent.effects.inference import InferenceEffect


def _effect_raising(exc: Exception):
    fx = InferenceEffect(endpoint="http://unused")
    fx._watchdog_grace_s = 10  # the watchdog must not interfere

    class _Client:
        async def post(self, *a, **kw):
            raise exc

    async def _no_poll(query):
        return {}

    fx._poll_health = _no_poll  # type: ignore[method-assign]
    return fx, _Client()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.ReadError("connection reset by peer"),
    ],
)
async def test_a_dropped_connection_comes_back_as_an_error_result(exc):
    fx, client = _effect_raising(exc)
    result = await fx._request_once_with_watchdog(
        client, {"query": "q", "variables": {"request": {"prompt": "p"}}}
    )
    assert result.text == "" and not result.finished
    assert result.error.startswith("Transport error (")
    assert type(exc).__name__ in result.error


@pytest.mark.asyncio
async def test_translation_still_reads_it_as_a_transport_failure():
    fx, client = _effect_raising(
        httpx.RemoteProtocolError("Server disconnected without sending a response.")
    )
    result = await fx._request_once_with_watchdog(
        client, {"query": "q", "variables": {"request": {"prompt": "p"}}}
    )
    assert _is_transport_failure(result.error)
