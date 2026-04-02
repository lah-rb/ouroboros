#!/usr/bin/env python3
"""
LLMvp Streaming Benchmark Tool

Comprehensive benchmarking script for measuring streaming LLM performance metrics.
Supports both REST API and GraphQL API with subscriptions.

Usage:
    uv run python benchmarks/streaming.py
    uv run python benchmarks/streaming.py --rest
    uv run python benchmarks/streaming.py --requests 10 --concurrency 2
"""

import asyncio
import json
import time
import statistics
import random
from typing import List, Dict, Any, Optional
from abc import ABC, abstractmethod
import argparse

import httpx
import yaml
import websockets
from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.live import Live
from rich.align import Align

# Initialize console for pretty output
console = Console()


class BenchmarkRequest:
    """Container for benchmark request configuration and results."""

    def __init__(
        self,
        request_id: int,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
    ):
        self.request_id = request_id
        self.prompt = prompt
        self.max_tokens = max_tokens
        self.temperature = temperature

        # Timing metrics (will be populated during benchmark)
        self.time_to_first_token: Optional[float] = None
        self.total_response_time: Optional[float] = None
        self.time_after_first_token: Optional[float] = None
        self.total_tokens_output: int = 0
        self.full_response_text: str = ""
        self.tokens_per_second: float = 0.0
        self.finish_reason: Optional[str] = None  # "length", "stop", etc.
        self.stream_completed: bool = False  # True when generation actually stops

        # Timing markers
        self._start_time: Optional[float] = None
        self._first_token_time: Optional[float] = None
        self._end_time: Optional[float] = None


