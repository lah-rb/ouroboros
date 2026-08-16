# GUARDIAN FLIGHT — gpt-oss-120b-a5 session arm vs its own batch-era anchor

*Dispatched 2026-08-15 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
dev/blind_panel/anchors/v2.0/guardian-gptoss-20260803/alpha (GUARDIAN — the
batch-era gpt-oss artifact, 08-01, 20/30 park); B =
tier_20260815-150819/staged/arm01 (CANDIDATE — gpt-oss-120b-a5, session walk
+ 08-15 tooling, 28/32 charters at the 2h wall). Same model both sides.
Candidate on B (rotation). Packets built with --arm-identifier, scans clean.

DISPATCHER POST-MORTEM (written after reading the verdict): the candidate's
28/32 was HOLLOW. Its design-derived goal set contained no world-content
charters, and the walk's own world_data.yaml turn generated only 690 chars —
a 2-room stub with five items named Placeholder S/W/O/R/D (the letters of
'sword'), written at the session temp floor on turn 10. The sweep then spent
2h making command mechanics pass against that stub, and an authored test
canonised the runaway victory banner as intended behaviour. Goal-completion
percentages are DESIGN-RELATIVE and must never be compared across arms —
muse's 24/42 beat this Guardian; gpt-oss's 28/32 lost to it. Flights are the
only cross-arm measure. Judge's verbatim record follows.*

---

I have completed the flight. Both artifacts were played first, probed, and modification-tested; source was consulted only to explain failures already observed in play.

---

# BLIND FLIGHT RECORD — TIER_RUBRIC v2.1

Packet: `/Users/lah-rb/ouroboros-runs/flight_20260815_guardian_vs_gptoss_session`
Play copies (writable): `<packet>/scratch/A`, `<packet>/scratch/B`; modification probes in `<packet>/scratch/modA`, `<packet>/scratch/modB`; instrumented boss probe in `<packet>/scratch/probeA`.

---

## ARTIFACT A — RECORD

### Premise line
A six-room stone dungeon that opens on a draughty entrance hall and funnels you north past goblin-held corridor, armoury and library to a throne room where a Shadow Dragon waits on an obsidian key; two hermit NPCs exist to tell you the key is the point. It is a small, legible, honestly-written crawl whose entire arc points at a boss fight the code never finishes wiring.

> *"Weeds choke a once-beautiful garden. A glint catches your eye among the vines."*

### Completability class: **UNWINNABLE**
Proven twice over.
1. **No win state exists anywhere in the tree.** A case-insensitive grep for `congratul|victor|triumph|you win|wins` across `main.py`, `game.py`, `world.py`, `entities.py`, `parser.py` and `world.yaml` returns **0** in every file. This is the exact item-31 asymmetry the checklist warns about: a fully-written defeat path and no win path.
2. **Instrumented confirmation.** In `scratch/probeA` I raised the sword to 60 damage and killed the boss outright:
```
> You strike for 61 damage. Shadow Dragon health is now 0.
The Shadow Dragon collapses!
> Attack what?
> Throne Room
...
Danger! Monsters present: Shadow Dragon
```
The kill produces nothing, and a **second Shadow Dragon is still standing** (see the double-placement seam below).

**Furthest point reached in honest play:** Throne Room, boss engaged, Shadow Dragon down to 32/50 before I died at −4 HP. The fight is arithmetically unloseable-for-the-dragon: 50 HP and 8 dmg/turn (12 in phase two) against my 20 HP, 6 attack, and a single 9-HP potion I cannot drink mid-fight.

### Conformance: 42/47 — **SIGNIFICANTLY-DEVIATED (core-loop trigger)**
Unmet: **22, 23, 31, 33, 38**
- **22** healing mid-combat — the combat sub-loop (`game.py:678-757`) handles only `flee`, else attack. No `use` path exists inside combat.
- **23** three regular monsters — three monsters authored, one of which is the boss; regular count is **2** (goblin, orc).
- **31** win path on defeating the boss — **absent from the tree.** Core-loop element.
- **33** defeat screen offers restart — `print("\nYou have fallen in battle. Game over.")` then `exit(0)`; no restart anywhere.
- **38** eight connected rooms — **6 authored.**

Trigger is **core-loop (item 31)**, not count (5 unmet, well under 10).

