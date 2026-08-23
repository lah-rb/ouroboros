# FLIGHT — deepseek-v4-flash COMPLETED artifact vs FRONTIER — 2026-08-23

**Delivery: FRONTIER 4–0 · Character: FRONTIER 6–0 · OVERALL: FRONTIER.**
No PANEL SPLIT. No CLOSE flag. Out-of-band scorecard, NOT ladder-bearing;
METHODS §5 family caveat applies (Opus judge, Claude-authored frontier).

Candidate = deepseek-v4-flash completion run (`completion_deepseek-v4-flash_
20260822-175345` + `_20260823-000222`), 28/28 goals, 62 cycles, ~17h, mission
status `completed` via its own final gate. Candidate sat on **B** (coin flip).
One claude-opus-5 judge, FLIGHT_PROMPT.md verbatim, KEY outside packet root.

## The candidate is genuinely good, and still lost every axis

**46/47 conformance, NEAR-FULL, WON** (twice). 8 rooms authored / 8 reachable,
**zero unplaced entities** — the campaign's most common decisive defect absent
in BOTH forms. Judge needed no source reading to win it. This is not a weak
artifact; it lost to a stronger one on ten forced choices.

Only unmet item: **#26** (monsters have distinct behaviours). `Monster` is a
stats-only dataclass — four monsters differing in four integers, one shared
`_combat_round()`. No behaviour field, no per-monster branch.

## DECISIVE DEFECTS (all model-innate unless noted)

**1. The printed name is untypeable — the classic seam bug, found again.**
`parser.py` strips `_STOP_WORDS = {"the","a","an","to","at","of",...}`;
`world.py` names an item `Handful of Berries`. The middle `of` is deleted, so
cleaned tokens never match the display name:
```
You see: Handful of Berries, Small Health Potion
> take handful of berries    You don't see that here.
> take Handful of Berries    You don't see that here.
> berries                    You take the Handful of Berries.
```
Judge routed around it by GUESSING, then confirmed in the tree — knowledge a
player cannot obtain. Charged at full weight on B7 and B8.

**2. The boss weakness is declared, promised, and never read.**
`world.py`'s docstring states the Warden is weak to the silver medallion;
`models.py` carries `weakness_item` and `phase_2_health`; both NPCs' dialogue
is written entirely around it. **`engine.py`'s combat resolver never reads
`weakness_item`.** The judge **won with the starting Rusted Sword, never
picking up the medallion**, and phase 2 is not fiercer (attack=12 both phases,
every counterattack exactly 9). The whole hint economy points at an optional
item.

**3. `flee` deletes the monster permanently.** `_handle_flee` sets
`room.monster_defeated = True` for every non-boss monster — two of three
regular monsters are skippable by attacking once and fleeing.

**4. The unlocked door does not persist.** Unlock → save → cold reload → the
sealed door is locked again. No door key in the save JSON.

**5. `load` on a malformed save kills the process** — uncaught
`JSONDecodeError`, and `{}` → uncaught `KeyError: 'player'`.

**6. Empty input silently prints the whole help block** (`parse_command` maps
empty → `help`).

**7. Hidden Grove is reachable and completely empty** — no item, NPC, monster
or feature, a one-exit dead end. **It exists to be the eighth room.** This is
the direct product of quality-gate goal #20 ("the demo world contains only 7
connected rooms"): the count was satisfied, the content was not.

**8. The shipped verification surface does not verify — label: interaction.**
`make test` → `Ran 0 tests` (unittest discover against pytest-style bare
functions). Under pytest directly: **2 failed, 4 passed** — the two failures
are the pre-relocation quarantined defeat-screen test driving a `die` command
the parser never had. `requirements.txt` claims stdlib-only while `tests/`
imports pytest. Test filenames truncated mid-sentence.

## What the frontier won on

Nine rooms, three behaviourally distinct monsters (shield cadence, wounded
frenzy, poison-over-time), a poison status effect persisted through save, a
boss whose *resistance inverts into a weakness* for a weapon that is
statistically weaker — you trade your best sword because you listened to an
NPC. Round-trips poison, per-room monster HP, boss phase, NPC stages and
previous location through a cold process. One traceback total (Ctrl-D at the
title menu).

## Narrowest axis — B9 workability (the only one that could flip)

The judge flagged this itself. DeepSeek has the **better module boundaries** —
`models`/`world`/`parser`/`engine` is a cleaner cut than the frontier's
651-line `game.py` god-object. It lost the axis on the modification probe
(2 touchpoints in one file vs 3, the frontier's `add()` helper removing the
forgotten-registration step), on having no extension point for monster
behaviour, and on `make run`/`make test` both failing as written.

## OPERATOR-RELEVANT: three of these were missed by my own verification

- I confirmed goal 17 (two-phase boss + weakness) complete **on the
  framework's word**. The weakness is unwired; the judge proved it by winning
  without the medallion. A goal marked complete is not a verified mechanic.
- I walked the room graph and reported "8 authored / 8 reachable, zero
  unplaced" — correct, and blind to Hidden Grove being **empty**. Reachability
  and placement do not measure whether a room contains anything.
- I never probed a multi-word item name, the single most decisive defect class
  in this campaign's history.

Full record: llmvp/configs/archive/TIER_JUDGEMENTS.md · dev/blind_panel/LADDER.md
