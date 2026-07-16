"""tau-bench adapter: Ouroboros ↔ sierra-research/tau-bench.

Piece 3 of the τ plan (tool API adapters). Wraps the official benchmark's
environments (retail/airline: tools over an in-memory JSON DB, LLM user
simulator, DB-state + output grading) behind a small surface the
control-inversion layer (piece 4) will drive:

    from adapters.tau.env import make_env
    from adapters.tau.runner import EpisodeHandle

The official grader is used untouched (never rebuild grading); the user
simulator's litellm calls are routed to the local LLMVP OpenAI shim.
"""
