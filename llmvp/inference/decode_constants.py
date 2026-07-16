"""Shared per-token decode-loop thresholds.

One source of truth for magic numbers that previously appeared as literals
in BOTH token loops (pool: llama_cpp_backend.generate_stream_sync; batched:
batched_engine.TokenPipeline) and had already started to drift.
"""

# Requests at or below this many max_tokens buffer their whole output instead
# of streaming per token (menu/short-answer turns — cheaper bridging).
BUFFER_MODE_MAX_TOKENS = 16

# Extra bytes beyond the longest stop-text kept when scanning the rolling
# tail for stop sequences (multi-byte boundary slack).
STOP_TAIL_SLACK = 8

# Detokenization context: how many prior tokens are passed to detokenize()
# so llama.cpp emits correct piece boundaries. Bounded — an unbounded prior
# list re-grows the O(n²) per-token cost incremental detok exists to remove.
DETOK_TAIL = 16