### Room graph and placement
- Authored rooms: **6** (`start, corridor, armory, garden, library, throne`). **Reachable from start: 6/6**, one component, no disconnected wing.
- **UNPLACED ENTITY: `amulet` / "Silver Amulet"** — in the item registry (`world.yaml:88-92`), in **no room's** `items:` list. Authored, placed nowhere, unobtainable. All three monsters and both NPCs are placed.
- **SEAM BUG — double placement.** `world.yaml` places monsters twice (each room's `monsters:` list *and* each monster's own `location:` field), and `world.py:134-135` honours both unconditionally: `rooms[monster.location].monsters.append(monster.id)`. Result in play: `Danger! Monsters present: Goblin Guard, Goblin Guard` and **two Shadow Dragons**. Two internally-reasonable conventions, contradicting each other across files.

### State integrity — round-trips the player, **rewrites the world**
A autosaves to `savegame.json` on quit/EOF and silently autoloads on next launch (there is no `save`/`load` command — both are rejected as unknown). Player health, attack, location, inventory, equipment and per-room item lists all round-trip **exactly**. But:
```
"defeated_monsters": ["goblin"]      ← in the save file
...on resume:
Long Corridor
Danger! Monsters present: Goblin Guard, Goblin Guard   ← both back
```
`defeated_monsters` is recorded and never applied to room monster lists on load. **The defeated monster respawns.** This is the A2 failure mode by name. (Corrupt save file: recovers to a fresh game, no traceback — clean.)

### Robustness table (worst impact: **uncaught traceback**)
| probe | result |
|---|---|
| unknown command `xyzzy` | `I don't understand that command.` — clean |
| empty input | no-op, clean |
| whitespace-only | no-op, clean |
| invalid direction `go up` | `You can't go that way.` — clean |
| take/attack/talk/examine/drop/use/equip nonexistent | specific clean refusals for each |
| case `GO NORTH` / `LOOK` | works |
| EOF (Ctrl-D) in main loop | `Exiting game.` + autosave — clean |
| **EOF mid-combat** | **`EOFError` traceback** at `game.py:680` |
| **any input mid-combat** | **silently converted to an attack** — `help`, `xyzzy`, `look`, `quit` all struck the goblin |
| corrupt `savegame.json` | recovers to fresh game — clean |

### Modification probe — **PASSED cleanly**
One edit to `world.yaml` only; no code touched. Added a seventh room `crypt` east of the library, placed the orphan Silver Amulet in it, and raised the sword from 3 to 9.
```
> Ancient Library ... Exits: south, north, east
> You pick up the Silver Amulet.
> Attack: 10          (base 1 + sword 9)
```
Everything took. Content is a genuine data knob.

### Decisive seam bug — is it what stopped me?
Partly. `game.py:742`:
```python
if self.equipment["weapon"] == "boss_key":
```
The boss's 15-damage weakness proc is gated on the Obsidian Key being the **equipped weapon** — but `boss_key` is `type: key`, and `equip` refuses it. Observed in play:
```
> You pick up the Obsidian Key.
> That item can't be equipped.
> You can't use that right now.
```
This is exactly the campaign pattern: *a boss gated on an equipment slot its own item type can never occupy*. **I only learned why by opening `game.py`** — knowledge a player has no way to obtain; both NPCs tell you to seek the key and the game then refuses every way of using it. But the seam is the proximate wall, not the terminal one: even with the proc, there is no win state to reach.

### Attribution
- No win path, 6 rooms not 8, 2 regular monsters not 3, no restart, unplaced amulet, weakness gated on an impossible slot, double placement, mid-combat EOF crash, mid-combat input swallowing, `defeated_monsters` not applied on load — **model-innate**.
- 809-line `game.py` monolith with a stray module-level `def __init__` at lines 784-809 whose docstring asserts the *opposite* of real behaviour ("It no longer attempts to automatically load a saved game" — it does, at line 63) — **model-innate**, a leftover from an in-place edit.
- README's "8 rooms / 3 regular monsters" and the broken console script `text-adventure = "main:run"` (`main.py` defines `main`, not `run`) — **model-innate**.
- `savegame.json` is not in `.gitignore`, so the autosave would be committed — **interaction**, minor.

---

## ARTIFACT B — RECORD

