"""Remote provider entries (MULTI_MODEL_PLAN Phase 3).

Registry detection of provider yamls, the claude_cli adapter against a
FAKE claude binary (canned JSON, arg capture), openai_compat against a
stubbed transport, and the routing rules: remote -> adapter, active
local -> fall through, inactive local -> explicit error, unknown ->
KeyError, remote never swappable."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import model_registry, model_swap, remote_router  # noqa: E402
from inference.providers import (  # noqa: E402
    ClaudeCliProvider,
    OpenAICompatProvider,
    RemoteProviderError,
)


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """One local config + one claude_cli entry + one openai_compat entry."""
    configs = tmp_path / "configs"
    configs.mkdir()
    gguf = tmp_path / "weights.gguf"
    gguf.touch()
    os.truncate(gguf, 50_000_000)

    (configs / "local-a.yaml").write_text(
        yaml.safe_dump(
            {
                "app": {"host": "127.0.0.1", "port": 8008},
                "model": {
                    "name": "local-a",
                    "family": "chatml",
                    "path": str(gguf),
                    "n_ctx": 4096,
                    "n_gpu_layers": -1,
                    "seed": -1,
                    "verbose": False,
                },
                "prompt": {},
                "generation": {},
                "knowledge": {"tokens_bin": "./x.bin", "token_limit": 64},
                "resources": {"cpu_threads": 1, "max_concurrent_requests": 1},
                "logging": {"enabled": False, "directory": "./logs"},
            }
        )
    )
    system_file = tmp_path / "BOSS.md"
    system_file.write_text("You are the boss.")
    (configs / "boss-claude.yaml").write_text(
        yaml.safe_dump(
            {
                "provider": "claude_cli",
                "model": "claude-opus-4-8",
                "system_file": str(system_file),
                "temperature_default": 0.4,
            }
        )
    )
    (configs / "boss-lmstudio.yaml").write_text(
        yaml.safe_dump(
            {
                "provider": "openai_compat",
                "model": "olmo-3.1-32b",
                "base_url": "http://localhost:1234/v1",
            }
        )
    )
    pointer = tmp_path / "active_config.txt"
    pointer.write_text("local-a\n")

    monkeypatch.setattr(model_registry, "CONFIGS_DIR", configs)
    monkeypatch.setattr(model_registry, "POINTER_FILE", pointer)
    remote_router._adapters.clear()
    return SimpleNamespace(dir=configs, system_file=system_file)


# ── Registry detection ───────────────────────────────────────────────


def test_registry_lists_remote_entries(catalog):
    entries = {e.name: e for e in model_registry.list_models()}
    assert entries["boss-claude"].provider == "claude_cli"
    assert entries["boss-claude"].family == "(remote)"
    assert entries["boss-claude"].weights_present  # always addressable
    assert entries["boss-lmstudio"].provider == "openai_compat"
    assert entries["local-a"].provider == "local_llama"


def test_remote_config_returns_none_for_local(catalog):
    assert model_registry.remote_config("local-a") is None
    assert model_registry.remote_config("boss-claude").model == "claude-opus-4-8"


def test_swap_rejects_remote_entry(catalog):
    with pytest.raises(KeyError, match="remote provider entry"):
        asyncio.run(model_swap.swap_model("boss-claude"))


# ── Routing rules ────────────────────────────────────────────────────


def test_route_none_and_active_local_fall_through(catalog):
    assert remote_router.resolve_route(None) is None
    assert remote_router.resolve_route("local-a") is None  # active local


def test_route_remote_returns_adapter(catalog):
    cfg, adapter = remote_router.resolve_route("boss-claude")
    assert cfg.provider == "claude_cli"
    assert isinstance(adapter, ClaudeCliProvider)


def test_route_inactive_local_and_unknown_raise(catalog, monkeypatch):
    model_registry.write_pointer("something-else")
    with pytest.raises(remote_router.InactiveLocalModel):
        remote_router.resolve_route("local-a")
    with pytest.raises(KeyError):
        remote_router.resolve_route("nope")


def test_adapter_cache_invalidated_by_config_edit(catalog):
    _, first = remote_router.resolve_route("boss-claude")
    assert remote_router.resolve_route("boss-claude")[1] is first  # cached
    path = catalog.dir / "boss-claude.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["model"] = "claude-sonnet-5"
    path.write_text(yaml.safe_dump(raw))
    os.utime(path, (0, 12345))  # force a distinct mtime
    cfg, second = remote_router.resolve_route("boss-claude")
    assert second is not first
    assert cfg.model == "claude-sonnet-5"


# ── claude_cli adapter against a fake binary ─────────────────────────


def _fake_claude(tmp_path: Path, payload: str, exit_code: int = 0) -> str:
    script = tmp_path / "fake_claude"
    script.write_text(
        "#!/bin/bash\n"
        'echo "$@" > "$0.args"\n'
        'cat > "$0.stdin"\n'
        f"cat <<'EOF'\n{payload}\nEOF\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_claude_cli_happy_path(tmp_path):
    payload = json.dumps(
        {
            "type": "result",
            "result": "The plan: attack at dawn.",
            "usage": {"input_tokens": 42, "output_tokens": 9},
        }
    )
    bin_path = _fake_claude(tmp_path, payload)
    p = ClaudeCliProvider("claude-opus-4-8", claude_bin=bin_path, timeout_s=10)
    r = asyncio.run(p.complete("What is the plan?", system="You are the boss."))
    assert r.text == "The plan: attack at dawn."
    assert (r.input_tokens, r.output_tokens) == (42, 9)
    args = Path(bin_path + ".args").read_text()
    assert "--model claude-opus-4-8" in args
    assert "--max-turns 1" in args
    # Tools must be REMOVED FROM VIEW (--tools ""), not merely disallowed:
    # under --max-turns 1 an attempted tool call IS error_max_turns, and
    # --disallowedTools still leaves the tools visible for the model to
    # attempt (the 2026-08-20 devstral consult failure, twice — three boss
    # consults died undelivered, then the --disallowedTools fix failed live).
    assert "--tools " in args
    assert "--disallowedTools" not in args
    assert '--strict-mcp-config --mcp-config {"mcpServers":{}}' in args
    assert "--system-prompt You are the boss." in args
    assert Path(bin_path + ".stdin").read_text().strip() == "What is the plan?"


def test_claude_cli_usage_sums_cache_tokens(tmp_path):
    """Input = fresh + cache-creation + cache-read: the trace question is
    context consumed, not tokens billed uncached."""
    payload = json.dumps(
        {
            "type": "result",
            "result": "ok",
            "usage": {
                "input_tokens": 9,
                "cache_creation_input_tokens": 11000,
                "cache_read_input_tokens": 17000,
                "output_tokens": 69,
            },
        }
    )
    p = ClaudeCliProvider("m", claude_bin=_fake_claude(tmp_path, payload), timeout_s=10)
    r = asyncio.run(p.complete("hi"))
    assert (r.input_tokens, r.output_tokens) == (9 + 11000 + 17000, 69)


def test_claude_cli_usage_falls_back_to_model_usage(tmp_path):
    """Some CLI runs report zeroed top-level usage with real numbers only
    in per-model modelUsage (camelCase) — seen live on the first opus
    boss call."""
    payload = json.dumps(
        {
            "type": "result",
            "result": "ok",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "modelUsage": {
                "claude-opus-4-8": {
                    "inputTokens": 12,
                    "outputTokens": 60,
                    "cacheReadInputTokens": 5000,
                    "cacheCreationInputTokens": 100,
                }
            },
        }
    )
    p = ClaudeCliProvider("m", claude_bin=_fake_claude(tmp_path, payload), timeout_s=10)
    r = asyncio.run(p.complete("hi"))
    assert (r.input_tokens, r.output_tokens) == (12 + 5000 + 100, 60)


def test_claude_cli_nonzero_exit_raises(tmp_path):
    bin_path = _fake_claude(tmp_path, "boom", exit_code=3)
    p = ClaudeCliProvider("m", claude_bin=bin_path, timeout_s=10)
    with pytest.raises(RemoteProviderError, match="exited 3"):
        asyncio.run(p.complete("hi"))


def test_claude_cli_error_result_raises(tmp_path):
    payload = json.dumps({"type": "result", "is_error": True, "result": "over quota"})
    bin_path = _fake_claude(tmp_path, payload)
    p = ClaudeCliProvider("m", claude_bin=bin_path, timeout_s=10)
    with pytest.raises(RemoteProviderError, match="over quota"):
        asyncio.run(p.complete("hi"))


def test_claude_cli_missing_binary_raises():
    p = ClaudeCliProvider("m", claude_bin="/nonexistent/claude", timeout_s=5)
    with pytest.raises(RemoteProviderError, match="not found"):
        asyncio.run(p.complete("hi"))


# ── openai_compat adapter against a stubbed transport ────────────────


def _openai_provider_with_response(monkeypatch, status=200, body=None):
    import httpx

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(status, json=body if body is not None else {})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def client_factory(**kw):
        kw["transport"] = transport
        return orig_client(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return captured


def test_openai_compat_happy_path(monkeypatch):
    body = {
        "model": "olmo-3.1-32b",
        "choices": [{"message": {"content": "dawn attack"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }
    captured = _openai_provider_with_response(monkeypatch, body=body)
    monkeypatch.setenv("TEST_BOSS_KEY", "sk-secret")
    p = OpenAICompatProvider(
        "olmo-3.1-32b",
        base_url="http://localhost:1234/v1",
        api_key_env="TEST_BOSS_KEY",
    )
    r = asyncio.run(p.complete("plan?", system="boss", max_tokens=64, temperature=0.4))
    assert r.text == "dawn attack"
    assert (r.input_tokens, r.output_tokens) == (11, 3)
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["json"]["messages"][0] == {"role": "system", "content": "boss"}
    assert captured["json"]["max_tokens"] == 64
    assert captured["auth"] == "Bearer sk-secret"


def test_openai_compat_http_error_raises(monkeypatch):
    _openai_provider_with_response(monkeypatch, status=500, body={"error": "down"})
    p = OpenAICompatProvider("m", base_url="http://localhost:9/v1")
    with pytest.raises(RemoteProviderError, match="HTTP 500"):
        asyncio.run(p.complete("hi"))


# ── remote_completion end-to-end (router + system_file + defaults) ───


def test_remote_completion_applies_system_and_defaults(catalog, tmp_path, monkeypatch):
    payload = json.dumps({"type": "result", "result": "ok", "usage": {}})
    bin_path = _fake_claude(tmp_path, payload)
    raw = yaml.safe_load((catalog.dir / "boss-claude.yaml").read_text())
    raw["claude_bin"] = bin_path
    (catalog.dir / "boss-claude.yaml").write_text(yaml.safe_dump(raw))
    remote_router._adapters.clear()

    r = asyncio.run(remote_router.remote_completion("boss-claude", "hello"))
    assert r.text == "ok"
    args = Path(bin_path + ".args").read_text()
    assert "--system-prompt You are the boss." in args  # system_file applied


# ── Failure reporting: the CLI talks on STDOUT ───────────────────────
#
# Measured live 2026-08-16: two escalation consults died as
#   "claude CLI exited 1 (model claude-sonnet-5): "
# — a trailing colon and nothing else, because the adapter reported only
# stderr and the claude CLI writes its failures as JSON on stdout. The same
# command reproduced clean from a shell, so the empty message was the only
# thing between us and the cause. These pin the four shapes.


def test_nonzero_exit_reports_the_clis_own_json_error_from_stdout(tmp_path):
    payload = (
        '{"type":"result","is_error":true,"api_error_status":429,'
        '"subtype":"rate_limit","result":"upstream rate limit reached"}'
    )
    p = ClaudeCliProvider(
        "claude-sonnet-5", claude_bin=_fake_claude(tmp_path, payload, exit_code=1)
    )
    with pytest.raises(RemoteProviderError) as e:
        asyncio.run(p.complete("hi"))
    msg = str(e.value)
    assert "upstream rate limit reached" in msg
    assert "api_error_status=429" in msg


def test_nonzero_exit_falls_back_to_raw_stdout_when_unparseable(tmp_path):
    p = ClaudeCliProvider(
        "claude-sonnet-5",
        claude_bin=_fake_claude(tmp_path, "not json at all, just noise", exit_code=1),
    )
    with pytest.raises(RemoteProviderError) as e:
        asyncio.run(p.complete("hi"))
    assert "not json at all" in str(e.value)


def test_nonzero_exit_with_both_streams_empty_says_so(tmp_path):
    """The exact live shape — silence must read as silence, not as a blank."""
    p = ClaudeCliProvider(
        "claude-sonnet-5", claude_bin=_fake_claude(tmp_path, "", exit_code=1)
    )
    with pytest.raises(RemoteProviderError) as e:
        asyncio.run(p.complete("hi"))
    assert "EMPTY" in str(e.value)


def test_is_error_result_surfaces_status_and_subtype(tmp_path):
    """Exit 0 but is_error — the retryable/reconfigure distinction."""
    payload = (
        '{"type":"result","is_error":true,"api_error_status":529,'
        '"subtype":"overloaded","result":"service overloaded"}'
    )
    p = ClaudeCliProvider(
        "claude-sonnet-5", claude_bin=_fake_claude(tmp_path, payload, exit_code=0)
    )
    with pytest.raises(RemoteProviderError) as e:
        asyncio.run(p.complete("hi"))
    msg = str(e.value)
    assert "529" in msg and "overloaded" in msg and "service overloaded" in msg
