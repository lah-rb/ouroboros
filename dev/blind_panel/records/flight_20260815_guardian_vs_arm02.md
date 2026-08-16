# GUARDIAN FLIGHT — muse-glimmer-30b arm02 (session base + quality resume leg)

*Dispatched 2026-08-15 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
tier_20260815-115356/staged/arm02 (CANDIDATE — muse-glimmer-30b, 18-min
structural session walk + 1h45m quality resume, 24/42 goals at the wall);
B = dev/blind_panel/anchors/v2.0/guardian-gptoss-20260803/alpha (GUARDIAN).
Candidate on A (rotation honored after sitting B in the session-vs-batch
flight). Packets scanned clean. Judge's verbatim record follows.*

---

# BLIND COMPARATIVE FLIGHT RECORD — TIER_RUBRIC v2.1

Packet: `/Users/lah-rb/ouroboros-runs/flight_20260815_guardian_vs_arm02`
Both artifact trees verified byte-identical after judging (played from `scratch/A`, `scratch/B` copies).

---

# ARTIFACT A

## Premise line
A drowned lighthouse crawl: you climb the Sable Spire from a salt-scoured courtyard to a sealed lantern room where a waterlogged guardian stands in a beam that no longer shines, and the thing that kills it is not a blade but a lens that focuses the sun. The whole game is one argument — light, not steel — planted by two NPCs, echoed in the boss's own immunity message, and paid off at the top of the stairs.
Quoted prose: *"Ah, you found the keeper's blade. It won't be enough alone. You need light, not steel."*

## Completability class: **WON**

```
> You hit Drowned Warden for 30 damage. HP 0/80
You defeated Drowned Warden!
The lighthouse is freed. You win!
```

Route: `east` → take/equip Rusted Cutlass → `north` → `east` → take/equip Salt-Stained Leather Coat → `south` → take Vial of Brine-Medicine → `north`, `west` → take Sunshard Lens → `north`, `north` → 3× `attack`.
**Reading the source was NOT required to win.** Both NPCs and the boss itself teach the rule in play: *"The Warden fears true light. Find what focuses the sun."*, *"The Drowned Warden is immune to steel in the first phase. Only sun-focused light breaks it."*, and on the first swing without the lens: *"Drowned Warden is immune to your attacks in phase 1!"*

## Room graph and placement
**8 authored rooms, 8 reachable from the start room** (`entry_courtyard`) — all eight visited in play. **No disconnected component. No unplaced entity**: all 5 items, both NPCs and all 4 monsters carry a room placement and were found in play.
Graph hygiene note: three exits are one-way (`spiral_stairwell.east → keepers_quarters` with no return east; `lantern_gallery.north → spiral_stairwell` but `spiral_stairwell.south → foggy_dock`; `storm_basement.north → sealed_beacon` but `sealed_beacon.south → spiral_stairwell`). Non-blocking — every room stays reachable — but the map does not survive a mental model.
Inert authored content: `rusted_key`, described as *"An old iron key needed to open the Sealed Beacon door"* — no door exists; the beacon is freely enterable. Authored intent not wired. *(model-innate)*

## Conformance: **44 / 47 — NEAR-FULL**
Unmet: **11, 16, 33.**
- **11 (`flee`)** — absent from the tree as a player verb. `grep -rn flee A/artifact/` returns only a monster-side message, a YAML description, and the README's claim. In play: `flee` → `Unknown command.` *(model-innate)*
- **16 (help lists the real vocabulary)** — help advertises `move north/s/e/w`; `move north` returns `Can't go that way.` It omits `status` and `choose`, both of which work.
- **33 (defeat screen offers a restart)** — no `restart`/`play again`/`new game` anywhere in the tree; death prints one line and the process exits.

Trigger note: the binary does **not** fire. 3 unmet is far under 10, and the core loop is intact and reached — terminal states both exist (win observed, defeat observed), the two-phase boss, the weakness chain, and JSON save/load are all present and working.