class StreamVisualizer:
    """Handles live visualization of streaming responses."""

    def __init__(self, refresh_rate: float = 20.0):
        """
        Initialize the visualizer.

        Args:
            refresh_rate: Maximum display refresh rate in FPS (default: 20)
        """
        self.streams = {}
        self.colors = ["red", "green", "yellow", "blue", "magenta", "cyan"]
        self.live = None
        self.prompts = {}

        # Throttling mechanism to prevent performance degradation
        self._refresh_interval = 1.0 / refresh_rate  # Minimum time between refreshes
        self._last_refresh_time = 0.0
        self._refresh_pending = False
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None

    def set_prompt(self, request_id: int, prompt: str):
        """Set the prompt for a stream."""
        self.prompts[request_id] = prompt

    def initialize_stream(self, request_id: int, start_time: float):
        """Initialize a stream entry immediately when request starts.

        This creates the panel showing "Waiting for first token..." before
        any tokens arrive, allowing users to see all concurrent requests.
        """
        if request_id not in self.streams:
            self.streams[request_id] = {
                "content": "",
                "start_time": start_time,
                "first_token_time": None,
                "token_count": 0,
            }
            # Use synchronous refresh for initialization (no tokens yet, low cost)
            self._refresh_display_sync()

    def start_live_display(self):
        """Start the live display."""
        if not self.live:
            self.live = Live(auto_refresh=False)
            self.live.start()

    def stop_live_display(self):
        """Stop the live display."""
        if self.live:
            self.live.stop()
            self.live = None

    async def update_stream(self, request_id: int, token_text: str):
        """Update a stream with new token text (async, throttled)."""
        if request_id not in self.streams:
            self.streams[request_id] = {
                "content": "",
                "start_time": time.perf_counter(),
                "first_token_time": None,
                "token_count": 0,
            }

        stream = self.streams[request_id]
        stream["content"] += token_text
        stream["token_count"] += len(token_text.split())

        # Schedule a throttled refresh
        await self._schedule_refresh()

    async def update_stream_timing(
        self, request_id: int, start_time: float, first_token_time: Optional[float]
    ):
        """Update stream timing with actual benchmark request timing."""
        if request_id in self.streams:
            stream = self.streams[request_id]
            stream["start_time"] = start_time
            if first_token_time is not None:
                stream["first_token_time"] = first_token_time
            # Schedule a throttled refresh
            await self._schedule_refresh()

    async def mark_stream_complete(self, request_id: int):
        """Mark a stream as complete."""
        if request_id in self.streams:
            self.streams[request_id]["completed"] = True
            self.streams[request_id]["end_time"] = time.perf_counter()
        # Force immediate refresh on completion for responsiveness
        await self._force_refresh()

    async def _schedule_refresh(self):
        """Schedule a display refresh, throttled to max refresh rate."""
        async with self._refresh_lock:
            now = time.perf_counter()
            time_since_last = now - self._last_refresh_time

            if time_since_last >= self._refresh_interval:
                # Enough time has passed, refresh immediately
                self._last_refresh_time = now
                self._refresh_pending = False
                self._refresh_display_sync()
            elif not self._refresh_pending:
                # Schedule a delayed refresh
                self._refresh_pending = True
                delay = self._refresh_interval - time_since_last
                if self._refresh_task:
                    self._refresh_task.cancel()
                self._refresh_task = asyncio.create_task(self._delayed_refresh(delay))

    async def _delayed_refresh(self, delay: float):
        """Execute a delayed refresh after the specified delay."""
        try:
            await asyncio.sleep(delay)
            async with self._refresh_lock:
                self._last_refresh_time = time.perf_counter()
                self._refresh_pending = False
                self._refresh_display_sync()
        except asyncio.CancelledError:
            # Refresh was superseded by another request
            pass

    async def _force_refresh(self):
        """Force an immediate display refresh."""
        async with self._refresh_lock:
            # Cancel any pending delayed refresh
            if self._refresh_task:
                self._refresh_task.cancel()
                self._refresh_task = None
            self._refresh_pending = False
            self._last_refresh_time = time.perf_counter()
            self._refresh_display_sync()

    def _create_stream_panel(self, request_id: int, stream_data: dict) -> Panel:
        """Create a rich panel for a single stream."""
        color = self.colors[request_id % len(self.colors)]

        # Use stored end_time if stream is completed, otherwise use current time
        end_time = stream_data.get("end_time", time.perf_counter())
        elapsed = end_time - stream_data["start_time"]
        if (
            "first_token_time" in stream_data
            and stream_data["first_token_time"] is not None
        ):
            ttft = stream_data["first_token_time"] - stream_data["start_time"]
            # Use end_time for tokens/sec calculation if completed
            calc_end_time = stream_data.get("end_time", time.perf_counter())
            tokens_per_sec = (
                stream_data["token_count"]
                / (calc_end_time - stream_data["first_token_time"])
                if (calc_end_time - stream_data["first_token_time"]) > 0
                else 0
            )
            title = f"Stream {request_id} | TTFT: {ttft:.3f}s | Tokens/s: {tokens_per_sec:.1f} | Elapsed: {elapsed:.1f}s"
        else:
            title = f"Stream {request_id} | Waiting for first token... | Elapsed: {elapsed:.1f}s"

        display_content = stream_data["content"]
        max_lines = 10
        lines = display_content.split("\n")
        if len(lines) > max_lines:
            display_content = "\n".join(lines[-max_lines:])
            display_content = f"[bold]...({len(lines) - max_lines} more lines)...[/bold]\n{display_content}"

        from rich.text import Text

        content_text = Text.from_markup(display_content)
        wrapped_lines = []
        current_line = ""
        words = content_text.plain.split()
        line_length = 0
        max_line_length = 60

        for word in words:
            if line_length + len(word) <= max_line_length:
                current_line += word + " "
                line_length += len(word) + 1
            else:
                wrapped_lines.append(current_line.strip())
                current_line = word + " "
                line_length = len(word) + 1
        if current_line:
            wrapped_lines.append(current_line.strip())

        content = "\n".join(wrapped_lines)

        completed_marker = " ✅" if stream_data.get("completed", False) else ""
        subtitle = self.prompts.get(request_id, "Live Streaming")
        if len(subtitle) > 60:
            subtitle = subtitle[:57] + "..."

        centered_content = Align.center(content)
        return Panel(
            centered_content,
            title=f"[bold {color}]{title}{completed_marker}[/bold {color}]",
            border_style=color,
            subtitle=subtitle,
            width=60,
        )

    def _refresh_display_sync(self):
        """Refresh the live display with all current streams (synchronous)."""
        if not self.live or not self.streams:
            return

        panels = [
            self._create_stream_panel(req_id, data)
            for req_id, data in self.streams.items()
        ]

        column_groups = []
        for i in range(0, len(panels), 3):
            column_group = panels[i : i + 3]
            if len(column_group) == 1:
                column_groups.append(column_group[0])
            else:
                columns = Columns(column_group, expand=True)
                column_groups.append(columns)

        grouped_content = Group(*column_groups)
        self.live.update(Align.center(grouped_content))
        self.live.refresh()


