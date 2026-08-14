"""The second-transport retry on a document that came back as HTML.

WHY IT EXISTS. Measured 2026-08-13, same URL and same headers, five Springer
open-access PDFs: urllib returned the PDF 5/5, httpx returned a 3,036-byte
interstitial 5/5. Across a mixed sample of failed records 6/14 (43%) were
recoverable purely by changing client. It is not the headers — HTTP/2 off,
ALPN stripped, `Accept-Encoding: identity` and `Connection: close` all still
return HTML — the discriminator is the TLS handshake.

The risk this file guards is not "does it work". It is that a fallback which
fires too eagerly, or believes a bad answer, quietly poisons the corpus with
HTML saved as .pdf. Every test below is about the fallback DECLINING.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from agent.effects.local import LocalEffects


@pytest.fixture
def fx(tmp_path):
    return LocalEffects(working_directory=str(tmp_path))


def _run(
    fx,
    url,
    path,
    monkeypatch,
    *,
    status=200,
    ctype="application/pdf",
    body=b"%PDF-1.4 x",
):
    """Drive _retry_download_stdlib with a stubbed urllib fetch."""

    class _Resp:
        status = None
        headers = {}

        def read(self, n):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    resp = _Resp()
    resp.status = status
    resp.headers = {"content-type": ctype}
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: resp, raising=False
    )
    monkeypatch.setattr(
        fx, "_get_http_client", lambda: SimpleNamespace(headers={"User-Agent": "x"})
    )
    resolved = os.path.join(fx.working_directory, path)
    return asyncio.run(
        fx._retry_download_stdlib(url, resolved, path, 10.0, 50_000_000, 0.0)
    )


def test_recovers_a_real_pdf(fx, monkeypatch):
    out = _run(fx, "https://pub/x.pdf", "pdfs/a.pdf", monkeypatch)
    assert out is not None and out.success
    assert out.bytes_written == len(b"%PDF-1.4 x")
    assert os.path.isfile(os.path.join(fx.working_directory, "pdfs/a.pdf"))


def test_declines_when_stdlib_also_returns_html(fx, monkeypatch):
    """The common case, and it must fall THROUGH to the original rejection —
    returning None, not a failure result that masks httpx's real answer."""
    out = _run(
        fx,
        "https://pub/x.pdf",
        "pdfs/a.pdf",
        monkeypatch,
        ctype="text/html; charset=utf-8",
        body=b"<html>nope</html>",
    )
    assert out is None
    assert not os.path.exists(os.path.join(fx.working_directory, "pdfs/a.pdf"))


def test_declines_on_non_200(fx, monkeypatch):
    out = _run(fx, "https://pub/x.pdf", "pdfs/a.pdf", monkeypatch, status=403)
    assert out is None


def test_enforces_pdf_magic(fx, monkeypatch):
    """A JPEG served at a .pdf URL passed every earlier check once and became
    a vacuously-verified 1-page paper. The fallback obeys the same rule."""
    out = _run(
        fx,
        "https://pub/x.pdf",
        "pdfs/a.pdf",
        monkeypatch,
        ctype="application/pdf",
        body=b"\xff\xd8\xff\xe0 jpeg",
    )
    assert out is None
    assert not os.path.exists(os.path.join(fx.working_directory, "pdfs/a.pdf"))


def test_enforces_max_bytes(fx, monkeypatch):
    class _Resp:
        status = 200
        headers = {"content-type": "application/pdf"}

        def read(self, n):
            return b"%PDF-" + b"x" * n  # always over the cap

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _Resp(), raising=False
    )
    monkeypatch.setattr(fx, "_get_http_client", lambda: SimpleNamespace(headers={}))
    resolved = os.path.join(fx.working_directory, "pdfs/a.pdf")
    out = asyncio.run(
        fx._retry_download_stdlib("u", resolved, "pdfs/a.pdf", 10.0, 100, 0.0)
    )
    assert out is None


def test_network_error_declines_quietly(fx, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr("urllib.request.urlopen", boom, raising=False)
    monkeypatch.setattr(fx, "_get_http_client", lambda: SimpleNamespace(headers={}))
    resolved = os.path.join(fx.working_directory, "pdfs/a.pdf")
    out = asyncio.run(
        fx._retry_download_stdlib("u", resolved, "pdfs/a.pdf", 10.0, 1000, 0.0)
    )
    assert out is None


def test_kill_switch(fx, monkeypatch):
    """One env var turns the second transport off entirely."""
    monkeypatch.setenv("OUROBOROS_DOWNLOAD_FALLBACK", "0")
    calls = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: calls.append(1),
        raising=False,
    )
    resolved = os.path.join(fx.working_directory, "pdfs/a.pdf")
    out = asyncio.run(
        fx._retry_download_stdlib("u", resolved, "pdfs/a.pdf", 10.0, 1000, 0.0)
    )
    assert out is None
    assert calls == [], "kill switch must prevent the request, not just the write"


def test_non_pdf_destination_skips_the_magic_rule(fx, monkeypatch):
    """The magic check keys off the DESTINATION, so a non-.pdf download is
    still allowed to be something else."""
    out = _run(
        fx,
        "https://pub/x.xml",
        "data/a.xml",
        monkeypatch,
        ctype="application/xml",
        body=b"<root/>",
    )
    assert out is not None and out.success