## State integrity
Strong. Save is a full world snapshot and round-trips across processes:
```json
{"player_location":"lantern_gallery","inventory":["sunshard_lens"],"equipment":{"weapon":"rusted_cutlass"},
 "stats":{"health":82,...},"rooms_items":{...,"lantern_gallery":["rusted_cutlass"],...},
 "monsters_defeated":{"barnacle_crab":true,...},"npc_dialogue_progress":{"old_mariner_finn":"boss_hint"}}
```
Verified on reload in a fresh process: the dropped cutlass is still on the gallery floor, the Barnacle Crab is still dead at Foggy Dock, and Finn resumes mid-tree (*"The Warden fears true light…"*) instead of at `start`. An old save loads cleanly against a world extended with a ninth room.
**Blemish:** dropping an equipped weapon leaves it *both* on the floor and equipped-and-effective — `Inventory: Sunshard Lens / Equipped: weapon: Rusted Cutlass` while `look` shows `You see: Rusted Cutlass`, and it still deals its damage. Item duplication. *(model-innate)*

## Robustness battery (10 probes)
| # | probe | result |
|---|---|---|
| 1 | unknown command (`xyzzy`) | `Unknown command.` — clean |
| 2 | empty line | `Unknown command.` — clean |
| 3 | **whitespace-only (`"   "`)** | **TRACEBACK, process dies** — `IndexError: list index out of range` at `parser.py:120 action = parts[0]` |
| 4 | verb with no argument | clean, if generic: `Can't go that way.` / `No such item here.` / `Nothing to attack here.` |
| 5 | invalid direction (`go up`) | `Can't go that way.` — clean |
| 6 | nonexistent target (`take lens`) | `No such item here.` — clean refusal, but no partial matching |
| 7 | wrong monster name (`attack goblin`) | **silent misinterpretation** — attacks the room's actual monster anyway |
| 8 | EOF at prompt | clean silent exit, rc 0 |
| 9 | EOF mid-combat | clean — combat runs in the main loop, no sub-prompt to trap in |
| 10 | missing / corrupt save | `Load failed: [Errno 2] …` / `Load failed: Expecting value: line 1 column 1` — clean |

**Worst impact: a hard traceback that kills the process on whitespace-only input** — one stray space bar from the main prompt, with no save on the way out.

## Modification probe (B9)
**Clean pass, both halves, data-only.**
- Ninth room: added `tidal_grotto` plus one `east:` line to `storm_basement` in `world.yaml` — 11 lines of data, **zero Python touched**. Worked first try, bidirectional, correct exit listing, and a pre-existing save still loaded against the extended world.
- Weapon damage: `damage: 5` → `25` on `rusted_cutlass`, one line. Damage went `10` → `30` per swing. Nothing else broke.

## Furthest point reached
The win screen, from a clean boot, with no source access. Also drove the full alternate branch (boss without the lens → phase-1 immunity → death).

## Attribution ledger
- Whitespace crash, missing `flee`, help/`move` mismatch, case-sensitive NPC matching, drop-duplication, unwired `rusted_key`, missing phase-2 narration — **model-innate**.
- Ships 3 tests, 4/5 assertions pass, **one shipped failing test** (`test_wardens_echo_dialogue_branches_on_sunshard_lens`: `npc_dialogue_progress must remain a string node id … assert isinstance(None, str)`). Real self-verification effort, shipped with a red light on. **Interaction** — recorded, charged, not excluded.
- Project layout — framework-coupled, excluded per §4.

## Decisive quoted findings
Two-phase boss + weakness chain, both halves observed:
```
> Drowned Warden is immune to your attacks in phase 1!
You hit Drowned Warden for 0 damage. HP 80/80
```
Three genuinely distinct monster scripts, observed not just declared — the crab double-strikes once wounded (`Barnacle Crab hits you for 6 damage. HP 88/100` / `HP 82/100` in one round), the rat swarm adds a poison tick, the keeper's ghost is authored to flee at half HP. Armour is a real defence stat: the Warden hits for `10` bare and `6` through the Salt-Stained Leather Coat.
Inventory-conditional dialogue works: carrying the lens re-routes the Warden's Echo straight past its opening node to the payoff line.
Against it — the prompt lies about its own input:
```
  1. Ask about the lighthouse
  2. Ask about the boss
Choose an option number or type 'choose <n>'.
> 2
Unknown command.
```
Bare `2` is refused; only `choose 2` works. A player recovers by trying the second half of the sentence, so I score it a nuisance, not a wall — but the prompt is false as printed. And `talk to old mariner finn` fails where `talk to Old Mariner Finn` succeeds: **A will not accept, in lowercase, the name it prints.**