class BaseBenchmarkRunner(ABC):
    """Abstract base class for benchmark runners."""

    def __init__(
        self, base_url: str = "http://localhost:8000", live_visualization: bool = False
    ):
        self.base_url = base_url
        self.live_visualization = live_visualization
        self.visualizer = StreamVisualizer() if live_visualization else None

    @abstractmethod
    async def _process_request(self, request: BenchmarkRequest) -> None:
        """Process a single request and collect metrics."""
        pass

    async def run_benchmark(
        self, requests: List[BenchmarkRequest], concurrency_level: int = 1
    ) -> Dict[str, Any]:
        """Run benchmark with specified concurrency level."""
        console.print(
            f"🚀 Starting benchmark with {concurrency_level} concurrent requests...",
            style="bold green",
        )

        # Initialize live display before starting any requests
        if self.live_visualization and self.visualizer:
            self.visualizer.start_live_display()
            # Set prompts for all requests upfront
            for req in requests:
                self.visualizer.set_prompt(req.request_id, req.prompt)

        semaphore = asyncio.Semaphore(concurrency_level)

        async def process_with_semaphore(req: BenchmarkRequest):
            async with semaphore:
                await self._process_request(req)

        # Track actual wall-clock elapsed time
        benchmark_start = time.perf_counter()
        await asyncio.gather(*[process_with_semaphore(req) for req in requests])
        benchmark_end = time.perf_counter()
        total_duration = benchmark_end - benchmark_start

        return self._analyze_results(requests, total_duration)

    def _calculate_metrics(self, request: BenchmarkRequest) -> None:
        """Calculate timing and token metrics for a completed request.

        This method should be called by subclasses after processing is complete.
        """
        if (
            request._start_time is not None
            and request._first_token_time is not None
            and request._end_time is not None
        ):

            request.time_to_first_token = (
                request._first_token_time - request._start_time
            )
            request.total_response_time = request._end_time - request._start_time
            request.time_after_first_token = (
                request._end_time - request._first_token_time
            )

        # NOTE: This is an approximation - actual token count may differ
        # as LLM tokens don't map 1:1 to whitespace-separated words
        if request.full_response_text:
            tokens = request.full_response_text.split()
            request.total_tokens_output = len(tokens)

        if (
            request.time_after_first_token is not None
            and request.time_after_first_token > 0
        ):
            request.tokens_per_second = (
                request.total_tokens_output / request.time_after_first_token
            )
        else:
            request.tokens_per_second = 0.0

    async def cleanup(self) -> None:
        """Cleanup resources. Override in subclasses if needed."""
        pass

    def _analyze_results(
        self, requests: List[BenchmarkRequest], total_duration: float
    ) -> Dict[str, Any]:
        """Analyze benchmark results and generate statistics."""
        successful_requests = [r for r in requests if r.time_to_first_token is not None]

        if not successful_requests:
            return {"success": False, "error": "No successful requests", "requests": []}

        ttft_values = [r.time_to_first_token for r in successful_requests]
        total_time_values = [r.total_response_time for r in successful_requests]
        after_first_token_values = [
            r.time_after_first_token for r in successful_requests
        ]
        token_count_values = [r.total_tokens_output for r in successful_requests]
        tokens_per_second_values = [r.tokens_per_second for r in successful_requests]

        return {
            "success": True,
            "request_count": len(requests),
            "successful_requests": len(successful_requests),
            "total_duration": total_duration,  # Actual wall-clock elapsed time
            "time_to_first_token": {
                "average": statistics.mean(ttft_values),
                "median": statistics.median(ttft_values),
                "min": min(ttft_values),
                "max": max(ttft_values),
                "std_dev": statistics.stdev(ttft_values) if len(ttft_values) > 1 else 0,
            },
            "total_response_time": {
                "average": statistics.mean(total_time_values),
                "median": statistics.median(total_time_values),
                "min": min(total_time_values),
                "max": max(total_time_values),
                "std_dev": (
                    statistics.stdev(total_time_values)
                    if len(total_time_values) > 1
                    else 0
                ),
            },
            "time_after_first_token": {
                "average": statistics.mean(after_first_token_values),
                "median": statistics.median(after_first_token_values),
                "min": min(after_first_token_values),
                "max": max(after_first_token_values),
                "std_dev": (
                    statistics.stdev(after_first_token_values)
                    if len(after_first_token_values) > 1
                    else 0
                ),
            },
            "tokens_per_second": {
                "average": statistics.mean(tokens_per_second_values),
                "median": statistics.median(tokens_per_second_values),
                "min": min(tokens_per_second_values),
                "max": max(tokens_per_second_values),
                "std_dev": (
                    statistics.stdev(tokens_per_second_values)
                    if len(tokens_per_second_values) > 1
                    else 0
                ),
            },
            "token_output": {
                "total": sum(token_count_values),
                "average_per_request": statistics.mean(token_count_values),
                "min": min(token_count_values),
                "max": max(token_count_values),
            },
            "finish_reasons": {
                reason: sum(1 for r in successful_requests if r.finish_reason == reason)
                for reason in set(
                    r.finish_reason for r in successful_requests if r.finish_reason
                )
            },
            "individual_results": [
                {
                    "request_id": r.request_id,
                    "time_to_first_token": r.time_to_first_token,
                    "total_response_time": r.total_response_time,
                    "time_after_first_token": r.time_after_first_token,
                    "total_tokens_output": r.total_tokens_output,
                    "tokens_per_second": r.tokens_per_second,
                    "finish_reason": r.finish_reason,
                }
                for r in successful_requests
            ],
        }


