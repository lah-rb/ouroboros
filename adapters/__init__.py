"""External harness adapters — one subpackage per benchmark/environment.

adapters.tb   — terminal-bench (TB1 legacy agent + TB2/Harbor harbor_agent)
adapters.tau  — tau-bench (env wrapper, user-sim session swap, control-inversion chat layer)
adapters.swe  — SWE-bench (instance runner, patch harness, predictions/report)
adapters.gaia — GAIA (loader, runner, scorer)

Each adapter wraps its external harness so the Ouroboros mission loop stays
benchmark-agnostic; consolidated from the four root-level *_adapter packages
in the 2026-07-16 root cleanup.
"""
