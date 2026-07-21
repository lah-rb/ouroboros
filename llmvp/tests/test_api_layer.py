"""API-layer units (graphql + rest) — first coverage for a 0% layer
(TESTING.md roadmap): resolver shaping and error mapping against fakes,
no model, no server.

Pinned behaviors:
  - resolve_max_tokens / resolve_temperature canonical chains (the
    explicit-0.0 temperature survival is a documented past bug).
  - The GraphQL completion resolver's ``truncated`` derivation
    (tokens_generated >= effective max) — the flag the swarm worker
    ramble-detection and the agent's InferenceResult.truncated depend on.
  - REST /completions response shaping + HTTP error mapping
    (ValueError -> 400, RuntimeError -> 500, non-string prompt -> 400).
  - REST _unwrap_code_fence forgiveness for fenced chat replies.
"""

from __future__ import annotations

import pytest

from core.inference import (
    CompletionOutcome,
    resolve_max_tokens,
    resolve_temperature,
)


def _outcome(text="hi", tokens=5, **kw) -> CompletionOutcome:
    return CompletionOutcome(text=text, tokens_generated=tokens, **kw)


# ── canonical request→config→floor chains ─────────────────────────────


def test_resolve_max_tokens_chain(installed_config):
    installed_config.generation.max_tokens_default = 1234
    assert resolve_max_tokens(77) == 77  # explicit wins
    assert resolve_max_tokens(None) == 1234  # config default
    installed_config.generation.max_tokens_default = 0
    assert resolve_max_tokens(None) == 256  # hard floor


def test_resolve_temperature_explicit_zero_survives(installed_config):
    # 0.0 means greedy and must not be swallowed by `or`-chaining —
    # BUT the GLOBAL temperature_floor (distinct from the session-depth
    # session_temp_floor) still applies to any request.
    installed_config.generation.temperature_floor = None
    assert resolve_temperature(0.0) == 0.0
    installed_config.generation.temperature_floor = 0.35
    assert resolve_temperature(0.0) == 0.35  # global floor engages
    assert resolve_temperature(0.9) == 0.9  # above floor untouched
    installed_config.generation.temperature_default = 0.6
    assert resolve_temperature(None) == 0.6  # default when unspecified


# ── GraphQL completion resolver: truncated derivation ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tokens,max_tokens,expect_truncated",
    [
        (100, 100, True),  # hit the ceiling exactly → truncated
        (150, 100, True),  # over (server counted past) → truncated
        (99, 100, False),  # ended on EOS under the ceiling
    ],
)
async def test_completion_truncated_derivation(
    installed_config, monkeypatch, tokens, max_tokens, expect_truncated
):
    import api.graphql_api as gql

    async def fake_run_completion(**kwargs):
        return _outcome(tokens=tokens, generated_tokens=tokens)

    async def no_remote(request):
        return None

    monkeypatch.setattr(gql, "run_completion", fake_run_completion)
    monkeypatch.setattr(gql, "_serve_remote_if_routed", no_remote)

    resp = await gql.Query().completion(
        request=gql.CompletionRequest(prompt="p", max_tokens=max_tokens)
    )
    assert resp.truncated is expect_truncated
    assert resp.tokens_generated == tokens


@pytest.mark.asyncio
async def test_completion_truncated_uses_config_default_ceiling(
    installed_config, monkeypatch
):
    # No explicit max_tokens: the effective ceiling is the config default,
    # so a generation that hits IT must still be flagged.
    import api.graphql_api as gql

    installed_config.generation.max_tokens_default = 64

    async def fake_run_completion(**kwargs):
        return _outcome(tokens=64, generated_tokens=64)

    async def no_remote(request):
        return None

    monkeypatch.setattr(gql, "run_completion", fake_run_completion)
    monkeypatch.setattr(gql, "_serve_remote_if_routed", no_remote)

    resp = await gql.Query().completion(request=gql.CompletionRequest(prompt="p"))
    assert resp.truncated is True


# ── REST: response shaping + error mapping ────────────────────────────


def _rest_client(monkeypatch, run_completion):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.rest_api as rest

    monkeypatch.setattr(rest, "run_completion", run_completion)
    app = FastAPI()
    app.include_router(rest.router)
    return TestClient(app, raise_server_exceptions=False)


def test_rest_completions_shapes_choices(installed_config, monkeypatch):
    async def ok(**kwargs):
        return _outcome(text="hello world")

    client = _rest_client(monkeypatch, ok)
    r = client.post("/completions", json={"prompt": "hi", "stream": False})
    assert r.status_code == 200
    assert r.json() == {"choices": [{"text": "hello world"}]}


def test_rest_completions_error_mapping(installed_config, monkeypatch):
    async def bad_value(**kwargs):
        raise ValueError("prompt too long")

    client = _rest_client(monkeypatch, bad_value)
    r = client.post("/completions", json={"prompt": "hi", "stream": False})
    assert r.status_code == 400
    assert "prompt too long" in r.json()["detail"]

    async def broken(**kwargs):
        raise RuntimeError("engine wedged")

    client = _rest_client(monkeypatch, broken)
    r = client.post("/completions", json={"prompt": "hi", "stream": False})
    assert r.status_code == 500


def test_rest_completions_rejects_non_string_prompt(installed_config, monkeypatch):
    async def ok(**kwargs):
        return _outcome()

    client = _rest_client(monkeypatch, ok)
    r = client.post("/completions", json={"prompt": 42, "stream": False})
    assert r.status_code == 400


def test_rest_unwrap_code_fence():
    from api.rest_api import _unwrap_code_fence

    assert _unwrap_code_fence("```python\nx = 1\n```") == "x = 1"
    assert _unwrap_code_fence("```\nplain\n```") == "plain"
    assert _unwrap_code_fence("no fence here") == "no fence here"
