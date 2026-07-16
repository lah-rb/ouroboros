# Exploratory router — tb swe subset (2026-07-05)

> **STATUS: CLOSED 2026-07-15 — bake-off concluded: TF-IDF word+char union champion SHIPPED (agent/reasoning_router.py); ModernBERT and the hidden-state probe lost. Full history: ADAPTIVE_REASONING_DECISION_LAYER.md (this dir).**

The router now investigates the workspace (read-only REACT loop, terminal
access) before deciding flow_set + profile, and hands its findings forward.
Tested on the 4 terminal-bench-core swe-bench-* tasks, auto-routed.

## Result: 2/4 — best on this subset, correct per-task routing

| run | router | langcodes | astropy-1 | astropy-2 | fsspec | grade |
|-----|--------|-----------|-----------|-----------|--------|-------|
| swe-tb-router-1 | blind menu (single-file→ops) | ops PASS | — | ops FAIL | ops FAIL | 1/4 |
| swe-tb-router-2 | + repair floor (→code_core) | cc FAIL | — | cc FAIL | cc FAIL | 0/4 |
| swe-tb-router-3 | exploratory (session LEAK) | cc FAIL | cc FAIL | ops/dflt | ops/dflt | 0/4 |
| **swe-tb-router-4** | **exploratory (leak fixed)** | **ops PASS** | **cc PASS** | ops/dflt FAIL | cc FAIL | **2/4** |

(astropy-1 is a new task, not in the head-to-head baseline.)

## What the exploratory router got right

- **langcodes → ops** (PASS). Ran the full 5-turn scout loop (read __init__.py,
  ran commands), found "the bug is in Language.__hash__ in langcodes/__init__.py"
  — judged it localized single-file → routed **ops**, which solves it reliably
  (3/3 in isolation). This is the exact call the deterministic floor got WRONG
  (floor forced code_core → 1/3). Routing on evidence, not phrasing.
- **astropy-1 → code_core** (PASS). Routed the diffuse separability logic to
  code_core, which solved it.
- **fsspec → code_core**. Explored (found dirfs.py inheritance), routed code_core
  (diffuse). FAIL — but fsspec is unsolved by every arm ever.

So the router made DIFFERENT, correct decisions per task: ops for the localized
fix, code_core for the diffuse ones. That is the whole point of the redesign —
and it doubled the prior best (1/4 → 2/4), reclaiming langcodes-via-ops that the
floor had lost.

## Rough edges (follow-ups, not blocking)

1. **astropy-2: transient session-open failure** → `open_router_session`
   returned session_started=false → default (ops/plain, no exploration,
   handoff_ops). Only 1/4 this run (vs 2/4 in the leaked run — the leak fix
   worked), and it defaulted safely (astropy-2 fails under all arms). But the
   router should be more robust to a failed open: retry the open, or gate on
   availableInstances (cf. llmvp-pool-leaks-on-hardkill). The default fallback
   masks it as ops/plain with empty findings.
2. **conclude_route has no retry** — one session_inference; a malformed JSON
   conclusion defaults to (ops, plain). The menu turns retry 3×; the conclude
   should too. Didn't bite here (the misses were open-failures, not parse
   failures) but it's a latent robustness gap.

## Verdict

The core hypothesis held: giving the router terminal access to investigate
before routing lets it make the correct small-local→ops vs diffuse→code_core
call on evidence — langcodes→ops→PASS proves it. The exploration mechanism +
findings hand-off work; the remaining work is open/conclude robustness (retry),
not the decision logic.
