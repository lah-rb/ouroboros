#!/usr/bin/env python3
"""
Abstract Base Class for LLM Backends

Defines the interface that all backend implementations must follow
to ensure feature parity across different inference engines.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncGenerator, Iterator, List, Optional, Tuple, Any
import logging

log = logging.getLogger("llm-mvp")


@dataclass
class BackendCapabilities:
    """Capabilities supported by a backend."""

    streaming: bool = True
    batching: bool = False
    async_api: bool = True
    chat_template: bool = False
    quantization: bool = True
    gpu_acceleration: bool = True
    manual_pooling: bool = True  # If backend needs manual instance pooling


class BaseBackend(ABC):
    """
    Abstract base class for LLM inference backends.

    All backends must implement these methods to ensure
    feature parity across the LLMvp system.
    """

    def __init__(self, config: Any):
        """
        Initialize backend with configuration.

        Args:
            config: The application configuration object
        """
        self.config = config
        self._capabilities = self._detect_capabilities()
        log.info(f"🔧 Initializing {self.backend_name} backend")

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Return the name of this backend."""
        pass

    @property
    def capabilities(self) -> BackendCapabilities:
        """Return backend capabilities."""
        return self._capabilities

    @abstractmethod
    def _detect_capabilities(self) -> BackendCapabilities:
        """Detect and return backend capabilities."""
        pass

    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the backend (load models, warm up, etc.).

        This is called once at startup before any inference.
        """
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """
        Clean up backend resources.

        This is called during application shutdown.
        """
        pass

    @abstractmethod
    async def acquire_instance(self) -> Any:
        """
        Acquire an inference instance from the pool.

        For backends with manual_pooling=True, this returns a model instance.
        For backends with internal pooling, this may return the engine itself.

        Returns:
            An instance handle for inference
        """
        pass

    @abstractmethod
    async def release_instance(self, instance: Any) -> None:
        """
        Release an inference instance back to the pool.

        Args:
            instance: The instance to release
        """
        pass

    @abstractmethod
    def generate_sync(
        self,
        instance: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> str:
        """
        Synchronous text generation.

        Args:
            instance: The inference instance
            prompt_tokens: Tokenized prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional generation parameters

        Returns:
            Generated text string
        """
        pass

    @abstractmethod
    def generate_stream_sync(
        self,
        instance: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> Iterator[str]:
        """
        Synchronous streaming text generation.

        Args:
            instance: The inference instance
            prompt_tokens: Tokenized prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional generation parameters

        Yields:
            Text chunks as they are generated
        """
        pass

    @abstractmethod
    async def generate_async(
        self,
        instance: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> str:
        """
        Asynchronous text generation.

        Default implementation runs sync version in thread pool.
        Backends with async_api=True should override for better performance.

        Args:
            instance: The inference instance
            prompt_tokens: Tokenized prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional generation parameters

        Returns:
            Generated text string
        """
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            self.generate_sync,
            instance,
            prompt_tokens,
            max_tokens,
            temperature,
            **kwargs,
        )

    @abstractmethod
    async def generate_stream_async(
        self,
        instance: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """
        Asynchronous streaming text generation.

        Default implementation runs sync version in thread pool.
        Backends with async_api=True should override for better performance.

        Args:
            instance: The inference instance
            prompt_tokens: Tokenized prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional generation parameters

        Yields:
            Text chunks as they are generated
        """
        from starlette.concurrency import iterate_in_threadpool

        def sync_generator():
            return self.generate_stream_sync(
                instance, prompt_tokens, max_tokens, temperature, **kwargs
            )

        async for chunk in iterate_in_threadpool(sync_generator()):
            yield chunk

    @abstractmethod
    def get_health_status(self) -> dict:
        """
        Get backend health status.

        Returns:
            Dict with status information (pool_size, available_instances, etc.)
        """
        pass

    @abstractmethod
    def tokenize(self, text: str) -> List[int]:
        """
        Tokenize text using backend's tokenizer.

        Args:
            text: Text to tokenize

        Returns:
            List of token IDs
        """
        pass

    @abstractmethod
    def detokenize(self, tokens: List[int]) -> str:
        """
        Convert tokens back to text.

        Args:
            tokens: List of token IDs

        Returns:
            Decoded text string
        """
        pass
