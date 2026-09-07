"""The fleet backend ORCHESTRATES the model instead of assuming it is hot.

LLMVP serves several models from one port with STRICT routing, so "the server
is up" is not the same question as "this model will answer". A batch that
only checked liveness would render every page, fire every region crop, and
fail on each one. `_ensure_llmvp_model` resolves it once at startup: hot ->
go, cold -> loadModel, unknown -> say so and stop, refused -> say why and stop.

loadModel is deliberately an EXPLICIT call rather than a side effect of the
first crop. That is LLMVP's own rule — a completion never loads weights,
because a multi-minute load hiding inside a request turns a timeout into a
mystery — and it is why a governor refusal arrives here as a readable
sentence instead of a stalled batch.

EVERYTHING HERE IS GRAPHQL. The preflight used to ask the REST shim's
/v1/models, which is optional (`app.openai_shim`) and absent on the remote
fleet — so the tool could not be pointed there at all. `models { name state }`
answers the same question on every fleet.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "pdf_extract",
    "extract_batch.py",
)
_spec = importlib.util.spec_from_file_location("_extract_batch_orch", _TOOL)
_MOD = importlib.util.module_from_spec(_spec)
sys.modules["_extract_batch_orch"] = _MOD
_spec.loader.exec_module(_MOD)

#: A REMOTE fleet, because that is the case the addressing bug broke.
_URL = "http://192.168.1.209:8008"


@pytest.fixture
def calls(monkeypatch):
    """Record what the orchestration asked the server for."""
    log: list = []

    def fake_models(base_url, timeout=3.0):
        log.append(("models", base_url))
        return fake_models.result

    def fake_load(base_url, model, timeout=300.0):
        log.append(("load", model))
        return fake_load.result

    fake_models.result = {}
    fake_load.result = (True, "")
    monkeypatch.setattr(_MOD, "_llmvp_models", fake_models)
    monkeypatch.setattr(_MOD, "_llmvp_load", fake_load)
    return log, fake_models, fake_load


def test_already_hot_does_not_load(calls):
    log, models, _ = calls
    models.result = {"muse-glimmer-30b": "active", "paddle-ocr-vl": "hot"}
    ok, why = _MOD._ensure_llmvp_model(_URL, "paddle-ocr-vl")
    assert (ok, why) == (True, "already hot")
    assert ("load", "paddle-ocr-vl") not in log


def test_the_active_primary_counts_as_ready(calls):
    """A campaign server runs paddle as its PRIMARY (state active, not hot)."""
    log, models, _ = calls
    models.result = {"paddle-ocr-vl": "active"}
    ok, why = _MOD._ensure_llmvp_model(_URL, "paddle-ocr-vl")
    assert (ok, why) == (True, "already hot")
    assert not [e for e in log if e[0] == "load"]


def test_unknown_model_fails_fast_without_a_load(calls):
    """A name the registry lacks is a CONFIG problem; loadModel would only
    KeyError. Say what the registry does have."""
    log, models, _ = calls
    models.result = {"qwen3-next-80b-a3": "active"}
    ok, why = _MOD._ensure_llmvp_model(_URL, "paddle-ocr-vl-mac")
    assert ok is False
    assert "unknown model 'paddle-ocr-vl-mac'" in why
    assert "qwen3-next-80b-a3" in why
    assert not [e for e in log if e[0] == "load"]


def test_cold_model_is_loaded_then_reverified(calls, monkeypatch):
    """Trust but verify: loadModel returning ok is not proof the model is
    servable — the listing is. Cold on the first look, hot on the second.
    """
    log, _, _ = calls
    seq = [{"paddle-ocr-vl": "cold"}, {"paddle-ocr-vl": "hot"}]

    def staged(base_url, timeout=3.0):
        log.append(("models", base_url))
        return seq.pop(0)

    # monkeypatch, NOT a bare attribute assignment: a direct write here
    # outlives the test and the fixture would then "restore" the stub,
    # poisoning whichever test ran next.
    monkeypatch.setattr(_MOD, "_llmvp_models", staged)
    ok, why = _MOD._ensure_llmvp_model(_URL, "paddle-ocr-vl")
    assert (ok, why) == (True, "loaded")
    assert ("load", "paddle-ocr-vl") in log
    assert seq == [], "both the pre-check and the re-verify must run"


def test_server_down_is_not_a_load_attempt(calls):
    """Nothing to ask if nothing is listening — and the message must not
    blame the model."""
    _, models, _ = calls
    models.result = None
    ok, why = _MOD._ensure_llmvp_model(_URL, "paddle-ocr-vl")
    assert ok is False
    assert why == "not reachable"


def test_governor_refusal_is_reported_verbatim(calls):
    """An over-budget refusal is a SIZING decision the operator has to read,
    not a crash to swallow."""
    _, models, load = calls
    models.result = {"paddle-ocr-vl": "cold"}
    load.result = (False, "paddle-ocr-vl needs 3.6 GB; 102.0 GB already hot")
    ok, why = _MOD._ensure_llmvp_model(_URL, "paddle-ocr-vl")
    assert ok is False
    assert "already hot" in why
    assert "loadModel refused" in why


def test_load_ok_but_still_cold_fails_loudly(calls):
    """The one case that would otherwise proceed into a batch that cannot
    work: a server that says yes and then does not serve it."""
    _, models, load = calls
    models.result = {"paddle-ocr-vl": "cold"}  # never becomes hot
    load.result = (True, "")
    ok, why = _MOD._ensure_llmvp_model(_URL, "paddle-ocr-vl")
    assert ok is False
    assert "not hot" in why


# ── addressing: the preflight must talk to the fleet it was pointed at ──
#
# The transcription calls always used OUROBOROS_LLMVP_URL in full, but the
# preflight rebuilt the address as 127.0.0.1:<port> AND asked the REST shim.
# Aiming OCR at another host therefore asked the LOCAL server whether it
# could serve paddle and, on a miss, called loadModel on it — loading the
# model onto the very GPU the remote routing exists to keep it off. Nothing
# caught it because every test above stubs these two functions; these do not.


class _Resp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _capture(monkeypatch, body: dict) -> list:
    seen: list = []

    def fake_urlopen(req, timeout=0):
        seen.append((req.full_url, json.loads(req.data.decode())))
        return _Resp(body)

    monkeypatch.setattr(_MOD.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_models_query_goes_to_the_configured_host_over_graphql(monkeypatch):
    seen = _capture(
        monkeypatch,
        {"data": {"models": [{"name": "paddle-ocr-vl", "state": "hot"}]}},
    )
    assert _MOD._llmvp_models(_URL) == {"paddle-ocr-vl": "hot"}
    assert [u for u, _ in seen] == ["http://192.168.1.209:8008/graphql"]
    assert "models" in seen[0][1]["query"]
    assert not any("127.0.0.1" in u or "/v1/" in u for u, _ in seen)


def test_load_mutation_goes_to_the_configured_host(monkeypatch):
    seen = _capture(monkeypatch, {"data": {"loadModel": {"ok": True, "detail": "hot"}}})
    ok, detail = _MOD._llmvp_load(_URL, "paddle-ocr-vl")
    assert (ok, detail) == (True, "hot")
    assert [u for u, _ in seen] == ["http://192.168.1.209:8008/graphql"]
    assert seen[0][1]["variables"] == {"n": "paddle-ocr-vl"}
    assert not any("127.0.0.1" in u for u, _ in seen)


def test_a_graphql_error_is_surfaced_not_swallowed(monkeypatch):
    """The server's message IS the verdict (\"unknown model\", \"not hot\") —
    callers branch on it, so it must arrive verbatim."""
    _capture(monkeypatch, {"data": None, "errors": [{"message": "unknown model 'x'"}]})
    with pytest.raises(RuntimeError, match="unknown model 'x'"):
        _MOD._graphql(_URL, "{ models { name } }", None, 3.0)
