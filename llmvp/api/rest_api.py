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


@router.post("/vision")
async def vision_completions(request: Request):
    """OpenAI-shaped vision completion: messages with image content parts.

    Separate from /chat/completions on purpose — the mtmd handler builds its
    own prompt from the model's chat template, so this is a different pipeline,
    not a flag on the text one. Non-streaming, matching /chat/completions.

    Image parts accept either the standard base64 data URI::

        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}

    or a local path, which is only read when it resolves under
    ``model.vision_image_roots``::

        {"type": "image_path", "path": "/abs/path.png"}
    """
    from fastapi.responses import JSONResponse

    from core.inference import run_vision_completion
    from inference.vision_images import ImageIntakeError

    body = await request.json()
    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")
    try:
        outcome = await run_vision_completion(
            messages=messages,
            max_tokens=body.get("max_tokens"),
            temperature=body.get("temperature"),
            # OpenAI clients already send `model`; honouring it routes to a
            # hot secondary (loadModel). resolve_local_backend is STRICT — an
            # unknown or not-hot name raises instead of quietly serving the
            # primary under someone else's model name. Clients that send a
            # cosmetic model string must omit it or name a real entry.
            model=body.get("model"),
        )
    except ImageIntakeError as exc:
        # 400, not 500: the caller sent an image we will not read (outside the
        # allowlist, traversal, oversize, or a remote URL we refuse to fetch).
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except KeyError as exc:
        # An unknown `model` name. Caller error, and the ONE thing the caller
        # needs to read — an OpenAI client that discovered its model string
        # from somewhere else gets a name it can act on instead of a bare 500.
        # KeyError stringifies with its own quotes; strip them.
        return JSONResponse(status_code=400, content={"error": str(exc).strip("\"'")})
    except RuntimeError as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})
    return {
        "id": "visioncmpl-llmvp",
        "object": "vision.completion",
        "model": outcome.vision_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": outcome.text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.generated_tokens,
        },
        "vision": {
            "image_count": outcome.image_count,
            "handler": outcome.handler,
            "decode_ms": outcome.decode_ms,
        },
    }