### Premise line
An entrance hall and a dark corridor — two rooms, one goblin, one guard — presented behind a centred ASCII banner and an eight-module package as though a castle were behind it. What the world data actually contains is a sword, a potion, and five items literally named "Placeholder S", "Placeholder W", "Placeholder O", "Placeholder R", "Placeholder D".

> *"A narrow, dimly lit passage."*

### Completability class: **NO-TERMINAL-STATES** (and the brief's win is UNWINNABLE)
The victory screen *is* reachable, and that is the defect. `main.py:42-49` defines victory as *"no remaining monsters in any room"* — and the world contains exactly one monster. Defeating the starter goblin fires it, with **no `break`**:
```
> Goblin snarls at Hero!
                      Congratulations! You have triumphed!
                         The realm is safe... for now.
You defeated the Goblin!
>                       Congratulations! You have triumphed!
                         The realm is safe... for now.
Health: 20/20
```
The banner then prints above **every** subsequent prompt, forever. The game never ends. Separately, the brief's actual win — defeat the boss — is unwinnable because **no boss entity exists in code or data**.

**Furthest point reached:** Dark Corridor — room 2 of 2. The corridor advertises an east exit and refuses it:
```
> Dark Corridor ... Exits: south, east
> move east
The destination room does not exist.
```

### Conformance: 30/47 — **SIGNIFICANTLY-DEVIATED (both triggers: count and core-loop)**
Unmet: **3, 9, 16, 20, 21, 22, 23, 26, 27, 29, 30, 33, 38, 41, 43, 44, 46** (17 unmet ≥ 10)
- **3** `go` — absent from the tree; only `move`. `> go north` → `Unknown command: go`.
- **9** `talk to` — only bare `talk`. `> talk to guard` → `to is not here.`
- **16** help lists the real vocabulary — lists 8 of 16 commands, omitting `take`, `drop`, `use`, `examine`, `talk`, `status`, `flee` and `help` itself, and renders as one line of literal `\n` (source: `parser.py` `help_text()` uses `"\\n"`).
- **20/21** weapon/armour change stats — **no code anywhere applies an item's `effect` to player stats.** `equip` sets a slot string and nothing else.
- **22** healing mid-combat — combat is atomic; there is no mid-combat input point.
- **23** three regular monsters — **one**.
- **26** distinct behaviours — one monster, `behavior: aggressive` authored, and no code branches on the `behavior` field at all.
- **27/29/30/43** boss, weakness, weakness placed, weakness item — **none exist**. Core-loop.
- **33** restart — none.
- **38** eight connected rooms — **two**.
- **41** armour among items — none authored.
- **44** two NPCs — **one**.
- **46** dialogue hints at the weakness — the guard's `start` line "Beware the boss." is never reachable (see below) and names no weakness.

Met-but-hollow under the presence rule, flagged rather than charged here: **39** (≥5 items — 7 authored, of which 5 are `Placeholder`), **28** (boss phases — phase-transition code exists in `world.py`, wired to nothing), **31** (a win path exists in code — misgated), **45** (a `dialogue_progress` lookup exists — never written).

### Room graph and placement
- Authored rooms: **2** (`entrance`, `corridor`). **Reachable from start: 2/2.**
- **DANGLING EDGE:** `corridor.connections.east: chamber` — `chamber` exists in no room definition anywhere.
- **UNPLACED ENTITIES: 5 of 7 items.** `s`, `w`, `o`, `r`, `d` — "Placeholder S/W/O/R/D" — are in the item registry and in no room. Their ids are the letters of **`sword`**, spelled out: the item list was generated by iterating the string `sword` as characters. They round-trip into every save file:
```json
"d": {"effect": {}, "id": "d", "name": "Placeholder D", "type": "misc"},
"o": {"effect": {}, "id": "o", "name": "Placeholder O", "type": "misc"},
```
- No boss, no armour, no second NPC, no weakness item exist to be unplaced.
- **Unreachable content:** the guard's `start` dialogue "Beware the boss." — the only boss reference in the whole world — can never be shown, because `engine.py:332` reads `dialogue_progress` from player state and **nothing ever writes it**, so the key is always `default`.

### State integrity — **round-trips correctly**
`save` writes a full `{combat, player, world}` snapshot; `load` restores it exactly, including a defeated monster staying dead:
```
> load
> Dark Corridor ... Exits: east, south / Items: sword     ← goblin correctly gone
```
Location, inventory, equipment, health all restore. One fault: `load` with **no save file present** prints `Game loaded.` — a false success (charged on robustness, not here).

