# floor-devstral-20260820 — the FLOOR anchor (promoted 2026-08-20)

Replaces `floor-devstral-20260803` as the ladder's bottom rung by
operator ruling. The retired anchor stays in the tree as the
comparison record; every flight against it is still valid history.

## Provenance
- Source: `tier_20260820-075618/staged-continuation/alpha`, byte-identical
  copy (verified `diff -r`). Charged as it ships, litter included:
  `tests/` runs 4-of-6 red.
- Producer: devstral-2-small-24b, SESSION structural
  (`game_challenge_tier_session`), top-phase quality. A 2h arm (10/31,
  stalled ~100 min in the module_statement prompting gap) plus an
  operator-directed 100-min continuation after the gap was fixed
  (c0417a8), which unstuck it on the FIRST lap and finished at
  **16/31 gate-verified goals**. The continuation seam is recorded in
  that run's MANIFEST.
- Framework: the 2026-08-20 stack — collapsed diagnose / uncapped
  investigation (8e1e1cf), role-not-reply verification (dd7af76),
  the sealed boss-consult provider (9ea48a1), and c0417a8.

## Why it is the floor
Blind flight vs `floor-devstral-20260803`: **Delivery 1-3 · Character
3-3 · OVERALL this artifact, SELF-FLAG: CLOSE.** The judge rested the
call on the two facts the rubric elevates — completability **WON**
(The Dark Sovereign killed at 5 HP; the retired anchor is UNWINNABLE,
its boss in a component nothing links into) and **placement clean
8/8** with every entity placed, against the retired anchor failing
both placement forms at once. Record:
`dev/blind_panel/records/flight_20260820_continuation_vs_floor.md`.

**The operator's promotion reasoning, recorded because the margin was
CLOSE:** the axes that moved are DESIGN-phase axes, and they moved in
both devstral runs of the day. Taking the WON artifact keeps those
solidly-moved axes represented in the anchor rather than leaving the
floor a pre-change artifact that the current framework dominates on
design while execution differences merely measure other model axes.
A floor that lags the framework weakens its own integrity as a
reference.

## Mode is load-bearing for this model
Same model, same framework, same wall, mode the only variable:
- SESSION (this artifact, 16/31) beat the retired floor (CLOSE).
- BATCH (`tier_20260820-155728`, 12/31) LOST to it 0-4 / 3-3.
Record: `dev/blind_panel/records/flight_20260820_batch_vs_floor.md`.
The session walk shipped a connected 8/8 world; the batch arm shipped
a one-way orphan, an unplaced NPC and an unplaced chest. Any future
devstral floor refresh should run SESSION structural or say why not.

## Known defects (charged in the flight; carry into any future use)
- **id-only parser**: every noun verb compares the bare snake_case id,
  never the display name it prints (`take Rusty Broadsword` fails;
  `take rusted_broadsword` works — not even a case transform). `look`
  lists no exits, items, monsters or NPCs. The judge: "a source-blind
  player is stopped on turn one" and won only with world.yaml open.
- **`save` hard-crashes**: `engine.py` calls `save_game` without
  importing it — NameError, process dies. `load_game()` is a declared
  stub returning None; `continue` at the title is dead code.
- **No terminal states**: the win prints no victory screen and play
  continues; death continues at negative health.
- The locket weakness never fires (`monster.name.lower() == "boss"`
  compares a display name to an id) — the four-part weakness arc is
  authored and inert.
- `move north`, the first line of its own help, cannot work; only bare
  direction words move.
- Healing potion never consumed; `take` never removes from the room;
  `new` does not rebuild the world.
- `tests/` ships 4-of-6 failing, asserting the broken `move north`.

## The flip condition, in the judge's own terms
"A judge who weights source-blind playability above delivered-and-
reachable scope should call this A" (the retired anchor). That is the
live objection to this promotion and it is on the record deliberately:
this anchor wins on reach and coherence, not on being pleasant to play
blind.
