"""First-class HTTP effects (http_request / http_download).

The scraper's scholarly APIs (Semantic Scholar, OpenAlex, Unpaywall) are
plain JSON REST — a first-class effect (httpx in LocalEffects, canned in
MockEffects) instead of an MCP server. Contract pins: never raises
(transport errors -> status=0 + error), JSON parsed only for JSON
content-types, downloads are sandbox-guarded, reject text/html (paywall
redirect pages masquerading as PDFs), and cap body size.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agent.effects.local import LocalEffects
from agent.effects.mock import MockEffects
from agent.effects.protocol import DownloadResult, HttpResult


def _local(tmp_path, handler) -> LocalEffects:
    return LocalEffects(
        working_directory=str(tmp_path),
        http_transport=httpx.MockTransport(handler),
    )


# ── LocalEffects.http_request ─────────────────────────────────────────


def test_json_response_parsed(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"data": [1, 2]})

    fx = _local(tmp_path, handler)
    r = asyncio.run(fx.http_request("GET", "https://api.example.org/x"))
    assert r.status == 200
    assert r.json_data == {"data": [1, 2]}
    assert r.error is None


def test_non_json_response_keeps_text_only(tmp_path):
    def handler(request):
        return httpx.Response(200, text="plain", headers={"content-type": "text/plain"})

    fx = _local(tmp_path, handler)
    r = asyncio.run(fx.http_request("GET", "https://api.example.org/x"))
    assert r.json_data is None
    assert r.text == "plain"


def test_transport_error_is_fail_soft(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom")

    fx = _local(tmp_path, handler)
    r = asyncio.run(fx.http_request("GET", "https://api.example.org/x"))
    assert r.status == 0
    assert "boom" in (r.error or "")


def test_params_forwarded(tmp_path):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    fx = _local(tmp_path, handler)
    asyncio.run(
        fx.http_request("GET", "https://api.example.org/x", params={"q": "hea"})
    )
    assert "q=hea" in seen["url"]


# ── LocalEffects.http_download ────────────────────────────────────────


def test_download_streams_bytes_to_workspace(tmp_path):
    def handler(request):
        return httpx.Response(
            200, content=b"%PDF-1.4 body", headers={"content-type": "application/pdf"}
        )

    fx = _local(tmp_path, handler)
    r = asyncio.run(fx.http_download("https://x.org/p.pdf", "pdfs/p.pdf"))
    assert r.success is True
    assert (tmp_path / "pdfs" / "p.pdf").read_bytes() == b"%PDF-1.4 body"


def test_download_rejects_html(tmp_path):
    def handler(request):
        return httpx.Response(
            200, text="<html>login</html>", headers={"content-type": "text/html"}
        )

    fx = _local(tmp_path, handler)
    r = asyncio.run(fx.http_download("https://x.org/p.pdf", "pdfs/p.pdf"))
    assert r.success is False
    assert "text/html" in (r.error or "")
    assert not (tmp_path / "pdfs" / "p.pdf").exists()


def test_download_rejects_path_traversal(tmp_path):
    fx = _local(tmp_path, lambda request: httpx.Response(200))
    r = asyncio.run(fx.http_download("https://x.org/p.pdf", "../escape.pdf"))
    assert r.success is False
    assert not (tmp_path.parent / "escape.pdf").exists()


def test_download_enforces_max_bytes(tmp_path):
    def handler(request):
        return httpx.Response(
            200, content=b"x" * 2048, headers={"content-type": "application/pdf"}
        )

    fx = _local(tmp_path, handler)
    r = asyncio.run(
        fx.http_download("https://x.org/p.pdf", "pdfs/p.pdf", max_bytes=1024)
    )
    assert r.success is False
    assert "max_bytes" in (r.error or "")
    assert not (tmp_path / "pdfs" / "p.pdf").exists()


def test_download_non_200_fails(tmp_path):
    fx = _local(tmp_path, lambda request: httpx.Response(403))
    r = asyncio.run(fx.http_download("https://x.org/p.pdf", "pdfs/p.pdf"))
    assert r.success is False
    assert r.status == 403


# ── MockEffects canning ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_exact_and_prefix_match():
    fx = MockEffects(
        http_responses={
            "https://api.x.org/a": HttpResult(status=200, url="a", text="exact"),
            "https://api.x.org/": HttpResult(status=200, url="p", text="prefix"),
        }
    )
    assert (await fx.http_request("GET", "https://api.x.org/a")).text == "exact"
    assert (await fx.http_request("GET", "https://api.x.org/b")).text == "prefix"


@pytest.mark.asyncio
async def test_mock_list_pops_in_order_and_unmatched_fails_soft():
    fx = MockEffects(
        http_responses={
            "https://api.x.org/q": [
                HttpResult(status=200, url="q", text="first"),
                HttpResult(status=429, url="q", text="second"),
            ]
        }
    )
    assert (await fx.http_request("GET", "https://api.x.org/q")).text == "first"
    assert (await fx.http_request("GET", "https://api.x.org/q")).status == 429
    unmatched = await fx.http_request("GET", "https://other.org/")
    assert unmatched.status == 0 and unmatched.error


@pytest.mark.asyncio
async def test_mock_download_default_writes_file_and_canned_failure():
    fx = MockEffects(
        http_downloads={
            "https://x.org/closed.pdf": DownloadResult(
                success=False, url="https://x.org/closed.pdf", path="p", error="403"
            )
        }
    )
    ok = await fx.http_download("https://x.org/open.pdf", "pdfs/open.pdf")
    assert ok.success is True
    assert "pdfs/open.pdf" in fx._files
    bad = await fx.http_download("https://x.org/closed.pdf", "pdfs/closed.pdf")
    assert bad.success is False
    assert fx.call_count("http_download") == 2
