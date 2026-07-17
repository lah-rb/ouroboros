"""OpenAI-compatible chat provider: any base-URL /chat/completions server.

Covers hosted APIs and — the quiet win — LOCAL cross-process servers:
an LMStudio-hosted boss model is just a registry entry pointing at
http://localhost:1234/v1, decoding concurrently with LLMVP's own model
in the proven-clean separate-process regime (the P0.b escape hatch).

API key: resolved from the ENV VAR named in the entry's api_key_env at
call time; never stored, never logged.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from .base import RemoteCompletion, RemoteProviderError

log = logging.getLogger("llm-mvp")


class OpenAICompatProvider:
    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key_env: Optional[str] = None,
        timeout_s: float = 300.0,
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key_env = api_key_env
        self._timeout_s = timeout_s

    async def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> RemoteCompletion:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {"model": self._model, "messages": messages}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature

        headers = {}
        if self._api_key_env:
            key = os.environ.get(self._api_key_env, "")
            if key:
                headers["Authorization"] = f"Bearer {key}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions", json=body, headers=headers
                )
        except httpx.HTTPError as exc:
            raise RemoteProviderError(
                f"openai_compat request failed ({self._base_url}): {exc}"
            ) from exc

        if resp.status_code != 200:
            raise RemoteProviderError(
                f"openai_compat HTTP {resp.status_code} ({self._base_url}): "
                f"{resp.text[:300]}"
            )

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as exc:
            raise RemoteProviderError(
                f"openai_compat unexpected response shape: {resp.text[:300]}"
            ) from exc

        usage = data.get("usage") or {}
        return RemoteCompletion(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=str(data.get("model", self._model)),
            raw=data,
        )
