"""The OCR tool reaches paddle over LLMVP's GraphQL, like every other lane.

WHAT THIS PINS. paddlex's PaddleOCR-VL pipeline is two models: layout
detection (local) and a VL recognizer it holds as a plain object and uses
three ways — .predict(items, **kw), .close(), .batch_sampler.batch_size. The
tool swaps that object for one speaking `visionCompletion`. These tests fix
the request shape (the fields LLMVP's VisionCompletionRequest declares and
NOTHING else — GraphQL rejects a whole request for one undeclared field),
the result shape the assembly step indexes positionally, the byte-level image
encoding the stock client used (BGR->RGB, PNG, no resize), the strict
served-model check, and the cold-secondary warm-and-retry idiom.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import sys
import time

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "pdf_extract",
    "extract_batch.py",
)
_spec = importlib.util.spec_from_file_location("_extract_batch_gql", _TOOL)
_MOD = importlib.util.module_from_spec(_spec)
sys.modules["_extract_batch_gql"] = _MOD
_spec.loader.exec_module(_MOD)

_URL = "http://192.168.1.209:8008"
_MODEL = "paddle-ocr-vl-mac"


class _Resp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


@pytest.fixture
def server(monkeypatch):
    """A scripted fleet: records every POST, answers from `respond(vars)`."""
    state = {"seen": [], "respond": None}

    def fake_urlopen(req, timeout=0):
        body = json.loads(req.data.decode())
        state["seen"].append((req.full_url, body))
        return _Resp(state["respond"](body))

    monkeypatch.setattr(_MOD.urllib.request, "urlopen", fake_urlopen)
    return state


def _vision_ok(text: str, served: str = _MODEL) -> dict:
    return {"data": {"visionCompletion": {"text": text, "visionModel": served}}}


def _crop(b: int, g: int, r: int, w: int = 2, h: int = 2):
    """A tiny BGR crop, the shape paddlex hands the recognizer."""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :] = (b, g, r)
    return arr


# ── request shape ─────────────────────────────────────────────────────


def test_vision_request_carries_exactly_the_declared_fields(server):
    server["respond"] = lambda body: _vision_ok("hello")
    text, served = _MOD._vision_completion(
        _URL, _MODEL, "data:image/png;base64,AAAA", "OCR:", 4096, 0.8
    )
    assert (text, served) == ("hello", _MODEL)
    url, body = server["seen"][0]
    assert url == "http://192.168.1.209:8008/graphql"
    assert "visionCompletion" in body["query"]
    req = body["variables"]["request"]
    assert req == {
        "prompt": "OCR:",
        "images": [{"url": "data:image/png;base64,AAAA"}],
        "model": _MODEL,
        "maxTokens": 4096,
        "temperature": 0.8,
    }
    assert "topP" not in req and "requestId" not in req


def test_a_different_served_model_is_an_error_not_an_answer(server):
    """STRICT on both ends: LLMVP refuses a cold name; the client refuses an
    answer from anyone but the model it asked for."""
    server["respond"] = lambda body: _vision_ok("text", served="muse-glimmer-30b")
    with pytest.raises(RuntimeError, match="served by 'muse-glimmer-30b'"):
        _MOD._vision_completion(
            _URL, _MODEL, "data:image/png;base64,AAAA", "OCR:", 1, 0
        )


# ── image encoding: what paddlex's stock client sent, byte for byte ───


def test_crops_are_encoded_bgr_to_rgb_png_without_resize():
    uri = _MOD._encode_png_data_uri(_crop(255, 0, 0, w=3, h=2))  # BGR blue
    assert uri.startswith("data:image/png;base64,")
    img = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
    assert img.format == "PNG"
    assert img.size == (3, 2), "no resize — min/max_pixels were never transported"
    assert img.convert("RGB").getpixel((0, 0)) == (0, 0, 255), "BGR -> RGB"


def test_raw_png_bytes_pass_through_for_page_mode():
    png = io.BytesIO()
    Image.new("RGB", (1, 1)).save(png, format="PNG")
    uri = _MOD._encode_png_data_uri(png.getvalue())
    assert base64.b64decode(uri.split(",", 1)[1]) == png.getvalue()


# ── the recognizer paddlex sees ──────────────────────────────────────


def test_recognizer_exposes_the_three_members_the_pipeline_uses():
    rec = _MOD._GraphQLVisionRecognizer(_URL, _MODEL, 2)
    assert rec.batch_sampler.batch_size == 8192
    assert rec.close() is None
    assert callable(rec.predict)


def test_results_come_back_in_input_order_as_result_dicts(server):
    """The assembly step indexes vlm_results positionally, then writes
    ["image"] into each — so order and mutability are both load-bearing.
    Later items answer FASTER here to prove order is by input, not arrival."""

    def respond(body):
        prompt = body["variables"]["request"]["prompt"]
        time.sleep({"OCR:": 0.03, "Table Recognition:": 0.02, "Formula": 0.0}[prompt])
        return _vision_ok("T:" + prompt)

    server["respond"] = respond
    rec = _MOD._GraphQLVisionRecognizer(_URL, _MODEL, 3)
    items = [
        {"image": _crop(1, 2, 3), "query": "OCR:"},
        {"image": _crop(4, 5, 6), "query": "Table Recognition:"},
        {"image": _crop(7, 8, 9), "query": "Formula"},
    ]
    out = list(rec.predict(items, skip_special_tokens=True, use_cache=True))
    assert [o["result"] for o in out] == ["T:OCR:", "T:Table Recognition:", "T:Formula"]
    assert all(isinstance(o, dict) for o in out)
    out[0]["image"] = "assembly writes here"  # must be mutable
    assert rec.last_vision_model == _MODEL
    assert len(server["seen"]) == 3


def test_sampling_defaults_match_the_stock_client(server):
    """paddlex passes max_new_tokens=4096 by default and pins temperature to 0
    when none is given; the tool passes --vl-temperature explicitly."""
    server["respond"] = lambda body: _vision_ok("x")
    rec = _MOD._GraphQLVisionRecognizer(_URL, _MODEL, 1)
    list(rec.predict([{"image": _crop(0, 0, 0), "query": "OCR:"}]))
    req = server["seen"][-1][1]["variables"]["request"]
    assert (req["maxTokens"], req["temperature"]) == (4096, 0.0)
    list(
        rec.predict(
            [{"image": _crop(0, 0, 0), "query": "OCR:"}],
            max_new_tokens=512,
            temperature=0.8,
            top_p=0.95,
            min_pixels=1,
            max_pixels=99,
        )
    )
    req = server["seen"][-1][1]["variables"]["request"]
    assert (req["maxTokens"], req["temperature"]) == (512, 0.8)
    assert "topP" not in req and "minPixels" not in req


def test_a_cold_secondary_is_loaded_once_and_the_request_retried(server):
    """A resident secondary is cold after every server bounce; the first
    request warms it. preocr_triage's idiom: loadModel once, retry once."""
    calls = {"n": 0}

    def respond(body):
        if "loadModel" in body["query"]:
            return {"data": {"loadModel": {"ok": True, "detail": "loaded"}}}
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "data": None,
                "errors": [
                    {
                        "message": "model 'paddle-ocr-vl-mac' is a local config but "
                        "is not hot — run loadModel('paddle-ocr-vl-mac') first"
                    }
                ],
            }
        return _vision_ok("warm now")

    server["respond"] = respond
    rec = _MOD._GraphQLVisionRecognizer(_URL, _MODEL, 1)
    out = list(rec.predict([{"image": _crop(0, 0, 0), "query": "OCR:"}]))
    assert out[0]["result"] == "warm now"
    kinds = [
        "load" if "loadModel" in b["query"] else "vision" for _, b in server["seen"]
    ]
    assert kinds == ["vision", "load", "vision"]