---

# ARTIFACT B

## Premise line
A generic keep-crawl: a hall, a corridor, an armoury, a garden, a library and a throne room, with a goblin, an orc and a Shadow Dragon at the end, and an Obsidian Key that two NPCs tell you to find. It reaches for the same shape as A — two-phase boss, weakness item, equipment slots — but ships six rooms of it and no ending.
Quoted prose: *"Beware the darkness ahead, traveler. The key to the dragon's heart lies hidden."*

## Completability class: **UNWINNABLE** (both by absence and by arithmetic)

Three independent reasons, each sufficient:

**(a) There is no win path anywhere in the tree.** `grep -rniE "win|victor|congrat|triumph"` across all of `*.py *.yaml *.md` returns exactly one hit — `print("You see the following items:", …)`. Killing the Shadow Dragon would print `The Shadow Dragon collapses!`, identical to a goblin, and drop you back at the prompt. This is absence, not unreachability, and it is precisely the asymmetry CHECKLIST.md's own item-31 note tells judges to watch for.

**(b) THE SEAM BUG — and yes, it is what stopped me.** `game.py:742`:
```python
# Check for weakness item in inventory
if self.equipment["weapon"] == "boss_key":
```
The comment says *inventory*; the code reads the **weapon slot**. `boss_key` is `type: key` in `world.yaml`, and the equip handler refuses it — in play, `equip Obsidian Key` → `That item can't be equipped.` The weakness bonus can therefore **never** fire under any sequence of player actions. This is verbatim the campaign's predicted class: a boss gated on an equipment slot its own item type can never occupy. Two files each internally reasonable — the item block sensibly types a key as a key; the combat block sensibly checks what you are wielding — and no third file reconciles them.

**(c) The arithmetic closes it anyway.** Ceiling is base attack 1 + Rusty Sword 3 + Iron Shield 2 = **6**; max health is **20** with a single Healing Potion that caps at 20 (`You use the Healing Potion and recover 8 health` at 12 HP). The dragon has 50 HP and hits for 8, rising to 12 in phase two. Flee-cycling is free and monster HP persists (verified: `44` → re-engage → `38`), but the total damage a player can absorb across the whole game is ~40, buying ~5 rounds at 6 damage. **~30 output against 50 HP.**

**Furthest point reached:** Throne Room, dragon at 32/50, dead on the floor.
```
> You strike for 6 damage. Shadow Dragon health is now 32.
The Shadow Dragon slashes you for 8 damage. Your health is now -4.

You have fallen in battle. Game over.
```
I read the source only *after* observing the failure — carrying, using and trying to equip the key all did nothing in play first.

## Room graph and placement
**6 authored rooms, 6 reachable** (`start → corridor → {armory, garden, library → throne}`). No disconnected component — but the brief asks for eight, the README claims eight, and there are six.
**UNPLACED ENTITY: `amulet` / "Silver Amulet"** — fully authored in the items registry (`type: key`, description, id) and present in **no room's item list**. One of the five items can never be seen by any player.
Also inert: behaviour class `coward` is implemented in the combat loop and assigned to no monster.

## Conformance: **41 / 47 — SIGNIFICANTLY-DEVIATED**
Unmet: **22, 23, 31, 33, 38, 45.**
- **22** — healing mid-combat does not exist in the tree. The combat loop (`game.py:678-750`) has branches for `flee` and for attacking, and nothing else. `use Healing Potion` inside a fight is silently executed as an attack.
- **23** — three regular monsters requested; two authored (Goblin Guard, Orc Warrior) plus the boss.
- **31** — **no win path exists in the code.** ← core-loop element
- **33** — no restart. Death does `print(...)`, `write_save()`, `exit(0)`.
- **38** — 6 rooms, not 8.
- **45** — both NPCs ship a single fixed line and `triggers: {}`. The node-walking machinery exists (`game.py:633-639`); no branch was ever authored, so nothing branches anywhere in the shipped data.

**Binary trigger: CORE LOOP, not count.** 6 unmet is under the 10 threshold; item 31's absence fires it on its own.

## State integrity — **the worst finding in either artifact**
B has no `save`/`load` commands. It auto-saves on quit and auto-loads on boot, silently (`Loaded saved game.`), from a `savegame.json` the game never names.

