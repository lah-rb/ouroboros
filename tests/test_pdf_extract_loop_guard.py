"""Region-level LOOP GUARD in the OCR tool's GraphQL recognizer (2026-09-19).

paddle's output never met the server's repetition guard; this guard asks a
looping region again once, warmer, and collapses what still loops to the
extraction marker, so no new document enters the corpus with a loop.
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


def _load_tool():
    spec = importlib.util.spec_from_file_location("_extract_batch_loop_guard", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pytest.importorskip("PIL")
_MOD = _load_tool()
Image = pytest.importorskip("PIL.Image")

LOOP = "Resultados. " + " ".join(["concluyada en la"] * 30) + " Fin."
CLEAN = "Resultados. En las muestras de la serie F se identificaron fases de alta temperatura. Fin."
TABLE = (
    "<table>" + "".join("<tr><td style='x'>—</td></tr>" for _ in range(30)) + "</table>"
)


def _item():
    # the encoder takes a numpy BGR crop or raw PNG bytes — hand it bytes
    import io

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return {"image": buf.getvalue(), "query": "OCR:"}


def _recognizer():
    return _MOD._GraphQLVisionRecognizer("http://x", "paddle-ocr-vl", 1, strict=False)


def _fake(answers: list[str], calls: list[float]):
    def fake(base_url, model, data_uri, prompt, max_tokens, temperature, strict=True):
        calls.append(temperature)
        return answers.pop(0), model

    return fake


def test_looping_region_is_asked_again_warmer_and_the_clean_answer_wins(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(_MOD, "_vision_completion", _fake([LOOP, CLEAN], calls))
    rec = _recognizer()
    out = rec._one(_item(), 512, 0.8)
    assert out == CLEAN
    assert calls == [0.8, 1.0]  # one retry, warmer, capped at 1.0
    assert rec.loop_retries == 1 and rec.loop_collapses == 0


def test_region_that_loops_twice_is_collapsed_to_the_marker(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(_MOD, "_vision_completion", _fake([LOOP, LOOP], calls))
    rec = _recognizer()
    out = rec._one(_item(), 512, 0.8)
    assert "degenerate OCR run collapsed" in out
    assert _MOD._loops.find_loops(out) == []
    assert out.startswith("Resultados.") and out.endswith("Fin.")
    assert rec.loop_collapses == 1 and len(calls) == 2


def test_clean_region_costs_one_call(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(_MOD, "_vision_completion", _fake([CLEAN], calls))
    rec = _recognizer()
    assert rec._one(_item(), 512, 0.8) == CLEAN
    assert calls == [0.8]


def test_table_markup_is_not_mistaken_for_a_loop(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(_MOD, "_vision_completion", _fake([TABLE], calls))
    rec = _recognizer()
    assert rec._one(_item(), 512, 0.8) == TABLE
    assert calls == [0.8]


def test_retry_transport_failure_falls_back_to_collapsing(monkeypatch):
    calls: list[float] = []

    def fake(base_url, model, data_uri, prompt, max_tokens, temperature, strict=True):
        calls.append(temperature)
        if len(calls) == 1:
            return LOOP, model
        raise RuntimeError("client disconnected")

    monkeypatch.setattr(_MOD, "_vision_completion", fake)
    rec = _recognizer()
    out = rec._one(_item(), 512, 0.8)
    assert "degenerate OCR run collapsed" in out and rec.loop_collapses == 1
