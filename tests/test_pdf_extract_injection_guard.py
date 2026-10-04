"""Region-level INJECTION GUARD in the OCR tool's GraphQL recognizer (2026-10-04).

paddle drops short runs of CJK/Thai/Tamil/Arabic/Cyrillic into Latin prose and
paraphrases the sentence around them; 47 % of accepted Latin-script papers
carried at least one. Greedy decoding halves it, and the few deterministic
cases left are asked again, warmer — a warm redraw read every one of them
clean in the A/B (dev/bench_ocr_sampling.py).
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "pdf_extract"
)


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_DIR, file))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pytest.importorskip("PIL")
_MOD = _load("_extract_batch_injection_guard", "extract_batch.py")
L = _MOD._loops
Image = pytest.importorskip("PIL.Image")

# Live examples from the corpus and the A/B.
INJECTED = [
    "using the transmitted excitation photon beam. Quantification starts by식: (2.1)",
    "The data points in Figure 5 demonstrate the重要性 of the model.",
    "reste difficile à appliquer dans les危色 de haute densité.",
    "were collected under the theா্เสการ (Faucheais, Rat, et al.)",
    "glasses and minerals have more than 10%责 impotent concentrations",
]
CLEAN = [
    "The δ56Fe values span 0.2 ‰ and μ = 1.5 at 532 nm (Ω cm).",  # Greek is science
    "after Wang (王) and Li (李), the site was resampled.",  # quoted, set off
    "Иванов И.И. Минералогия. Москва, 1985.",  # a Russian reference IS Cyrillic
    "拉曼光谱在 532 nm 激发下采集, 样品为方解石 CaCO3。",  # a Chinese region is Chinese
    "",
]


@pytest.mark.parametrize("text", INJECTED)
def test_injection_is_found_in_latin_prose(text):
    assert L.find_injections(text)


@pytest.mark.parametrize("text", CLEAN)
def test_legitimate_scripts_are_not_injection(text):
    assert L.find_injections(text) == []


def _item():
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


GOOD = "using the transmitted excitation photon beam. Quantification starts by solving (2.1)"


def test_greedy_injection_is_redrawn_warmer_and_the_clean_draw_wins(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(_MOD, "_vision_completion", _fake([INJECTED[0], GOOD], calls))
    rec = _recognizer()
    assert rec._one(_item(), 512, 0.0) == GOOD
    assert calls == [0.0, 0.8]
    assert rec.injection_retries == 1 and rec.injection_kept == 0


def test_a_term_every_draw_repeats_stands(monkeypatch):
    """When no redraw is clean the original answer is kept, not dropped."""
    calls: list[float] = []
    answers = [INJECTED[1]] * (1 + _MOD._INJECTION_RETRIES)
    monkeypatch.setattr(_MOD, "_vision_completion", _fake(answers, calls))
    rec = _recognizer()
    assert rec._one(_item(), 512, 0.0) == INJECTED[1]
    assert len(calls) == 1 + _MOD._INJECTION_RETRIES
    assert rec.injection_retries == 0 and rec.injection_kept == 1


def test_a_clean_answer_costs_no_extra_request(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(_MOD, "_vision_completion", _fake([GOOD], calls))
    rec = _recognizer()
    assert rec._one(_item(), 512, 0.0) == GOOD
    assert calls == [0.0]


def test_a_looping_redraw_is_not_accepted(monkeypatch):
    loop = "Results. " + " ".join(["the band at the"] * 30) + " End."
    calls: list[float] = []
    monkeypatch.setattr(
        _MOD, "_vision_completion", _fake([INJECTED[0], loop, GOOD], calls)
    )
    rec = _recognizer()
    assert rec._one(_item(), 512, 0.0) == GOOD


def test_the_tool_decodes_greedy_by_default():
    import inspect

    assert (
        inspect.signature(_MOD.extract_paper).parameters["temperature"].default == 0.0
    )


def test_documents_in_that_script_are_not_judged():
    """Russian ordinals and a Japanese instrument model are the document's own
    writing; only a Latin DOCUMENT is asked about injection."""
    russian = "Гранитоиды 1-й фазы и 2-й фазы. " * 20 + "Table 1: SiO2 72.1, Al2O3 14.3"
    assert not L.latin_document(russian)
    assert L.latin_document("The band at 1085 cm-1 is assigned to the stretch. " * 20)
    assert L.latin_document("")  # a scan: no text layer to say otherwise


def test_guard_is_off_for_a_non_latin_document(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(
        _MOD, "_vision_completion", _fake(["日本分光102型 spectrometer"], calls)
    )
    rec = _recognizer()
    rec.guard_injections = False
    assert rec._one(_item(), 512, 0.0) == "日本分光102型 spectrometer"
    assert calls == [0.0]