**1. Death is permanent and the game bricks itself.** `initiate_combat` on player death calls `write_save()` *before* `exit(0)`, writing `"health": -1`. The next boot silently restores it:
```
=== SAVE AFTER DEATH ===   {"player": {"health": -1, "attack": 1}, ...}
=== RESTART FROM DEAD SAVE ===
> Health: -1
Attack: 1
Location: Long Corridor
```
A player who dies once resumes dead, forever, with no restart command and no in-game way to reset — only deleting a file the game never mentions.

**2. Load rewrites the world.** `load_state` restores `defeated_monsters` but never removes them from the rooms, and monster HP is not in the save schema at all. Verified: killed the Goblin Guard, quit, relaunched — `Danger! Monsters present: Goblin Guard, Goblin Guard`, back at full health. Every reload resurrects everything you killed.
**3.** `npc_progress` is saved and always empty, because no NPC has a second node.

## Robustness battery (10 probes)
| # | probe | result |
|---|---|---|
| 1 | unknown command | `I don't understand that command.` — clean |
| 2 | empty line | silently ignored — clean |
| 3 | whitespace-only | silently ignored — clean |
| 4 | verb with no argument | **best in the flight**: `Go where?` / `Take what?` / `Attack what?` / `Use what?` / `Examine what?` |
| 5 | invalid direction | `You can't go that way.` — clean |
| 6 | nonexistent target | `No such monster here.` / `No such item here.` — clean |
| 7 | **mid-combat non-combat command** | **SEVERE silent misinterpretation** — `help`, `status`, `quit`, `xyzzy`, `use Healing Potion` are *all* silently executed as attacks. `flee` is the only recognised word. You cannot quit, heal, or check your HP inside a fight, and each attempt costs you a turn of damage |
| 8 | EOF at prompt | `Exiting game.` — clean |
| 9 | **EOF mid-combat** | **TRACEBACK, process dies** — `EOFError: EOF when reading a line` at `game.py:680` |
| 10 | corrupt / missing save | clean; falls back to a new game |

**Worst impact: the combat sub-loop.** The prompt is `"> "` — byte-identical to the main prompt — with no indication that the vocabulary just shrank from fourteen commands to one. The player has no signal that anything changed, and the game's own help is flatly false for the duration of every fight.

Plus a permanent display corruption, from a second cross-module seam: `world.py` populates `room.monsters` from the YAML room's `monsters:` list (line 127) **and then again** from each monster's own `location:` field (lines 133-135), with no dedup. Every monster is listed twice, forever, and killing one removes only one entry:
```
> The Goblin Guard collapses!
> attack Goblin Guard
You engage the Goblin Guard!
> look
Danger! Monsters present: Goblin Guard
```
A corpse you can "engage" that then does nothing, in every cleared room, for the rest of the run.

## Modification probe (B9)
**Room addition: clean pass, data-only.** Added a `grotto` room plus a `west:` exit in `world.yaml` — no Python touched, worked first try.
**Weapon damage: clean pass.** `attack: 3` → `30` on `sword`; `Attack: 31`, `You strike for 31 damage`.
**What it cost to work in:** `game.py` is a single 809-line / 30 KB module holding the loop, every command handler, room rendering, the combat sub-loop and save/load. Finding the boss-weakness gate meant reading past four hundred lines of dispatch. The loader's double-registration is exactly the kind of seam a maintainer inherits and cannot see from either end. No tests ship.

## Attribution ledger
- The `boss_key` weapon-slot seam, the missing win path, the monster double-registration, the dead-state save, the combat sub-loop, the 6-room world, the unplaced Silver Amulet, the unused `coward` behaviour — **model-innate**.
- **Interaction:** `[project.scripts] text-adventure = "main:run"` against a `main.py` that defines `main()`, not `run()`. The README's *first* documented way to launch the game is wired to a function that does not exist. Also declares `dev = ["ruff", "black"]` and a `ruff check .` workflow with no evidence of either having been run, and no tests. Charged, per CHARGE WHAT SHIPS.
- Project layout — framework-coupled, excluded.

---

# THE TEN FORCED CHOICES

## PANEL A — DELIVERY

