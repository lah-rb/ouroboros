# Round-0 contract-gap review — worker's-eye root cause

Focused audit of WHY the swarm produced a non-integrated program, from
the worker's-eye view (a worker sees only: its own stub, its module
skeleton, its module's full contract, and a SIGNATURE-ONLY digest of
imported modules — no sibling bodies, no whole-program view, no mission
objective). Verdict: **context-starvation by an under-specified
contract**, plus one validator bug and the fundamental per-symbol gate
blind spot.

Two corrections to the earlier panel framing:
- Failure #2 is NOT latent — `main` constructs `GameState()` at startup,
  so the broken import crashes the program on arrival (the mission's own
  smoke command dies before the loop). It doesn't "run and do nothing."
- The doctest gate DID catch state.py + combat.py (their doctests fail
  on the bad import). It's blind on main/save_load/world_loader (no `>>>`
  blocks → `python -m doctest` is a no-op PASS).

## Failure #1 — `main` dispatches nothing (placeholder loop)
**CONTRACT UNDERSPECIFICATION + GATE GAP.**
- The contract specified main's dispatch only as PROSE ("invoke
  appropriate handlers (movement, inventory, combat, etc.)") — no
  verb→callee table. The worker filled the body with a placeholder.
- Worse: main's skeleton imports were INCOMPLETE — `combat` and
  `save_load` imports absent — so the worker was STRUCTURALLY UNABLE to
  call CombatEngine/save_game/load_game (workers may not add imports).
  The author violated its own "complete & final imports" rule.
- The worker never sees the objective, so the command vocabulary
  (go/take/use/talk/attack/flee/look/status/help) was invisible.
- `main` is a REPL → cannot be doctested → no per-symbol gate catches it.

## Failure #2 — `from .player import Player` nested in `GameState.__init__`
**WORKER NON-COMPLIANCE + VALIDATOR GAP.**
- Contract had `GameState` as a `@dataclass` with a zero-arg doctest
  constructor. The worker bolted on a hand-written `__init__` carrying a
  wrong nested relative import (`from .player import Player`) despite a
  correct top-level `from entities import Player`.
- Slipped the "no imports" rule because `_validate_worker_body` checks
  only `tree.body` (top level); the nested import is invisible there
  (`ast.walk` finds it). No retry ever fired.

## Failure #3 — `GameState`/`MonsterState` interface drift across workers
**CONTRACT UNDERSPECIFICATION (dominant) + fail-open reviewer + GATE GAP.**
- SMOKING GUN: the signature-only dep digest for a CLASS carries no
  constructor and no fields — just `class GameState:`. Three workers
  each invented a different constructor (`GameState(world_data)`,
  `GameState(player=,rooms=,...)`, dataclass fields) — none agreeing.
  Save/load can't round-trip.
- `MonsterState` is a genuine contract SELF-CONTRADICTION: combat's
  contract says "Damage = attacker.attack - defender.defense" but
  `MonsterState` has only `health`/`phase`. The cohesion reviewer's
  rubric covers this but it fail-opened.
- combat's doctest uses a self-contained `Dummy` (per the self-contained
  rule) so it never touches real `MonsterState` — self-contained
  doctests structurally cannot catch cross-module type drift.

## Ranked levers for round 1
1. **Expand the dep digest to full class constructor + fields + method
   signatures** (FORMAT: `_dep_digest` / `_stub_signature_index` in
   contract_swarm_actions.py). The single structural root of #3 and
   main's blind `GameState(world_data)` guess — today every consuming
   worker sees only `class GameState:` and must invent the API. repomap
   already extracts the data; this is a rendering change. Highest ROI.
2. **Whole-program integration gate in the structural phase** (new gate):
   run the mission `smoke_command` PLUS a short scripted playthrough
   (quit-only would miss #1's no-op loop), route failures to repair.
   The only thing that can see entry-point wiring, cross-module
   construction, and cross-type attribute use — all invisible to
   per-symbol doctests. Catches #1/#2/#3.
3. **`_validate_worker_body` import check `tree.body` → `ast.walk`**, and
   reject class bodies adding methods not in the contract stub (worker
   validation). One-line + a guard; catches #2 pre-splice via the
   existing retry.
- Honorable mention (PREVENTS #1 rather than detects): author-instruction
  rule that an entry-point/dispatch symbol must enumerate its dispatch
  explicitly (verb→callee), and the entry module's imports must be
  complete.
