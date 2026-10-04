"""LLMVP reports the temperature it ACTUALLY sampled at.

LLMVP owns the model's parameters: a client asks for a temperature, and the
server may raise it to the model's `temperature_floor`, raise it again on a
deep session turn, or re-drive a degenerate turn at the recovery recipe's
temperature. The agent's history recorded 0.0 for every turn that asked for a
relative temperature ("t*0.1" does not parse as a number) while this server
was flooring them at 0.7 (tier_20260924-191710). The pool loop now stashes
the final sampler temperature beside its other per-request telemetry, and the
completion response carries it back.
"""

from __future__ import annotations

import pytest

from conftest import FakeGenLlama, make_pool_backend


@pytest.fixture
def eog(monkeypatch):
    import llama_cpp

    monkeypatch.setattr(llama_cpp, "llama_token_is_eog", lambda vocab, t: t == 0)


def _run(backend, inst, temperature, **kw):
    return list(
        backend.generate_stream_sync(
            inst, [5, 6], 10, temperature, static_in_prompt=False, **kw
        )
    )


def test_the_pool_loop_stashes_the_temperature_it_sampled_at(eog):
    backend = make_pool_backend()
    inst = FakeGenLlama([7, 0])
    _run(backend, inst, 0.7)
    assert inst._last_temperature == 0.7


def test_a_sampling_override_is_what_gets_reported(eog):
    """The degenerate-retry recipe re-drives a turn through sampling_overrides;
    the response must report the override, not the requested value."""
    backend = make_pool_backend()
    inst = FakeGenLlama([7, 0])
    _run(backend, inst, 0.3, sampling_overrides={"temp": 1.0})
    assert inst._last_temperature == 1.0


def test_a_failed_request_does_not_inherit_the_previous_temperature(eog):
    backend = make_pool_backend()
    inst = FakeGenLlama([7, 0])
    _run(backend, inst, 0.9)
    inst._last_temperature = 0.9
    # The reset runs before anything can raise, so a request that dies at its
    # context guard leaves None, not the previous request's value.
    inst.n_tokens = 10**9
    with pytest.raises(Exception):
        _run(backend, inst, 0.2)
    assert inst._last_temperature is None


def test_the_completion_response_carries_the_temperature():
    from api.graphql_api import CompletionResponse

    r = CompletionResponse(text="x", tokens_generated=1, temperature=0.7)
    assert r.temperature == 0.7
    assert CompletionResponse(text="x", tokens_generated=1).temperature is None