| axis | choice | justification |
|---|---|---|
| **A1 working surface** | **A** | A's advertised surface works down to the win screen; B's central mechanic — combat — accepts one of the fourteen commands its own help lists, its boss weakness can never fire, and its documented entry point calls a function that does not exist. |
| **A2 state integrity** | **A** | A round-trips location, stats, equipment, dropped items *by room*, defeated monsters and dialogue nodes across processes; B resurrects every monster you killed on each load and, on death, saves `health: -1` and reloads it, permanently bricking the game. |
| **A3 robustness** | **A** | Each has exactly one traceback (A: whitespace at the prompt; B: EOF in combat), and B's out-of-combat refusals are the better-written of the two — but B adds pervasive *silent misinterpretation* inside the core mechanic, where `quit`, `help` and `use Healing Potion` all become attacks, which the rubric ranks as the worst class of the three. Closest axis on this panel. |
| **A4 delivered scope** | **A** | 8 rooms / 5 placed items / 2 multi-node dialogue trees / 3 behaviourally distinct monsters / working two-phase boss / a reached win / save-load / 3 tests, against 6 rooms / 4 reachable items / 2 fixed lines / 2 regular monsters / no win / no restart / no tests. |

## PANEL B — CHARACTER

| axis | choice | justification |
|---|---|---|
| **B5 ambition** | **A** | On the design each set out to build and ignoring what survived: both attempted a two-phase boss with a weakness item, but A also reached for a separate defence stat, three individually-named combat routines (double-strike-when-wounded, flee-at-half-HP, poison tick), multi-node dialogue graphs *plus* inventory-conditional re-routing, and an authored key-and-door — more distinct mechanisms attempted than B's single-stat equipment, one-line NPCs and auto-persistence. |
| **B6 imagination** | **A** | A has a *non-obvious idea* — the weapon is a lens, light rather than steel — planted by a dead keeper's echo muttering *"the lens is hidden where paint is false"* and paid off by the boss's own immunity line; B is Entrance Hall, Long Corridor, Armory, Goblin, Orc, Dragon, and a banner that never names the game. |
| **B7 experience (felt play)** | **A** | A's world teaches its own rule in play and the win is earned by connecting a hint to a hidden object; B sends you after a key that sits in the boss's own room and then does nothing when you carry it, use it, or equip it, with no ending to reach even if it worked. Charged against A at full weight: its dialogue prompt asks for a bare number and refuses it, and it rejects `old mariner finn` in the lowercase it never prints — I got past the second by copying the room text exactly, which a player can also do. |
| **B8 craft (UI)** | **B** | B's help is a formatted, accurate fourteen-command table where A's is a run-on line advertising a dead `move` and omitting a live `status`; B's argument-less errors (`Go where?`, `Attack what?`) are the best surface in the flight; and the axis's own named mechanism — *will not accept the names it prints* — fires against A, which refuses `old mariner finn` and `SUNSHARD LENS`, while B is case-insensitive throughout and carries real synonyms (`inv`, `stats`, `get`, `hit`, `pick up`). B loses ground on a status block with no max HP and a `Danger!` line that double-counts every monster and keeps listing corpses, but the surface a player types against is B's. |
| **B9 workability** | **A** | Both mod probes passed as pure data edits, so the axis turns on organisation: A splits models / loader / parser / state / engine / main with a typed data model and ships tests; B is one 809-line `game.py` holding the loop, every handler, combat and persistence, with a loader that registers monsters from two sources at once and a `pyproject` entry point pointing at a nonexistent function. |
| **B10 documentation** | **A** | Accuracy against play first: A's README makes one false claim (`flee`) and is otherwise verified true, including its 8-rooms / 5-items / 2-NPCs / two-phase-boss inventory; B's makes four — "8 rooms" (there are 6), "branching dialogue" (one fixed line each), `text-adventure` as the headline launch command (broken), and "see the LICENSE file" (no LICENSE in the tree). |

---

# TALLIES

**Delivery: A 4–0 · Character: A 5–1**

**OVERALL: A**

No PANEL SPLIT — both panels point the same way, and B's single axis is B8, its genuine strength.

**No CLOSE flag.** The margin is not a matter of polish: B has no win condition anywhere in its tree, its boss weakness is gated on an equipment slot its own item type is refused from occupying, and one death permanently bricks the save it silently auto-loads. A was played to its win screen from a clean boot without source access.