def test_other_errors_propagate_to_the_per_page_fallback(server):
    server["respond"] = lambda body: {"data": None, "errors": [{"message": "boom"}]}
    rec = _MOD._GraphQLVisionRecognizer(_URL, _MODEL, 1)
    with pytest.raises(RuntimeError, match="boom"):
        list(rec.predict([{"image": _crop(0, 0, 0), "query": "OCR:"}]))
    assert len(server["seen"]) == 1, "no loadModel for a non-'not hot' error"


# ── page mode rides the same transport ───────────────────────────────


def test_page_mode_posts_the_page_prompt_over_graphql(server):
    server["respond"] = lambda body: _vision_ok("page text")
    png = io.BytesIO()
    Image.new("RGB", (1, 1)).save(png, format="PNG")
    text = _MOD._page_transcribe(png.getvalue(), 0.8, 0.95, base_url=_URL, model=_MODEL)
    assert text == "page text"
    url, body = server["seen"][0]
    assert url.endswith("/graphql") and "/v1/" not in url
    req = body["variables"]["request"]
    assert req["prompt"] == _MOD._PAGE_PROMPT
    assert req["maxTokens"] == _MOD._PAGE_MODE_MAX_TOKENS
    assert req["model"] == _MODEL
    assert "topP" not in req