### Robustness table (worst impact: **false success on load**; no tracebacks in play)
| probe | result |
|---|---|
| unknown command `xyzzy` | `Unknown command: xyzzy` — clean |
| **empty / whitespace input** | **silently treated as `look`** — parser default; benign but a misinterpretation |
| invalid direction `move up` | `You can't go up from here.` — clean |
| move through the dangling exit | `The destination room does not exist.` — clean refusal of a broken world |
| take/attack/talk/examine/drop/use/equip nonexistent | clean refusals, all specific |
| case `MOVE NORTH` / `LOOK` | works |
| EOF (Ctrl-D) | clean exit, no traceback |
| **`load` with no save file** | **`Game loaded.`** — silent false success |
| mid-combat oddities | not reachable — combat resolves atomically, no player input between rounds |
| launch | `RuntimeWarning: 'game.main' found in sys.modules...` on every start |

### Modification probe — **HALF FAILED**
Adding the room worked and needed one YAML edit: I authored `chamber` (which also repaired B's own dangling exit) and walked into it — `> You move east to Great Chamber.` Changing the weapon's damage **did nothing at all**: I raised `sword.effect.attack` from 2 to 9 and status still read `Attack: 5`, equipped. There is no code path from an item's `effect` to a player stat, so *a weapon's damage is not a modifiable quantity in this artifact* — the rubric's own named probe, failing outright. Additionally, **3 of the 7 shipped tests fail** under `pytest`:
```
FAILED tests/test_monster_ai_takes_its_turn_during_combat_and_perf.py
FAILED tests/test_status_command_shows_player_health__equipped_ite.py
FAILED tests/test_use_command_applies_a_consumable_item__e_g___hea.py
E  AssertionError: assert 'You use the potion' in 'The potion cannot be used to restore health.'
```

### Decisive seam bug — is it what stopped me?
Yes, twice.
1. **`world.py:105-107`** — the boss-phase subsystem imports from two wrong modules:
```python
from .combat import session as combat_session   # no module-level `session`; it is Combat.session
from .entities import BossPhase                 # BossPhase lives in combat.py, not entities.py
except Exception:  BossPhase = None
```
A bare `except` swallows both failures, `BossPhase` becomes `None`, and every phase branch is skipped into the fallback taunt. That is why the only combat narration a player ever sees is `Goblin snarls at Hero!`. Two files each internally reasonable, wired to each other wrongly.
2. **`corridor.east → chamber`** — an exit the room advertises to a room id that exists nowhere. This is what stopped exploration at room two.
3. Not a seam but worth naming: `entities.py` subtracts defense inside `take_damage` **and** `combat.py:120` subtracts it again when computing `dmg_to_player`, so `max(3−2,1)=1` then `max(1−2,0)=0` — **the monster deals literally zero damage.** I finished the only fight in the game at `Health: 20/20`.

### Attribution
- Two-room world, placeholder items generated from the letters of "sword", no boss/armour/second NPC, victory misgated and unterminated, dead phase imports, double defense subtraction, inert item effects, escaped `\n` help, missing `go` and `talk to` — **model-innate**.
- A shipped test file named `test_defeat_monster_keeps_game_running_t.py` whose docstring reads *"The game should remain alive after defeating a regular monster... attack the goblin (which triggers the victory screen), and then verify that the process has NOT terminated"* — **interaction**. The build harness's test step **canonised the runaway victory banner as intended behaviour**, which is why it survived. Recorded as a handicap, not excluded.
- README's "8 rooms" and "multi-phase boss" against a two-room bossless world; "Run tests (when they exist)" while shipping seven — **model-innate**.

---

# FLIGHT VERDICT — TEN FORCED CHOICES

## PANEL A — DELIVERY

| axis | choice | justification |
|---|---|---|
| **A1 working surface** | **A** | Every one of A's 16 advertised verbs works when invoked; B's `go` and `talk to` don't exist, its help is one unreadable line missing half the vocabulary, its equipment is inert, its monster deals zero damage, and one of its two exits refuses. |
| **A2 state integrity** | **B** | B's save is a full world snapshot that reloads exactly, defeated monster included; A records `defeated_monsters` and never applies it, so the killed goblin is standing again on resume — the axis's named failure, rewriting the world. |
| **A3 robustness** | **B** | B took ten probes without a single traceback; A crashes with an uncaught `EOFError` on Ctrl-D mid-combat and silently converts every mid-combat input — `help`, `quit`, `xyzzy` — into an attack, which is worse than B's benign empty-input-as-`look` and its one false `Game loaded.` |
| **A4 delivered scope** | **A** | A landed 6 rooms, 5 items, 3 monsters with 4 behaviour scripts, 2 NPCs, working equipment, an interactive per-round combat loop and autosave; B landed 2 rooms, 1 monster, 1 NPC, 2 real items and 5 objects named "Placeholder". |

## PANEL B — CHARACTER

| axis | choice | justification |
|---|---|---|
| **B5 ambition** | **A** | A reached for an actual dungeon — four distinct behaviour scripts, a two-phase boss that escalates its own attack, a hidden weakness item with a 15-damage proc, staged NPC dialogue chaining — while B's more general three-phase machine is docstringed by its own author as a *"Placeholder enum for future boss-phase logic"* and its world is placeholder-literal; deferral is not reach. |
| **B6 imagination** | **A** | A gives you an overgrown garden with a glint among the vines, a dust-covered armoury, a throne draped in shadows and an Obsidian Key "pulsing with dark energy"; B gives you "The starting point." and five items called Placeholder S, W, O, R and D. |
| **B7 experience (felt play)** | **A** | A has a real dungeon rhythm — gear up, weigh a fight, flee, heal, be pointed at the key — against B's two rooms, one bloodless auto-resolved fight, and a victory banner that fires on the starter goblin then prints above every prompt forever; I charge A at full weight for the wall where the game refuses to equip or use the key both NPCs tell you to seek, which I only got past by opening `game.py:742`, knowledge a player cannot obtain — and it still loses the axis by a distance. |
| **B8 craft (UI)** | **A** | A's help lists exactly the real vocabulary, status shows health/attack/location/weapon/armour, errors are specific and varied, and the parser takes synonyms, bare directions and "pick up"; B's help renders literal `\n`, omits eight verbs, rejects `go`, prints raw ids, shows an unchanging `Attack: 5` whatever you equip, and warns on every launch. |
| **B9 workability** | **A** | The mandatory probe decides it: A's one-YAML-file edit added a room and changed the sword's damage with both taking effect immediately, while in B changing a weapon's damage **cannot be done at all** because nothing reads an item's `effect` — and B's pretty module boundaries are worth less than they look when two of its cross-module imports name the wrong module and a bare `except` hides it, with 3 of 7 shipped tests failing. |
| **B10 documentation** | **A** | Both READMEs claim eight rooms and both are contradicted, but A is off by two while B is off by six and also promises "a multi-phase boss" that exists nowhere and "tests (when they exist)" that exist and fail; A's Controls section is the one fully accurate block of documentation in either tree, which outweighs its broken `main:run` console-script target. |

---

## TALLIES

**Delivery: TIED 2–2 · Character: A 6–0**

**No PANEL SPLIT.** The panels do not disagree on direction — Panel B is unanimous for A and Panel A is *tied*, not pointing at B. The tie is still the informative part of this flight and should be read plainly: **B is the more defensively-coded shell, and A is the artifact with something inside it.** B round-trips its state correctly and survives every probe without crashing, because there is very little there to round-trip and no interactive combat sub-loop to mis-parse; A crashes on a mid-combat Ctrl-D and respawns a monster it recorded as dead, because it actually has monsters, a combat loop, and a persisted world. Delivered scope and working surface, the two Panel-A axes that measure how much there *is*, both go to A decisively.

## OVERALL: **A**

Not close, and not flagged. B ships a two-room world with no boss, no armour, one NPC, a weapon that cannot be given damage, and five items whose names spell out the string they were mistakenly generated from — then declares victory on the starter goblin and never stops. A ships a real if small dungeon, loses on state integrity and robustness for defects that only exist because it has systems to break, and fails its win condition outright — but on every question of what the artifact *is*, it wins unanimously.

**Both artifacts are SIGNIFICANTLY-DEVIATED and neither is completable.** A is UNWINNABLE with no win state anywhere in its tree; B has NO-TERMINAL-STATES and no boss to win against.
