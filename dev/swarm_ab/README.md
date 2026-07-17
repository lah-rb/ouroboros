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

## round1 (2026-07-17, code b33027c) — prevention levers; batch still wins

Levers applied (all in contract_swarm_actions.py + author_instruction, no
flow change): (1) `_dep_digest` renders a `.pyi` view (class fields +
ctor + method sigs) instead of bare `class X:`; (2) worker import check
`tree.body`→`ast.walk` + reject off-contract class methods; (3)
parse-time import-completeness vs architecture `imports_from`; (4)
author instruction: explicit dispatch table + complete entry imports +
attribute discipline. Structural stop unchanged.

Swarm generation: contracts 6/6 parsed but 4 issues PERSISTED across both
revisions (author couldn't resolve); 16/16 symbols, 6 workers, 217s, 2
retried; assembly 6/6; doctests 0/6 at assembly. ~31.8k gen tokens.
Design chose a NESTED PACKAGE layout this round (`adventure/core/...`) vs
round-0 flat — a nondeterministic-planning confound.

Blind judge (1 judge): **BETA (round-0 code_core baseline) preferred,
decisively.** Round-1 swarm (alpha) runs 1 / cov 2 / cohesion 1 /
quality 2 / play 1; baseline (beta) runs 3 / cov 3 / cohesion 4 /
quality 4 / play 3.

**What round 1 fixed (real):** the placeholder `main` is gone — main now
wires `load_world → GameEngine → engine.start()` with complete imports;
workers stopped inventing constructors where the digest gave them the
shape.

**What remains (dominant, reframed):** the assembled program still
crashes at startup and the whole module-seam layer is mis-wired —
`load_world` returns raw dicts but `GameEngine` expects `.id` objects;
`Command.verb` vs engine's `cmd.name`; `CombatEngine()` arity; engine
calls `fight()`/`attempt_flee()` that don't exist; `room.exits` vs the
model's `connections`. These are **contract SELF-INCONSISTENCIES at the
author level** (module A's contract calls B.foo() while B's contract
declares B.bar()), which:
- lever 1 (type shapes to workers) CANNOT fix — workers faithfully
  implement an inconsistent contract;
- the fail-open LLM cohesion reviewer misses.

So round 0's "context-starved workers" hypothesis is PARTIALLY confirmed
(workers stopped inventing when given shapes) but the deeper bottleneck
is that the CONTRACT itself is internally inconsistent across modules and
nothing verifies cross-module usage against declared interfaces.

## round2 lever (now dominant — previously deferred)

**Deterministic AST cross-check in `apply_contract_review`** (back the
fail-open LLM reviewer): over the assembled contract set, flag every
cross-module call / attribute access / constructor kwarg / arity that
does not match the target symbol's DECLARED contract (name, params,
fields, methods). Route mismatches to the revision loop. This is the
only mechanism that catches the entire round-1 residual class. Pair with
a contract-format rule that dict-typed cross-module data must declare its
shape (or use typed models) — the `load_world → GameEngine` dict-vs-object
drift. If a deterministic cross-check still can't close the gap, that is
strong evidence contract-mediated coordination can't match shared-context
coherence for this problem class.
