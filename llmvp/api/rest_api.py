#!/usr/bin/env python3
"""
REST API Module - OpenAI Compatibility Shim

FastAPI router that provides OpenAI-compatible REST endpoints.
This is a minimal compatibility layer for existing services that
know how to talk to OpenAI. GraphQL is the standard interaction
method for this project.

This module provides /v1/completions (raw prompt) and /v1/chat/completions
(OpenAI message list — for external chat agents like terminal-bench's Terminus)
as thin shims over the shared inference logic in core.inference.
"""

import json
import logging
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

# Local imports
from core.config import get_config
from core.inference import run_chat_completion, run_completion, stream_completion

# Set up logging
config = get_config()
log = logging.getLogger("llm-mvp")

# Create router for OpenAI-compatible endpoints
router = APIRouter(tags=["openai-shim"])


def _unwrap_code_fence(text: str) -> str:
    """Unwrap a response that is ENTIRELY one markdown code fence.

    ```json\\n{…}\\n```  ->  {…}  . Leaves clean JSON / plain prose untouched
    (only acts when the whole stripped body starts with ``` and ends with ```).
    Lets strict json.loads chat clients consume models that fence despite a
    'no markdown' instruction.
    """
    if not text:
        return text
    s = text.strip()
    if not s.startswith("```"):
        return text
    lines = s.split("\n")
    if not lines[0].startswith("```"):
        return text
    lines = lines[1:]  # drop the opening ``` / ```json line
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    else:
        return text  # no closing fence — not a fully wrapped block, leave it
    return "\n".join(lines).strip()


@router.post("/completions")
async def completions(request: Request):
    """
    OpenAI-compatible completion endpoint.

    Provides a minimal compatibility layer for existing services
    that expect OpenAI's /v1/completions API.

    Args:
        request: HTTP request object

    Returns:
        JSON response with completion text (or streaming response if stream=true)
    """
    body = await request.json()
    user_prompt = body.get("prompt")

    if not isinstance(user_prompt, str):
        raise HTTPException(status_code=400, detail="`prompt` must be a string")

    max_tokens = body.get("max_tokens")
    temperature = body.get("temperature")
    stream = bool(body.get("stream", config.generation.streaming_default or False))
    # Opt-in per-flow KV cache (config.model.flow_kv_cache): a caller pins a
    # flow's static head by passing `static_prefix` (the invariant text that
    # leads `prompt`) + `flow_cache_key`. Ignored unless the flag is on.
    static_prefix = body.get("static_prefix")
    flow_key = body.get("flow_cache_key")

    if stream:
        return StreamingResponse(
            _stream_response(user_prompt, max_tokens, temperature),
            media_type="application/json",
        )
    else:
        try:
            outcome = await run_completion(
                prompt=user_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                static_prefix=static_prefix,
                flow_key=flow_key,
            )
            return {"choices": [{"text": outcome.text}]}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))


@router.post("/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completion endpoint.

    Accepts a ``messages`` list (system/user/assistant/tool) and renders it
    through the model's format renderer. Lets external OpenAI-chat agents
    (terminal-bench's Terminus, litellm clients) drive the model. Run the
    server with ``--skip-knowledge`` so the external scaffold's own system
    prompt isn't layered under SOUL.md. Non-streaming only for now.
    """
    body = await request.json()
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(
            status_code=400, detail="`messages` must be a non-empty list"
        )

    max_tokens = body.get("max_tokens")
    temperature = body.get("temperature")
    model = body.get("model") or config.model.name

    try:
        answer, tokens_out = await run_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Normalize a fully markdown-fenced response down to its contents. Many
    # models wrap JSON in ```json … ``` even when told not to; a compliant
    # OpenAI endpoint serving structured output returns the bare value, so
    # strict json.loads clients (terminal-bench's Terminus) expect that. This
    # only unwraps a response that is ENTIRELY one fenced block — clean JSON or
    # plain prose is returned untouched.
    answer = _unwrap_code_fence(answer)

    return {
        "id": "chatcmpl-llmvp",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"completion_tokens": tokens_out},
    }


async def _stream_response(
    prompt: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> AsyncGenerator[bytes, None]:
    """
    Generate streaming response using shared inference.

    Args:
        prompt: User prompt
        max_tokens: Maximum tokens to generate
        temperature: Sampling temperature

    Yields:
        bytes: JSON-encoded response chunks
    """
    try:
        async for text, is_complete in stream_completion(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            if is_complete:
                break
            yield json.dumps({"choices": [{"text": text}]}).encode() + b"\n"
    except ValueError as exc:
        yield json.dumps({"error": str(exc)}).encode() + b"\n"
    except RuntimeError as exc:
        yield json.dumps({"error": str(exc)}).encode() + b"\n"
