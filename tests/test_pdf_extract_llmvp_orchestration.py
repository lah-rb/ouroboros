"""The fleet backend ORCHESTRATES the model instead of assuming it is hot.

LLMVP serves several models from one port with STRICT routing, so "the server
is up" is not the same question as "this model will answer". A batch that
only checked liveness would render every page, fire every region crop, and
fail on each one. `_ensure_llmvp_model` resolves it once at startup: hot ->
go, cold -> loadModel, refused -> say why and stop.

loadModel is deliberately an EXPLICIT call rather than a side effect of the
first crop. That is LLMVP's own rule — a completion never loads weights,
because a multi-minute load hiding inside a request turns a timeout into a
mystery — and it is why a governor refusal arrives here as a readable
sentence instead of a stalled batch.
"""

from __future__ import annotations

import importlib.util
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


@pytest.fixture
def calls(monkeypatch):
    """Record what the orchestration asked the server for."""
    log: list = []

    def fake_models(port, timeout=3.0):
        log.append(("models", port))
        return fake_models.result

    def fake_load(port, model, timeout=300.0):
        log.append(("load", model))
        return fake_load.result

    fake_models.result = set()
    fake_load.result = (True, "")
    monkeypatch.setattr(_MOD, "_llmvp_models", fake_models)
    monkeypatch.setattr(_MOD, "_llmvp_load", fake_load)
    return log, fake_models, fake_load


def test_already_hot_does_not_load(calls):
    log, models, _ = calls
    models.result = {"muse-glimmer-30b", "paddle-ocr-vl"}
    ok, why = _MOD._ensure_llmvp_model(8008, "paddle-ocr-vl")
    assert (ok, why) == (True, "already hot")
    assert ("load", "paddle-ocr-vl") not in log


def test_cold_model_is_loaded_then_reverified(calls, monkeypatch):
    """Trust but verify: loadModel returning ok is not proof the model is
    servable — the listing is. Cold on the first look, listed on the second.
    """
    log, _, _ = calls
    seq = [set(), {"paddle-ocr-vl"}]

    def staged(port, timeout=3.0):
        log.append(("models", port))
        return seq.pop(0)

    # monkeypatch, NOT a bare attribute assignment: a direct write here
    # outlives the test and the fixture would then "restore" the stub,
    # poisoning whichever test ran next.
    monkeypatch.setattr(_MOD, "_llmvp_models", staged)
    ok, why = _MOD._ensure_llmvp_model(8008, "paddle-ocr-vl")
    assert (ok, why) == (True, "loaded")
    assert ("load", "paddle-ocr-vl") in log
    assert seq == [], "both the pre-check and the re-verify must run"


def test_server_down_is_not_a_load_attempt(calls):
    """Nothing to ask if nothing is listening — and the message must not
    blame the model."""
    _, models, _ = calls
    models.result = None
    ok, why = _MOD._ensure_llmvp_model(8008, "paddle-ocr-vl")
    assert ok is False
    assert why == "not reachable"


def test_governor_refusal_is_reported_verbatim(calls):
    """An over-budget refusal is a SIZING decision the operator has to read,
    not a crash to swallow."""
    _, models, load = calls
    models.result = set()
    load.result = (False, "paddle-ocr-vl needs 3.6 GB; 102.0 GB already hot")
    ok, why = _MOD._ensure_llmvp_model(8008, "paddle-ocr-vl")
    assert ok is False
    assert "already hot" in why
    assert "loadModel refused" in why


def test_load_ok_but_still_unlisted_fails_loudly(calls):
    """The one case that would otherwise proceed into a batch that cannot
    work: a server that says yes and then does not serve it."""
    _, models, load = calls
    models.result = set()  # never becomes listed
    load.result = (True, "")
    ok, why = _MOD._ensure_llmvp_model(8008, "paddle-ocr-vl")
    assert ok is False
    assert "not listed" in why