class RestBenchmarkRunner(BaseBenchmarkRunner):
    """Benchmark runner for REST API."""

    def __init__(
        self, base_url: str = "http://localhost:8000", live_visualization: bool = False
    ):
        super().__init__(base_url, live_visualization)
        self.client = httpx.AsyncClient(timeout=120.0)

    async def _process_request(self, request: BenchmarkRequest) -> None:
        """Process a REST streaming request."""
        url = f"{self.base_url}/v1/completions"

        request._start_time = time.perf_counter()

        # Initialize stream in visualizer immediately so panel shows "Waiting..."
        if self.live_visualization and self.visualizer:
            self.visualizer.initialize_stream(request.request_id, request._start_time)

        try:
            response_data = {
                "prompt": request.prompt,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "stream": True,
            }

            async with self.client.stream(
                "POST",
                url,
                json=response_data,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    console.print(
                        f"❌ Request {request.request_id} failed with status {response.status_code}",
                        style="red",
                    )
                    return

                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue

                    lines = chunk.decode().strip().split("\n")
                    for line in lines:
                        if not line.strip():
                            continue

                        try:
                            data = json.loads(line)
                            choice = data["choices"][0]
                            token_text = choice.get("text", "")
                            finish_reason = choice.get("finish_reason")

                            # Record first token time on first valid token
                            if token_text and request._first_token_time is None:
                                request._first_token_time = time.perf_counter()

                            if token_text:
                                request.full_response_text += token_text

                                if self.live_visualization:
                                    await self.visualizer.update_stream(
                                        request.request_id, token_text
                                    )
                                    await self.visualizer.update_stream_timing(
                                        request.request_id,
                                        request._start_time,
                                        request._first_token_time,
                                    )

                            # CRITICAL: Record end time immediately when generation stops
                            if finish_reason is not None:
                                request._end_time = time.perf_counter()
                                request.finish_reason = finish_reason
                                request.stream_completed = True
                                if self.live_visualization:
                                    await self.visualizer.mark_stream_complete(
                                        request.request_id
                                    )
                                break

                        except (json.JSONDecodeError, KeyError) as e:
                            console.print(
                                f"⚠️ Error parsing chunk: {e}", style="yellow"
                            )
                            continue

                    # Exit outer loop if stream completed
                    if request.stream_completed:
                        break

        except Exception as e:
            console.print(f"❌ Request {request.request_id} failed: {e}", style="red")
            return

        # If stream didn't complete normally (e.g., connection closed), record end time now
        if not request.stream_completed:
            request._end_time = time.perf_counter()

        self._calculate_metrics(request)

    async def cleanup(self) -> None:
        """Close the HTTP client to free resources."""
        await self.client.aclose()


class GraphQLBenchmarkRunner(BaseBenchmarkRunner):
    """Benchmark runner for GraphQL API with WebSocket subscriptions."""

    def __init__(
        self, base_url: str = "http://localhost:8000", live_visualization: bool = False
    ):
        super().__init__(base_url, live_visualization)
        # Convert http:// to ws:// for WebSocket connection
        self.ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")

    async def _process_request(self, request: BenchmarkRequest) -> None:
        """Process a GraphQL subscription request via WebSocket."""
        ws_endpoint = f"{self.ws_url}/graphql"

        request._start_time = time.perf_counter()

        # Initialize stream in visualizer immediately so panel shows "Waiting..."
        if self.live_visualization and self.visualizer:
            self.visualizer.initialize_stream(request.request_id, request._start_time)

        try:
            async with websockets.connect(
                ws_endpoint, subprotocols=["graphql-transport-ws"]
            ) as websocket:
                # Initialize connection
                init_message = {"type": "connection_init"}
                await websocket.send(json.dumps(init_message))

                # Wait for connection acknowledgment
                response = await websocket.recv()
                data = json.loads(response)
                if data.get("type") != "connection_ack":
                    console.print(
                        f"❌ Connection not acknowledged for request {request.request_id}",
                        style="red",
                    )
                    return

                # Send subscription request
                subscription_message = {
                    "type": "subscribe",
                    "id": str(request.request_id),
                    "payload": {
                        "query": """
                            subscription StreamCompletion($request: CompletionRequest!) {
                                streamCompletion(request: $request) {
                                    text
                                    isComplete
                                }
                            }
                        """,
                        "variables": {
                            "request": {
                                "prompt": request.prompt,
                                "maxTokens": request.max_tokens,
                                "temperature": request.temperature,
                            }
                        },
                    },
                }
                await websocket.send(json.dumps(subscription_message))

                # Receive streaming responses
                while True:
                    try:
                        response = await asyncio.wait_for(
                            websocket.recv(), timeout=120.0
                        )
                        data = json.loads(response)

                        if data.get("type") == "next":
                            payload = (
                                data.get("payload", {})
                                .get("data", {})
                                .get("streamCompletion", {})
                            )
                            token_text = payload.get("text", "")
                            is_complete = payload.get("isComplete", False)

                            # Record first token time on first valid token
                            if token_text and request._first_token_time is None:
                                request._first_token_time = time.perf_counter()

                            if token_text:
                                request.full_response_text += token_text

                                if self.live_visualization:
                                    await self.visualizer.update_stream(
                                        request.request_id, token_text
                                    )
                                    await self.visualizer.update_stream_timing(
                                        request.request_id,
                                        request._start_time,
                                        request._first_token_time,
                                    )

                            # CRITICAL: Record end time immediately when generation stops
                            if is_complete:
                                request._end_time = time.perf_counter()
                                request.stream_completed = True
                                request.finish_reason = "complete"
                                if self.live_visualization:
                                    await self.visualizer.mark_stream_complete(
                                        request.request_id
                                    )
                                break

                        elif data.get("type") == "error":
                            console.print(
                                f"❌ GraphQL error for request {request.request_id}: {data}",
                                style="red",
                            )
                            break

                    except asyncio.TimeoutError:
                        console.print(
                            f"⚠️ Timeout waiting for response in request {request.request_id}",
                            style="yellow",
                        )
                        break

                # Send complete message
                complete_message = {"type": "complete", "id": str(request.request_id)}
                await websocket.send(json.dumps(complete_message))

        except Exception as e:
            console.print(f"❌ Request {request.request_id} failed: {e}", style="red")
            return

        # If stream didn't complete normally, record end time now
        if not request.stream_completed:
            request._end_time = time.perf_counter()

        self._calculate_metrics(request)


def load_prompts_from_file(prompts_file: str = "benchmarks/prompts.yaml") -> List[str]:
    """Load prompts from YAML file."""
    try:
        with open(prompts_file, "r", encoding="utf-8") as f:
            prompts_config = yaml.safe_load(f)

        if "prompts" in prompts_config and isinstance(prompts_config["prompts"], list):
            return prompts_config["prompts"]
        else:
            console.print(
                "⚠️ No 'prompts' list found in YAML file. Using default prompts.",
                style="yellow",
            )
            return get_default_prompts()
    except FileNotFoundError:
        console.print(
            f"⚠️ Prompts file {prompts_file} not found. Using default prompts.",
            style="yellow",
        )
        return get_default_prompts()
    except Exception as e:
        console.print(
            f"⚠️ Error loading prompts file: {e}. Using default prompts.",
            style="yellow",
        )
        return get_default_prompts()


def get_default_prompts() -> List[str]:
    """Get default prompts."""
    return [
        "What are the basic rules of Kipukas?",
        "Can you explain how scoring works in Kipukas?",
        "What types of cards exist in Kipukas and what do they do?",
    ]


def get_random_prompts(
    num_requests: int, prompts_file: str = "benchmarks/prompts.yaml"
) -> List[str]:
    """Get random prompts for benchmarking."""
    all_prompts = load_prompts_from_file(prompts_file)

    if len(all_prompts) < num_requests:
        console.print(
            f"ℹ️ Only {len(all_prompts)} unique prompts available, some will be repeated.",
            style="blue",
        )
        return [random.choice(all_prompts) for _ in range(num_requests)]
    else:
        return random.sample(all_prompts, num_requests)


def display_results(results: Dict[str, Any], live_visualization_enabled: bool = False):
    """Display benchmark results in a user-friendly format."""
    console.print("\n📊 BENCHMARK RESULTS", style="bold blue")

    summary_table = Table(title="Aggregate Metrics", show_header=True)
    summary_table.add_column("Metric")
    summary_table.add_column("Average")
    summary_table.add_column("Median")
    summary_table.add_column("Min")
    summary_table.add_column("Max")
    summary_table.add_column("Std Dev")
    summary_table.add_column("Sum / Total")

    def format_time(seconds: float) -> str:
        if seconds < 0.001:
            return f"{seconds*1000:.2f} ms"
        else:
            return f"{seconds:.3f} s"

    # Use actual wall-clock elapsed time for overall throughput calculation
    total_duration = results.get("total_duration", 0.0)

    ttft = results["time_to_first_token"]
    summary_table.add_row(
        "Time to First Token",
        format_time(ttft["average"]),
        format_time(ttft["median"]),
        format_time(ttft["min"]),
        format_time(ttft["max"]),
        format_time(ttft["std_dev"]),
        "",
    )

    total = results["total_response_time"]
    summary_table.add_row(
        "Total Response Time",
        format_time(total["average"]),
        format_time(total["median"]),
        format_time(total["min"]),
        format_time(total["max"]),
        format_time(total["std_dev"]),
        format_time(total_duration),  # Corrected: actual elapsed time
    )

    after = results["time_after_first_token"]
    summary_table.add_row(
        "Time After First Token",
        format_time(after["average"]),
        format_time(after["median"]),
        format_time(after["min"]),
        format_time(after["max"]),
        format_time(after["std_dev"]),
        "",
    )

    token_count_values = [
        r["total_tokens_output"] for r in results["individual_results"]
    ]
    median_tokens = statistics.median(token_count_values)
    std_dev_tokens = (
        statistics.stdev(token_count_values) if len(token_count_values) > 1 else 0
    )

    tokens = results["token_output"]
    summary_table.add_row(
        "Tokens per Request",
        f"{tokens['average_per_request']:.1f}",
        f"{median_tokens:.1f}",
        str(tokens["min"]),
        str(tokens["max"]),
        f"{std_dev_tokens:.1f}",
        f"{tokens['total']}",
    )

    # Calculate overall throughput using actual elapsed time
    tps = results["tokens_per_second"]
    if results.get("individual_results") and total_duration > 0:
        total_tokens = sum(
            r["total_tokens_output"] for r in results["individual_results"]
        )
        overall_tokens_per_second = total_tokens / total_duration
    else:
        overall_tokens_per_second = 0.0

    summary_table.add_row(
        "Tokens per Second",
        f"{tps['average']:.1f}",
        f"{tps['median']:.1f}",
        f"{tps['min']:.1f}",
        f"{tps['max']:.1f}",
        f"{tps['std_dev']:.1f}",
        f"{overall_tokens_per_second:.1f}",
    )

    # Total Benchmark Duration row with corrected time
    summary_table.add_row(
        "Total Benchmark Duration",
        "",
        "",
        "",
        "",
        "",
        format_time(total_duration),  # Corrected: actual elapsed time
    )

    console.print(summary_table)

    # Display finish reason breakdown
    if "finish_reasons" in results and results["finish_reasons"]:
        console.print("\n🛑 Finish Reasons:", style="bold yellow")
        for reason, count in results["finish_reasons"].items():
            reason_desc = {
                "length": "max_tokens reached",
                "stop": "EOS token",
                "complete": "completed",
            }.get(reason, reason)
            console.print(f"  • {reason} ({reason_desc}): {count}")

    if not live_visualization_enabled and len(results["individual_results"]) <= 10:
        console.print("\n📋 INDIVIDUAL REQUEST METRICS", style="bold green")
        individual_table = Table(
            title=f"Individual Results ({len(results['individual_results'])} requests)",
            show_header=True,
        )
        individual_table.add_column("ID")
        individual_table.add_column("TTFT")
        individual_table.add_column("Total Time")
        individual_table.add_column("After First Token")
        individual_table.add_column("Tokens")
        individual_table.add_column("Tokens/s")

        for result in results["individual_results"]:
            individual_table.add_row(
                str(result["request_id"]),
                format_time(result["time_to_first_token"]),
                format_time(result["total_response_time"]),
                format_time(result["time_after_first_token"]),
                str(result["total_tokens_output"]),
                f"{result['tokens_per_second']:.1f}",
            )

        console.print(individual_table)

    success_rate = results["successful_requests"] / results["request_count"]
    console.print(f"\n🎯 Success Rate: {success_rate:.1%}", style="bold green")

    # Note about token counting
    console.print(
        "\n[dim]ℹ️  Note: Token counts are approximated using whitespace splitting. "
        "Actual LLM token counts may differ as tokens don't map 1:1 to words.[/dim]"
    )


def main():
    """Main entry point for the benchmark script."""
    parser = argparse.ArgumentParser(
        description="LLMvp Streaming Benchmark Tool - Supports REST and GraphQL APIs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--url", default="http://localhost:8000", help="Base URL of the LLMvp server"
    )

    parser.add_argument(
        "--rest",
        action="store_true",
        help="Use REST API instead of GraphQL API (default is GraphQL)",
    )

    parser.add_argument(
        "--requests", type=int, default=5, help="Number of benchmark requests to make"
    )

    parser.add_argument(
        "--concurrency", type=int, default=1, help="Number of concurrent requests"
    )

    parser.add_argument(
        "--max-tokens",
        type=int,
        default=256,
        help="Maximum tokens to generate per request",
    )

    parser.add_argument(
        "--temperature", type=float, default=0.7, help="Temperature for generation"
    )

    parser.add_argument(
        "--live", action="store_true", help="Enable live streaming visualization"
    )

    args = parser.parse_args()

    # Generate benchmark requests
    try:
        prompts = get_random_prompts(args.requests)
        requests = [
            BenchmarkRequest(
                request_id=i,
                prompt=prompts[i],
                max_tokens=args.max_tokens,
                temperature=args.temperature,
            )
            for i in range(args.requests)
        ]
    except Exception as e:
        console.print(
            f"❌ Error getting random prompts: {e}. Falling back to default prompt.",
            style="red",
        )
        fallback_prompt = "What are the basic rules of Kipukas?"
        requests = [
            BenchmarkRequest(
                request_id=i,
                prompt=fallback_prompt,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
            )
            for i in range(args.requests)
        ]

    # Create appropriate benchmark runner
    # GraphQL is now the default, --rest is opt-in
    if args.rest:
        console.print("🔌 Using REST API", style="bold cyan")
        runner = RestBenchmarkRunner(base_url=args.url, live_visualization=args.live)
    else:
        console.print(
            "🔌 Using GraphQL API with WebSocket subscriptions", style="bold cyan"
        )
        runner = GraphQLBenchmarkRunner(base_url=args.url, live_visualization=args.live)

    try:
        results = asyncio.run(runner.run_benchmark(requests, args.concurrency))

        if args.live and runner.visualizer:
            runner.visualizer.stop_live_display()

        display_results(results, args.live)

    except Exception as e:
        console.print(f"❌ Benchmark failed: {e}", style="red")
        raise
    finally:
        # Cleanup resources (e.g., close HTTP client)
        asyncio.run(runner.cleanup())


if __name__ == "__main__":
    main()
