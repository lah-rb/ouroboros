"""Trace-derived server-health classifiers — the stub-emission signal.

The generate_rewrite stub-emission rate is the primary in-process
server-souring signal (2026-06 staleness investigation: soured LLMVP emitted
agentic action-stubs instead of file rewrites on 80% of hard prompts while
vanilla llama.cpp on the same machine emitted 0%). These two classifiers are
the single source of truth for that shape, extracted from the (retired)
dev/contam_forensics.py and dev/vanilla_compare.py forensics scripts; the
kept operational monitors (dev/contam_monitor.py, dev/canary_probe.py,
dev/marathon_health.py, dev/refresh_efficacy.py) import them from here.
"""


def is_stub(response: str | None) -> bool:
    """True if a model response is an agentic action-stub, not real content."""
    r = (response or "").strip()
    if not r:
        return False
    return (
        ('"action"' in r or '"choice"' in r or "list_files" in r) and len(r) < 200
    ) or (r.startswith("```json") and len(r) < 200)


def classify(response: str | None) -> str:
    """Classify a generate_rewrite-shaped response: "stub" | "file" | "error".

    Stub if it matches the action-stub shape OR is implausibly short for a
    file rewrite (corpus stubs were ~170-320 gen-tok vs ~1300+ for a real
    file).
    """
    if response is None:
        return "error"
    r = response.strip()
    if is_stub(r):
        return "stub"
    if len(r) < 250 and ('"action"' in r or '"choice"' in r or r.startswith("```json")):
        return "stub"
    if len(r) < 120:  # nothing resembling a complete file
        return "stub"
    return "file"
