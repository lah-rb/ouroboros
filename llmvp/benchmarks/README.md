# LLMvp Streaming Benchmark

Comprehensive benchmarking tool for measuring streaming LLM performance metrics with focus on time-to-first-token, total tokens output, and streaming timing.

## Quick Start

```bash
# Basic benchmark (5 requests)
uv run llmvp.py --benchmark --requests 10

# With concurrency testing (5 concurrent requests)
uv run llmvp.py --benchmark --requests 20 --concurrency 5

# With live streaming visualization
uv run llmvp.py --benchmark --requests 4 --concurrency 2 --live
```

## Features

- **Precise Timing**: TTFT, Total Response Time, Time After First Token, Streaming Duration
- **Token Tracking**: Track the number of produced per stream and in total
- **Concurrency Testing**: Configurable concurrent request levels
- **Live Visualization**: Real-time streaming display with color-coded streams
- **Comprehensive Metrics**: Average, median, min, max, std dev, and sum calculations

## Command Line Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--url` | Base URL of LLMvp server | `http://localhost:8000` |
| `--requests` | Number of benchmark requests | `5` |
| `--concurrency` | Concurrent requests | `1` |
| `--prompt-length` | Test prompt length (short/medium/long) | `short` |
| `--max-tokens` | Max tokens per request | `256` |
| `--temperature` | Generation temperature | `0.7` |
| `--live` | Enable live streaming visualization | `False` |

## Metrics Collected

### Key Performance Indicators
- **Time to First Token (TTFT)**: Crucial for perceived responsiveness
- **Total Response Time**: Complete request processing time
- **Time After First Token**: Streaming efficiency
- **Tokens per Second**: Generation throughput

### Aggregate Statistics
- Average, median, min, max, standard deviation for all metrics
- Sum columns showing total tokens and overall throughput
- Success rate calculation

## Requirements

- Running LLMvp server instance
- Dependencies: `httpx`, `rich` (already in project dependencies)

```bash
uv sync  # Ensure all dependencies are installed
```

This benchmark targets the `/v1/completions` endpoint and is fully compatible with the LLMvp API interface.