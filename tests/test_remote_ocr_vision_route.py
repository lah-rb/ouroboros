"""The ocr lane routed to another box (2026-09-29: the 3060 at 192.168.1.76).

Vision calls follow the lane's domain the way text calls always did; the pre-OCR
triage reads the page on the lane's paddle and asks the DEFAULT server for the
verdict; a server on another host gets the image inline, since it cannot open
this host's paths. Before this, the routed lane's vision call fell through to
the local server and would have hot-loaded paddle beside tensor-split muse.
"""

from __future__ import annotations

import base64
import types

import pytest

from agent.actions import preocr_triage as pt
from agent.effects.child import DEFAULT_DOMAIN, ChildEffects
from agent.effects.inference import _image_part
from agent.effects.local import LocalEffects


class _Recorder:
    """Parent effects that records which domain each call carried."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.working_directory = "/tmp"

    async def run_vision(
        self,
        prompt,
        image_path,
        model=None,
        max_tokens=None,
        temperature=None,
        domain="",
    ):
        self.calls.append(("vision", domain))
        return types.SimpleNamespace(
            text="Raman spectra of calcite and aragonite. " * 10, error=None
        )

    async def run_inference(
        self, prompt, config_overrides=None, static_prefix=None, flow_key=None
    ):
        self.calls.append(("text", (config_overrides or {}).get("domain", "")))
        return types.SimpleNamespace(
            text='{"bin": "raman", "priority": 1, "geology": true}', error=None
        )


@pytest.mark.asyncio
async def test_child_effects_stamp_the_lane_domain_on_vision():
    parent = _Recorder()
    await ChildEffects(parent, "ocr", inference_domain="ocr").run_vision("p", "/x.png")
    await ChildEffects(parent, "ocr", inference_domain="ocr").run_vision(
        "p", "/x.png", domain="other"
    )
    assert parent.calls == [("vision", "ocr"), ("vision", "other")]


@pytest.mark.asyncio
async def test_an_unrouted_lane_calls_vision_exactly_as_before():
    seen = {}

    class _Strict:
        async def run_vision(
            self, prompt, image_path, model=None, max_tokens=None, temperature=None
        ):
            seen["ok"] = True  # no `domain` kwarg reaches a parent that never had one

    await ChildEffects(_Strict(), "figtext").run_vision("p", "/x.png")
    assert seen == {"ok": True}


@pytest.mark.asyncio
async def test_triage_reads_on_the_lane_and_judges_on_the_default(monkeypatch):
    async def _rendered(effects, pdf_rel, out_rel):
        return True, ""

    monkeypatch.setattr(pt, "render_first_page", _rendered)
    parent = _Recorder()
    await pt.triage_one(
        ChildEffects(parent, "ocr", inference_domain="ocr"), "k1", "pdfs/k1.pdf"
    )
    assert parent.calls == [("vision", "ocr"), ("text", DEFAULT_DOMAIN)]


def test_default_domain_resolves_to_the_default_client(tmp_path):
    fx = LocalEffects(
        str(tmp_path),
        llmvp_endpoint="http://localhost:8008/graphql",
        llmvp_domains={
            "ocr": {
                "endpoint": "http://192.168.1.76:8008/graphql",
                "model": "paddle-ocr-vl",
            }
        },
    )
    assert fx._get_inference(DEFAULT_DOMAIN) is fx._get_inference("")
    assert fx._get_inference("ocr")._endpoint == "http://192.168.1.76:8008/graphql"


def test_a_remote_server_gets_the_image_inline(tmp_path):
    img = tmp_path / "page.png"
    img.write_bytes(b"\x89PNG fake")
    assert _image_part("http://localhost:8008/graphql", str(img)) == {"path": str(img)}
    assert _image_part("http://127.0.0.1:8008/graphql", str(img)) == {"path": str(img)}
    part = _image_part("http://192.168.1.76:8008/graphql", str(img))
    assert part == {
        "url": "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
    }
