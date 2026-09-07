"""Per-domain inference routing.

One mission is one PROCESS — `drain_lane.ClaimSet` is in-process only, so a
second agent would claim the same papers twice. Routing a single lane's
inference to another host is how a remote server assists without splitting
the process: the tokens move, the claims and gates do not.

These tests pin the three things that would silently break that:
routing happens at all, an unmapped domain still reaches the default server,
and `domain` is CONSUMED rather than forwarded to the model.
"""

from __future__ import annotations

from agent.effects.local import LocalEffects

MAC = "http://192.168.1.209:8008/graphql"


def _fx(tmp_path, domains=None):
    return LocalEffects(
        working_directory=str(tmp_path),
        llmvp_endpoint="http://localhost:8008/graphql",
        llmvp_domains=domains,
    )


def test_a_mapped_domain_routes_to_its_own_endpoint_and_model(tmp_path):
    fx = _fx(tmp_path, {"curate": {"endpoint": MAC, "model": "muse-glimmer-30b-swarm"}})
    client = fx._get_inference("curate")
    assert client._endpoint == MAC
    # The registry name is HOST-LOCAL: the same weights are -cuda here and
    # -swarm on the mac, so a domain that moves hosts must carry its model.
    assert client._model == "muse-glimmer-30b-swarm"


def test_an_unmapped_domain_falls_back_to_the_mission_endpoint(tmp_path):
    """A typo'd or new domain must degrade to local, never fail the round."""
    fx = _fx(tmp_path, {"curate": {"endpoint": MAC}})
    assert fx._get_inference("figtext")._endpoint == "http://localhost:8008/graphql"
    assert fx._get_inference("")._endpoint == "http://localhost:8008/graphql"
    assert fx._get_inference("curat")._endpoint == "http://localhost:8008/graphql"


def test_no_domain_map_is_the_previous_behaviour(tmp_path):
    fx = _fx(tmp_path, None)
    assert fx._get_inference("curate") is fx._get_inference()
    assert fx._get_inference()._endpoint == "http://localhost:8008/graphql"


def test_one_client_per_endpoint_not_per_domain(tmp_path):
    """Two domains on one host must SHARE a client — a client owns a health
    watchdog, and one per domain would double-poll the same server."""
    fx = _fx(
        tmp_path,
        {
            "curate": {"endpoint": MAC, "model": "m"},
            "ocr": {"endpoint": MAC, "model": "m"},
        },
    )
    assert fx._get_inference("curate") is fx._get_inference("ocr")


def test_domain_is_consumed_and_never_sent_to_the_model(tmp_path, monkeypatch):
    """`domain` selects a SERVER. If it leaked into config_overrides it would
    ride along as a model parameter, and the caller's dict would be mutated
    under them."""
    import asyncio

    fx = _fx(tmp_path, {"curate": {"endpoint": MAC}})
    seen = {}

    class _Stub:
        _endpoint = MAC

        async def run_inference(self, prompt, config_overrides=None, **kw):
            seen["overrides"] = config_overrides

            class R:
                error = None
                text = "ok"
                tokens_generated = 1

            return R()

    monkeypatch.setattr(fx, "_get_inference", lambda domain="": _Stub())
    monkeypatch.setattr(fx, "_maybe_sample_server_health", lambda: _noop())

    async def _noop():
        return None

    caller_dict = {"max_tokens": 10, "domain": "curate"}
    asyncio.run(fx.run_inference("hi", caller_dict))

    assert "domain" not in seen["overrides"], "domain leaked to the model call"
    assert seen["overrides"]["max_tokens"] == 10
    assert caller_dict["domain"] == "curate", "caller's dict was mutated"
