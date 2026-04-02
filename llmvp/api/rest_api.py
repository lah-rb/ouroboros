#!/usr/bin/env python3
"""
REST API Module - OpenAI Compatibility Shim

FastAPI router that provides OpenAI-compatible REST endpoints.
This is a minimal compatibility layer for existing services that
know how to talk to OpenAI. GraphQL is the standard interaction
method for this project.

This module provides only the /v1/completions endpoint as a thin
shim over the shared inference logic from core.inference.
"""

import json
import logging
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

# Local imports
from core.config import get_config
from core.inference import run_completion, stream_completion

# Set up logging
config = get_config()
log = logging.getLogger("llm-mvp")

# Create router for OpenAI-compatible endpoints
router = APIRouter(tags=["openai-shim"])


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

    if stream:
        return StreamingResponse(
            _stream_response(user_prompt, max_tokens, temperature),
            media_type="application/json",
        )
    else:
        try:
            answer, _ = await run_completion(
                prompt=user_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return {"choices": [{"text": answer}]}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))


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
