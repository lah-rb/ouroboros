# Contract-swarm A/B — iteration reference

## round4 (2026-07-17, code 701de84) — FIRST BOOTING SWARM; frontier → data↔data

Levers (both in the round-4 commit): (1) **data-exemplar broadcast** —
`DataShapeContract.example` (a literal minimal instance the architecture
already carries) is broadcast to EVERY worker (`_worker_prompt`: "index
ONLY these keys") AND appended to each data file's own generation
directive (`_enrich_data_goals`, idempotent+persisted), so readers and
the file bind to one exemplar; (2) **flow fix** — the create-loophole
backfill now skips infrastructure files (`is_infrastructure_file`,
shared with the drift detector), so an unplanned `__init__.py` can't
strand the sweep.

Result — **the swarm produced a BOOTING, PLAYABLE program for the first
time** (rounds 0–3 never launched):
- Structural **11/11 goals complete**, reached the environment boundary
  cleanly (round 3 died at 9/10) → first clean judge since round 2.
- Cross-module type check **6/6 clean**; the assembled game launches
  (`python main.py`), and `help`/`look`/`go <dir>`/`status`/`take`/
  `talk to <defined-npc>` all work — real movement across a data-driven
  world, no loader `KeyError`. The **code↔data SHAPE drift is CLOSED**:
  the exemplar bound `world_loader`'s key access to the yaml shape.
- This round's design chose a FLAT layout with FOUR data files
  (rooms/items/npcs/monsters.yaml), so the `__init__.py` case didn't
  recur — but the fix is in and the sweep reached the boundary.

Blind judge (1 judge, both builds RUN): **batch (B) preferred — 4th
straight batch win**, but the closest round yet and both builds have a
blocking defect:

| axis | swarm r4 (A) | batch baseline (B) |
|---|---|---|
| runs | 3 | 2 |
| coverage | 3 | 5 |
| cohesion | 1 | 4 |
| quality | 3 | 4 |
| play | 1 | 4 |

- Swarm launches out-of-the-box but crashes on normal play; batch is a
  complete coherent game (save/load round-trips, narrated combat, boss
  phases) that **doesn't launch as delivered** (relative-import +
  hardcoded `src/world.yaml` + broken entry point — a near-trivial fix),
  after which it plays flawlessly. Its only gameplay flaw: unwinnable
  boss (silver_key can't be equipped) — same class as round 0.

**The frontier advanced exactly one joint — now data↔data referential
integrity** (verified against the canonical output, not just the judge):
the 4 data files were each generated INDEPENDENTLY by serial fallback,
each with its own exemplar but NO shared id registry, so each invented
its own id namespace and they diverged — the telephone game at the DATA
layer. `rooms.yaml` references 7 item ids (`apple`, `steel_sword`, …) of
which **7/7 are dangling** (items.yaml uses a disjoint set:
`sword_of_dawn`, …); 2/4 npc refs dangle (`merchant`/`armorer` absent
from npcs.yaml). So placed items are inert and `talk to merchant` hard-
crashes with `KeyError`. The exemplar broadcast fixed the SHAPE (which
keys exist) but not the cross-file VOCABULARY (which entity ids exist) —
the same class round 3 closed for code, one layer down.

Two smaller residuals: (a) the serial-fallback `engine.py` (a MISSING
file, not a swarm-worker artifact) has a broken save/load — `json` never
imported + the loaded dict is never rehydrated to a player object; the
serial-fallback file is the weak link AGAIN (round 2 it was engine arity,
round 4 it's engine save/load); (b) a **residual splice sentinel** leaked
into shipped code as a comment (`# ⟦OUROBOROS-SYMBOL load_world⟧ …`) —
harmless (a `#` line) but a real assembly-hygiene bug: a worker echoed
the frame guidance and `splice_frame` kept it.

Cost: swarm **46,130 gen tokens** (4 data files + revisions, up from
round-3's ~30k) vs batch's 7.8k ≈ **6×**. Batch still wins on cost,
cohesion, and playability for THIS problem class (small, tightly-coupled)
— but the swarm now BOOTS and the coordination mechanism is visibly
converging joint-by-joint.

**Round-5 direction:** apply the round-3 broadcast principle to the DATA
layer — a **shared entity-id registry** as one broadcast artifact every
data-file generator binds to (the canonical room/item/npc/monster ids,
so cross-file references resolve), OR generate the coupled data files
together (one author) instead of independently. Secondary: route
serial-fallback files through the same contract/gate rigor as swarm
workers (the fallback file has been the weak link in 2 of 3 booting-
blocked rounds), and drop the leaked splice sentinel. The tipping-point
question — at what upfront-design rigor does cohesion hold, and is 6×
the cost worth it, and where — stays open, but the answer is sharpening:
the swarm's niche is loosely-coupled work where the batch can't fit one
context; on the tightly-coupled game the batch still wins, yet each round
the swarm closes another coordination joint.

## round3 (2026-07-17, code 0f94885) — broadcast MOVED THE NEEDLE

Lever (isolated): `_project_digest` broadcasts every module's full-shape
`.pyi` to EVERY worker (not just imports) — the conference call on the
shared vocabulary. Result — the FIRST lever to break the recurring
pattern:
- **Cross-module type check: 8/8 files clean** (round 2 was 6/7). The
  code↔code interface drift that crashed rounds 0–2 (undefined methods,
  arity, wrong attributes) is GONE — workers no longer re-derive the
  shared API; they read the same broadcast.
- Assembly 8/8 (round 2 was 7/8), only 1 worker retry.

But it still doesn't boot, for a DIFFERENT residual on a new axis:
- **Residual 1 — code↔DATA drift:** `loader.load_world` reads
  `mon["max_health"]` but `world.yaml`'s monsters have no `max_health`
  → boot `KeyError`. `world.yaml` is a DATA file built by SERIAL
  FALLBACK with a content brief — it is the one participant NOT in the
  conference call, so the code's key expectations and the data file
  drift. The type check is blind here (untyped dict-key access; data
  files have no type contract).
- **Residual 2 — a FLOW bug (not paradigm):** the nested-package design
  produced a `src/__init__.py` structural goal the sweep can't resolve
  (empty package-init, no contract) → `check_phase ↔ structural_sweep_next`
  looped to the 100-step cap, errored, and the outer 51×-no-dispatch
  guard killed the run at structural 9/10 — before the environment
  boundary, so NO clean judge this round. Package-init handling in the
  sweep needs a fix for clean measurement.

**Read:** the tipping point is advancing exactly where predicted — the
shared-vocabulary broadcast tipped code-interface cohesion (the joint
that broke every prior round). Two things now stand between the swarm
and a booting program: the data file is outside the contract (code↔data
drift), and a flow bug on package-init goals blocks the run. This
vindicates continuing.

**Round 4 direction:** (a) extend the shared contract to the DATA schema
— the data file's shape becomes a broadcast artifact both the loader
worker and the world.yaml generator bind to (closes code↔data drift);
(b) fix the sweep's empty-package-init loop so a round can reach the
environment boundary for a clean judge; (c) the deferred contract-bound
serial fallback still pending. Cost curve so far: swarm ~30k+ gen tokens/
round vs batch 7.8k — the "is it worth it" question stays open until a
round boots.

## STATUS (2026-07-17): CONTINUING — coordination frontier advancing

Superseding the earlier "pause" call (which was premature — Luke's
correction: prove/falsify the coordination mechanism at inspectable
small scale BEFORE scaling, and the consistently-decent per-symbol code
says the ceiling is COORDINATION, not capability). Rounds 0–2 lost the
blind judge 3/3 to the batch build (which also costs ~4× fewer tokens),
but the SHAPE of the losses is the finding: per-symbol quality is fine;
INTEGRATION fails, and each lever advances the cohesion frontier to the
NEXT weakest joint rather than failing flat. Round 3's broadcast broke
the recurring code↔code drift entirely (8/8 type-clean) — the frontier
is now the code↔data-file contract + a flow bug, not the code interfaces.
This is a tipping-point search (at what upfront-design rigor does
cohesion hold, and is it worth the cost), NOT a closed verdict.

Per-round history — each round a DIFFERENT joint, frontier advancing:

| round | lever | judge (swarm runs/cov/coh/qual/play) | boot-crash cause |
|---|---|---|---|
| 0 | baseline | 1 / 1 / 1 / 2 / 1 | placeholder main; broken import; divergent GameState ctors |
| 1 | .pyi digest, worker validation, import-completeness, author dispatch/attr rules | 1 / 1 / 1 / 2 / 1 | data-shape drift (load_world dict vs .id objects) + broad seam mismatch |
| 2 | + deterministic cross-module AST gate + dict-shape rule | 1 / 1 / 1 / 3 / 1 | run_engine arity — worker-FAILED engine.py went to off-contract serial fallback, AFTER the gate ran (coverage hole) |
| 3 | + full shared-interface BROADCAST to every worker | (no judge — flow bug) | code↔DATA drift (loader wants max_health, world.yaml lacks it); flow bug on src/__init__.py sweep loop |
| 4 | + data-exemplar broadcast (readers+file bind to example) + infra-backfill flow fix | 3 / 3 / 1 / 3 / 1 (batch 2 / 5 / 4 / 4 / 4) — **first BOOTING swarm** | data↔DATA referential integrity (4 independently-generated data files, disjoint id namespaces → dangling room→item/npc refs); serial-fallback engine.py save/load bug |

Batch baseline stayed ~2–3 / 3 / 4 / 4 / 3 throughout (a coherent,
mostly-playable game with real bugs — unwinnable boss, atomic combat).

**The frontier is advancing, not stuck:** round 3's broadcast is the
first lever to eliminate a whole failure class (code↔code interface
drift: 8/8 type-clean, up from 6/7). The remaining gap moved to the
code↔data-file contract (the data file is built off-contract by serial
fallback — outside the "conference call") and a flow bug. See round 3
above for the round-4 direction.

Cost stays the open question: swarm ~30k+ gen tokens/round vs batch's
7.8k, and no round has booted yet — so "at what upfront-design rigor
does cohesion hold, and is it worth the cost, and where" is still being
measured. The paradigm's eventual niche is likely where batch CAN'T
apply (projects too large for one context, or loosely-coupled work), but
that's not yet proven; the small tightly-coupled game is the hard case
we're using to find the mechanism first.

Infra (committed, tested, code_core untouched): the flow set, `.pyi`
digest + project broadcast, worker validation, the cross-module AST gate
(`_check_module` / `action_run_contract_typecheck`).

---


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
