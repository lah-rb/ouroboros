# Contract-swarm A/B — iteration reference

Comparing the `contract_swarm` flow set (contract → review → parallel
symbol workers → splice) against `code_core`'s single-completion batch
build, same objective (missions/game_challenge_swarm.yaml), both stopped
at the structural→environment boundary (raw structural deliverable,
before functional repair). Harness: `dev/ab_contract_swarm.sh`.

The batch build is TUNED; the swarm is NEW and more complex — expect
several iteration rounds. `round0/code_core/` is the FROZEN baseline to
compare future swarm rounds against; `round0/swarm/` is each round's
swarm output for diffing. During rapid iteration we run ONE blind judge
(round 0's 3-judge panel was unanimous, so a single judge suffices).

## round0 (2026-07-17, code 286d9dd) — baseline

| | batch (code_core) | swarm |
|---|---|---|
| wall to structural-stop | 11m | 29m |
| generation tokens (batch note) | 7,837 | 33,841+ |
| raw first-shot | 8/8 files clean, 0 gate fails | 7 files, 5 failed own doctests, 1 missing |
| blind panel (3 judges) | **3–0 preferred**, scores runs 2.3 / cov 3.0 / cohesion 4.0 / quality 4.0 / play 2.3 | runs 1.0 / cov 1.0 / cohesion 1.0 / quality 2.0 / play 1.0 |

Batch won on cost, speed, AND quality. Both are stdlib text adventures;
NEITHER is spec-complete (see below).

## Why the swarm lost round 0 — INTEGRATION incoherence

The swarm's isolated workers each produced locally-valid, doctest-passing
code, but the assembled program did not cohere. Panel found:
- `main.py` command loop = a literal "Placeholder for future command
  handling" no-op — dispatches nothing.
- `state.py` carries a function-local `from .player import Player`
  (broken: no such module; relative import in a non-package).
- Interface drift: `GameState` dataclass fields vs its hand-written
  `__init__` disagree; `save_load` calls `GameState(player=, rooms=)`
  but `__init__` takes `world_data`; `combat` targets a `MonsterState`
  lacking `attack`/`defense`.

Root cause (Luke's read, confirmed): the workers were CONTEXT-STARVED by
an UNDER-SPECIFIED contract. Workers see only their contract slice +
skeleton + dependency SIGNATURES — no sibling bodies, no whole-program
view. Where the contract left an interface or the entry-point wiring
imprecise, isolated workers diverged. Structural gates (syntax/import/
lint/doctest) are per-symbol/per-module and never run the integrated
program, so they passed all of this — and `main` (a REPL) can't be
doctested at all, so the single most integration-critical file got the
least validation. This is the shared-context coherence the batch build
gets for free, and the exact latent-bug class Luke's per-file-generation
concern predicted.

The batch build is not "good" — it has an unwinnable boss (weakness item
can't be equipped), atomic combat (no mid-fight heal, flee is a no-op),
2 monsters not 3, a launch-path bug. It is a REAL BUGGY GAME; the swarm
is a NON-RUNNING SKELETON. The gap is integration.

## Iteration levers (from the round-0 contract-gap review)

See round0/CONTRACT_REVIEW.md for the detailed worker's-eye findings.
