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
            # Advisory text only — the consult contract (escalate is
            # read-only, 72d3901) wants a paragraph of direction, never an
            # investigation. Without this, the model may spend turn 1 on a
            # tool call, and under --max-turns 1 that IS the failure:
            # error_max_turns, exit 1, no advice delivered. Observed on the
            # 2026-08-20 devstral floor rerun — all three boss consults on
            # a stuck goal died this way while the fix the boss would have
            # named was a one-line import. Live-verified: with tools
            # disallowed the same consult answers in one turn.
            "--disallowedTools",
            "*",
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
            # BOTH streams, because the CLI reports its own failures as JSON on
            # STDOUT and leaves stderr empty. Measured live 2026-08-16: two
            # escalation consults died as `claude CLI exited 1 (model
            # claude-sonnet-5): ` — the trailing colon IS the whole diagnosis,
            # and the identical command reproduced clean from a shell, so the
            # message was the only thing standing between us and the cause.
            # Prefer the CLI's own error text when it parses; fall back to raw
            # stdout, then stderr, and say explicitly when both are empty so
            # "silent" is never mistaken for "unreported".
            err = stderr.decode("utf-8", errors="replace").strip()
            out = stdout.decode("utf-8", errors="replace").strip()
            detail = ""
            if out:
                try:
                    _d = json.loads(out)
                    detail = str(
                        _d.get("result") or _d.get("error") or _d.get("subtype") or ""
                    ).strip()
                    _status = _d.get("api_error_status")
                    if _status:
                        detail = f"[api_error_status={_status}] {detail}".strip()
                except json.JSONDecodeError:
                    detail = f"stdout: {out[-500:]}"
            if err:
                detail = f"{detail} | stderr: {err[-500:]}".strip(" |")
            if not detail:
                detail = "(both stdout and stderr were EMPTY)"
            raise RemoteProviderError(
                f"claude CLI exited {proc.returncode} (model {self._model}): {detail}"
            )

        try:
            data = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise RemoteProviderError(
                f"claude CLI returned unparseable JSON: {stdout[:200]!r}"
            ) from exc

        if data.get("is_error"):
            # api_error_status separates "the provider refused" (429/5xx —
            # retryable, and the shape a rate limit takes) from "the CLI itself
            # failed", which is the distinction that decides whether a caller
            # should back off or reconfigure.
            _status = data.get("api_error_status")
            _sub = data.get("subtype") or ""
            raise RemoteProviderError(
                "claude CLI error result"
                + (f" [api_error_status={_status}]" if _status else "")
                + (f" [{_sub}]" if _sub else "")
                + f": {data.get('result')}"
            )

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
