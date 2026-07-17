"""Claude CLI provider: `claude -p` as a registry model.

One completion = one subprocess run of the locally-installed claude CLI
in print mode with JSON output. Auth is the CLI's own problem (it holds
its login); LLMVP never sees or handles credentials. `--max-turns 1`
keeps the call a pure completion — no agentic tool loops on our dime.

Result JSON shape (claude CLI 2.x): {"type": "result", "result": <text>,
"usage": {"input_tokens": N, "output_tokens": N, ...}, ...}. Fields are
read defensively so CLI upgrades degrade to missing counts, not errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from .base import RemoteCompletion, RemoteProviderError

log = logging.getLogger("llm-mvp")


class ClaudeCliProvider:
    def __init__(
        self,
        model: str,
        *,
        claude_bin: str = "claude",
        timeout_s: float = 300.0,
    ):
        self._model = model
        self._bin = claude_bin
        self._timeout_s = timeout_s

    async def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,  # noqa: ARG002 — CLI has no cap flag
        temperature: Optional[float] = None,  # noqa: ARG002 — CLI fixes sampling
    ) -> RemoteCompletion:
        cmd = [
            self._bin,
            "-p",
            "--model",
            self._model,
            "--output-format",
            "json",
            "--max-turns",
            "1",
        ]
        if system:
            cmd += ["--system-prompt", system]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=self._timeout_s
            )
        except FileNotFoundError as exc:
            raise RemoteProviderError(
                f"claude CLI not found ({self._bin!r}) — is it installed?"
            ) from exc
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            raise RemoteProviderError(
                f"claude CLI timed out after {self._timeout_s:.0f}s "
                f"(model {self._model})"
            ) from exc

        if proc.returncode != 0:
            tail = stderr.decode("utf-8", errors="replace")[-500:]
            raise RemoteProviderError(
                f"claude CLI exited {proc.returncode} (model {self._model}): {tail}"
            )

        try:
            data = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise RemoteProviderError(
                f"claude CLI returned unparseable JSON: {stdout[:200]!r}"
            ) from exc

        if data.get("is_error"):
            raise RemoteProviderError(f"claude CLI error result: {data.get('result')}")

        tokens_in, tokens_out = _extract_usage(data)
        return RemoteCompletion(
            text=str(data.get("result", "")),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            model=self._model,
            raw=data,
        )


def _extract_usage(data: dict) -> tuple[int, int]:
    """(input, output) token totals from a claude CLI result.

    Input counts CACHED tokens too — for cost-comparable traces the
    question is "how much context did this call consume", not "what was
    billed uncached". Top-level ``usage`` (snake_case) is primary; some
    runs report zeros there and carry real numbers only in per-model
    ``modelUsage`` (camelCase), so that is the fallback.
    """
    u = data.get("usage") or {}
    tokens_in = sum(
        int(u.get(k, 0) or 0)
        for k in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    tokens_out = int(u.get("output_tokens", 0) or 0)
    if tokens_in == 0 and tokens_out == 0:
        for mu in (data.get("modelUsage") or {}).values():
            tokens_in += sum(
                int(mu.get(k, 0) or 0)
                for k in (
                    "inputTokens",
                    "cacheCreationInputTokens",
                    "cacheReadInputTokens",
                )
            )
            tokens_out += int(mu.get("outputTokens", 0) or 0)
    return tokens_in, tokens_out
