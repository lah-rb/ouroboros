# GUARDIAN GATE — DECISIVE PROBE: qwen3-next-coder vs the SITTING Guardian

*Dispatched 2026-08-22 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
dev/blind_panel/anchors/v2.0/guardian-gptoss-20260803/alpha (SITTING
GUARDIAN — gpt-oss-120b-a5); B = tier_20260821-221104/staged-resumed/alpha
(PROBE — qwen3-next-coder-80b-a3, BATCH structural, 2h wall_killed +
45min resume, 17/36 goals). Candidate at B (rotation honored). Scans clean.

VERDICT: Delivery A(Guardian) 3-1 · Character 3-3 (TIED, not a split) ·
**OVERALL: GUARDIAN**. No CLOSE flag.

**THE GATE IS DECIDED, AND IT DECIDES AGAINST PROMOTION.** The
pre-registered rule (recorded 2026-08-20, before either arm ran) named
qwen3-next-coder the DECISIVE probe because it is dial-less
(thinking_available:false) and therefore immune to the thinking-turn
confound that contaminates gemma. It did NOT reverse its v2.0 0-5 sweep;
it lost again. Per the rule as written, a gemma-only reversal is
explicitly NOT atrophy evidence, so the remaining probe cannot carry the
claim on its own. **The sitting guardian HOLDS**, and the gpt-oss
candidate's 8-2 stands as a same-family framework gain rather than proof
the anchor has decayed.

THE OPERATOR'S OTHER HYPOTHESIS IS THE ONE THIS SUPPORTS: the challenge
has grown (20 -> 36 goals, two-phase bosses, dialogue state) as Ouroboros
became more performant, and a no-think coder that posted "the epoch's
cleanest shape" on the older brief may simply sit below the line the
current brief draws. This is a placement result, not a broken run.

WHAT THE PROBE ACTUALLY SHIPPED — a total cross-file identifier seam.
7 of 8 rooms reachable, but of the entities: 1 of 6 items, 1 of 5
monsters, 1 of 2 NPCs, and **0 of 1 boss**. rooms.yaml references ids
that exist in no registry (iron_sword, shadow_wraith, corrupted_amulet …)
while the registry's real entities — including malgath_the_hollow (the
boss) and void_shard (the weakness item) — are placed in NO room at all.
Both placement failure forms at once, plus a stranded altar_chamber whose
back-link was written and whose way in never was.

TWO SEAM BUGS STOPPED THE JUDGE, both campaign archetypes:
  * parser.py:158 takes `target = tokens[1]` and drops the rest, so the
    game refuses every name it prints — `attack Goblin Scout` fails
    against a room that just printed "Monsters present: Goblin Scout";
    only the internal id `goblin_scout` works. The helper even carries a
    comment claiming it fixed exactly this.
  * process_combat_turn() returns its narration string and the only
    caller throws it away, so combat is INVISIBLE and its kill message
    lands on whatever command was typed next (`quit` printed "You
    defeated Goblin Scout!" and did not quit).

THE SYMMETRY WORTH KEEPING: both artifacts are UNWINNABLE with NO WIN
PATH ANYWHERE IN EITHER TREE — a grep for victory language returns
nothing in the guardian's tree and nothing but quit's "GAME OVER" in the
probe's. Two models, two framework eras, same absence.

RUN CONTEXT the judge could not see: this artifact came from a 2h arm
that was wall_killed (its first live firing — a period-12 relaunch orbit
burned 46 min) plus a 45-min resume. It reached 17/36 under those
conditions. Escalation interceded essentially never across ~6h of
qwen-coder running (one forced boss consult total): the gate counts
failed ATTEMPTS per goal, and this model fails by wandering inside a
single session, which an attempt counter cannot see.

Judge's verbatim record follows.*

---
# BLIND COMPARATIVE FLIGHT RECORD
**Packet:** `flight_20260822_qwencoder_vs_guardian` · TIER_RUBRIC v2.1 · one judge, both artifacts played

---

# ARTIFACT A

## Premise line
A small stone dungeon in six rooms — hall, corridor, armory, garden, library, throne — where you loot a rusty sword, kill two guards, and walk north into a dragon that will kill you. It is a competent, entirely generic fantasy crawl with no name of its own and no ending.
> *"An ominous throne dominates the room, draped in shadows. The final guardian awaits."*

## Entry-point ledger
| documented way in | result |
|---|---|
| `python main.py` (README) | **works** — title banner, "Press Enter", play begins |
| `pip install .` then `text-adventure` (README) | **broken** — `pyproject.toml` maps `text-adventure = "main:run"`; `main.py` defines `main()`, not `run()`. Verified: `hasattr(main,'run') == False`. Model-innate. |

The ASCII banner does not spell a legible word (it renders approximately `Setowte`), and the game is never given a title — the header is `Welcome to the Adventure!`.

## Completability class: **UNWINNABLE**
No win path exists anywhere in the tree. `grep -rniE "congratul|victor|you win|triumph|restart|play again"` over `*.py *.yaml *.md` returns **nothing**. Killing the boss would print `The Shadow Dragon collapses!` and drop you back to the prompt. Independently, the fight is arithmetically unwinnable: boss 50 HP / 8 attack (→12 in phase two) against a player capped at 20 HP and 6 attack, with the only healing item unusable in combat.

**Furthest point reached:** Throne Room, holding the Obsidian Key, engaged the Shadow Dragon, died at −4 HP. **Reading the source was NOT required to get there** — every name the game prints is a name it accepts.

## Conformance: **41 / 47 — SIGNIFICANTLY-DEVIATED**
**Unmet: 22, 23, 31, 33, 38, 45.**
Trigger is **core-loop**, not count (6 unmet < 10): item 31, the win path, is absent from the tree.
- **22** no code path connects combat to `use_item`; combat is a nested `input()` loop that recognises only `flee`.
- **23** only two regular monsters (`goblin`, `orc`); `dragon` is the boss.
- **31** no win path anywhere (above).
- **33** no restart offered or implemented.
- **38** six rooms authored, not eight.
- **45** each NPC has exactly one dialogue node and `triggers: {}`; no branch exists anywhere.

## Room graph and placement
`world.yaml` authors **6 rooms; all 6 reachable** from `start` as one component (start↔corridor↔{armory, garden, library}, library↔throne). No disconnected component.
**Unplaced entity: `amulet` / "Silver Amulet"** — authored in the item registry, in no room's `items` list. 5 items authored, 4 obtainable.
Boss, both NPCs and all three monsters are placed.

## Seam bugs
**Yes — a seam bug is the reason the boss mechanic is dead.** `game.py:742` gates the weakness on
```python
if self.equipment["weapon"] == "boss_key":
```
but `boss_key` has `type: key`, and `equip_item` accepts only `Weapon`/`Armor` instances. Observed:
> `> equip Obsidian Key` → `That item can't be equipped.`

This is the canonical *"Boss gated on an equipment slot its own item type can never occupy"*. Model-innate.

Second seam, cosmetic but visible on every screen: `world.py` registers monsters **twice** — once from each room's `monsters:` list (line 127) and again from each monster's own `location:` field (lines 133–135):
> `Danger! Monsters present: Goblin Guard, Goblin Guard`

Third: a **module-level orphan** `def __init__` at `game.py:784`, dedented out of the class, whose docstring reads *"It no longer attempts to automatically load a saved game"* — a repair written into the wrong scope. The class's real `__init__` still autoloads. Dead code that lies. Model-innate.

## State integrity
Save is **automatic and inescapable**: written on `quit`, on EOF, and on death; loaded silently at every launch. There is no save command, no load command, no new-game option, and the README never mentions it.

- Player stats, location, inventory, equipment and per-room item lists **round-trip correctly**.
- `defeated_monsters` is **written and never applied on load** — killed monsters resurrect:
> save: `"defeated_monsters": ["orc"]` → next launch: `Danger! Monsters present: Orc Warrior, Orc Warrior`
- Death is **saved**, then silently resumed:
> `"player": {"health": -4}` → next launch: `Health: -4 / Location: Throne Room`

The only way to start over is to find and delete `savegame.json`. Model-innate.

## Robustness battery (10 probes)
| probe | behaviour | class |
|---|---|---|
| unknown command `xyzzy` | `I don't understand that command.` | clean |
| empty / whitespace | no output, reprompt | clean |
| invalid move `go up` | `You can't go that way.` | clean |
| verb with no arg (`go`,`take`,`use`,`equip`) | `Go where?` / `Examine what?` etc. | clean |
| nonexistent noun (`take Excalibur`, `talk to Ghost`) | `No such item here.` / `No one here by that name.` | clean |
| case / synonyms (`LOOK`, `Go North`, `ATTACK Goblin Guard`, `hit`, `inv`, `pick up`) | all accepted | clean |
| 400/500-char input | `I don't understand that command.` | clean |
| unicode `★★★` | `I don't understand that command.` | clean |
| **EOF at main prompt** | `Exiting game.` + save | clean |
| **EOF inside combat** | **`EOFError` traceback, `game.py:680`** | **traceback** |
| **EOF at the title prompt** (empty stdin) | **`EOFError` traceback, `main.py:22`** | **traceback** |
| **any non-`flee` command inside combat** | **silently executed as an attack** — `status`, `look`, `use Healing Potion` and `quit` all strike the monster | **silent misinterpretation** |

**Worst impact: traceback** (two ordinary EOF paths), with systematic silent misinterpretation inside combat as the close second.
> `> use Healing Potion` → `You strike for 1 damage. Goblin Guard health is now 8.`

## Modification probe (B9)
Added a 7th room (`crypt` / "Sunken Crypt"), linked it `garden south ↔ crypt north`, placed the orphan `amulet` in it, and changed `sword` attack 3 → 99. **`world.yaml` only; no Python touched.** Everything took effect on the next launch: room reachable, amulet takeable, `Attack: 100` after equipping. Nothing broke. The save file keys rooms by id, so the new room round-tripped.

## Attribution
model-innate: the boss-weakness seam, the doubled monster registry, the orphan `__init__`, the missing win path, the broken `main:run` console entry, autoload-on-death. framework-coupled: project layout only (excluded). interaction: none observed — no shipped save file, no stub tooling; `pyproject` declares real `ruff`/`black` extras.

---

# ARTIFACT B

## Premise line
**OUROBOROS DUNGEON** is a prison-break through a corrupted hollow toward Malgath, a void-wrought thing whose warden sends you to find a shard that can pierce his veil. It is a genuinely well-written dungeon that is almost entirely empty when you walk into it — one item, one monster, one NPC, and no Malgath anywhere.
> *"A towering figure of void-wrought bone, draped in tattered robes that ripple like smoke. Its hollow eyes pulse with a sickly violet light, and the air around it shimmers with unstable energy."*

## Entry-point ledger
| documented way in | result |
|---|---|
| `python main.py` (README) | **works** — boxed banner, main menu (New/Load/Quit) |
| `pip install -e .` then `text-adventure` | maps `main:main`, which **exists** — resolves correctly |
| Main menu → `2. Load Game` | **always** `No saved game found.` (see State integrity) |

Minor: prints `TERM environment variable not set.` and leaks `ESC[3JESC[HESC[2J` when stdout is not a TTY.

## Completability class: **UNWINNABLE**
No win path anywhere: `grep -rniE "phase|victor|congratul|you win|GAME OVER|malgath" *.py` finds `"GAME OVER"` only as the string returned by `quit`. Nothing in code or data expresses victory. Independently, **the boss is in no room** — Malgath cannot be met, and the Void Shard that is supposed to defeat him is in no room either.

**Furthest point reached:** Collapsed Armory; killed the Goblin Scout; visited all seven reachable rooms; talked to the one reachable NPC. **Reading the tree WAS required** — see B7 below.

## Conformance: **40 / 47 — SIGNIFICANTLY-DEVIATED**
**Unmet: 8, 17, 28, 30, 31, 33, 38.**
Trigger is **core-loop** (7 unmet < 10): item 31, the win path, is absent from the tree.
- **8** `equip` is not a command. `VALID_COMMANDS` has no entry, `process_command` has no branch. Observed: `> equip Rusted Key` → `Unknown command: equip`. (Equipping happens as a side effect of `use`.)
- **17** `_handle_status` prints Health / Attack / Defense and **no location at all**.
- **28** two phases appear in neither code nor data — `behavior: phase_shift` is one label no code reads.
- **30** `void_shard` is in no room's item list anywhere.
- **31** no win path (above).
- **33** the defeat line does not end the run and offers no restart.
- **38** eight rooms authored; **seven reachable** (see below).

Items 22, 26, 29, 36, 45, 47 are scored MET on the checklist's presence rule and are charged hard on the axes: monster `behavior` strings are read by nothing; the dialogue `options` trees render never; `save_game()` is imported and never called; combat narration strings are built and discarded.

## Room graph and placement — the decisive finding
`data/rooms.yaml` authors **8 rooms**. **Seven are reachable** from `cell`.
**DISCONNECTED COMPONENT:** `altar_chamber` ("Deep Altar Chamber") declares `connections: {north: throne_room}` — the back-link — but `throne_room` declares only `west/north/south` and never links to it. The author wrote the return trip and not the way in. One room stranded.

**UNPLACED / UNRESOLVABLE ENTITIES — this is a total cross-file identifier seam.** `rooms.yaml` references ids that do not exist in `items.yaml` / `monsters.yaml`, and the entities that do exist are referenced by nobody:

| room references | exists in registry? | registry entity | placed in any room? |
|---|---|---|---|
| `iron_sword`, `leather_vest` | **no** | `rusted_iron_sword`, `leather_tunic` | **no** |
| `corrupted_amulet`, `silver_chalice`, `ancient_journal`, `crystal_lamp`, `purification_flask`, `obsidian_dagger` | **no** | `healing_potion`, `void_shard`, `rune_dagger` | **no** |
| `shadow_wraith`, `spectral_guardian`, `book_wraith`, `dread_reaver` | **no** | `shadow_stalker`, `stone_golem`, `corrupted_knight` | **no** |
| — | — | **`malgath_the_hollow` (the boss)** | **NO ROOM AT ALL** |
| — | — | **`sister_vanya` (NPC 2)** | **NO ROOM AT ALL** |

Only `rusted_key` (cell) and `goblin_scout` (armory) resolve. **Delivered world: 7 rooms, 1 of 6 items, 1 of 5 monsters, 1 of 2 NPCs, 0 of 1 boss.** Every monster `drops:` id also dangles, so kills yield nothing. Both forms of the placement defect are present at once — a stranded component *and* five unplaced entities including the boss and the weakness item.

Confirmed in play — six of seven rooms print an empty list, and the prose promises what is not there:
> `A hidden alcove holds a dusty journal.`
> `Items visible: `

The quest text compounds it: Elias sends you to *"the Sunken Library"*; the room is called Hidden Library and the Shard is nowhere.

## Seam bugs
**Yes — two seam bugs are what stopped me, and both are the archetype.**

**(1) Parser emits one token; handlers compare full display names.** `parser.py:158` takes `target = tokens[1]` and drops the rest into `remaining_args`, which nobody reads. `engine.py:_handle_attack` correctly snake-cases full display names — but never receives them. The helper even carries a comment claiming it fixed exactly this:
```python
# Normalize target to snake_case to support multi-word names like "Goblin Scout"
```
Observed, against a room that had just printed `Monsters present: Goblin Scout`:
> `> attack Goblin Scout` → `You don't see 'goblin' here.`
> `> attack goblin` → `You don't see 'goblin' here.`
> `> attack scout` → `You don't see 'scout' here.`
> `> attack goblin_scout` → `You engage Goblin Scout in combat!`

The same truncation kills `examine`, `drop` and `use` on the one item in the game:
> `> take Rusted Key` → `You picked up Rusted Key.`
> `> examine Rusted Key` → `You don't see 'Rusted' here.`
> `> drop Rusted Key` → `You don't have 'Rusted'.`

**(2) Combat narration is discarded by its only caller.** `process_combat_turn()` returns `(True, "You attack X for N damage…")`; `process_command` runs it, checks only the boolean, throws the string away, and returns the parsed command's own message instead. Combat is therefore invisible — and the terminal message lands on whatever command happened to be typed:
> `> attack goblin_scout` → `You engage Goblin Scout in combat!`
> `> look`×6 → room descriptions only; health drains 100 → 97 with no narration
> `> quit` → `You defeated Goblin Scout! Dealt 10 damage.`  *(and the game did not quit)*

**(3)** The weakness system compares `item.type` against an elemental string (`weakness: fire`, `weakness: light`); item types are `weapon`/`armor`/`consumable`/`quest_item`. It can never fire. Malgath has no `weakness` field at all.
**(4)** `dialogue.yaml` authors `options:` as a **list of dicts** with `next:` targets (`elias_malgath_hints`, `elias_passage`, `elias_shard_tale`); `_handle_talk` expects a **dict** and calls `.items()`, and none of the three target nodes exist as dialogue nodes. No branch has ever rendered.

All model-innate.

## State integrity
**The save round trip cannot be attempted.** `save_game()` is imported in `main.py:5` and **called nowhere** — verified by grep, and in play:
> `> save` → `Unknown command: save`
> Menu `2. Load Game` → `Loading saved game... No saved game found.`

`to_dict`/`from_dict`/`save_game`/`load_game` are all written and correct-looking; nothing invokes the writer. Death does not end the run either — the player continues at 0 HP.

## Robustness battery (10 probes)
| probe | behaviour | class |
|---|---|---|
| unknown command `xyzzy` | `Unknown command: xyzzy` | clean |
| empty / whitespace | skipped, reprompt | clean |
| invalid move `go up` | `You can't go up from here.` | clean |
| verb with no arg | `Invalid command: attack requires a target` | clean |
| nonexistent noun | `You don't see 'X' here.` | clean |
| movement during combat | `Cannot leave combat area during an active battle!` | clean |
| 400-char input | echoed as `Unknown command: bbbb…` | clean (ugly) |
| unicode `★★★` | `Unknown command: ★★★` | clean |
| EOF mid-game | `End of input. Exiting...` | clean |
| EOF at menu / empty stdin | `Goodbye!` | clean |
| invalid menu choice | `Invalid choice. Please enter 1, 2, or 3.` | clean |
| **`quit` during combat** | **swallowed; prints a kill message and play continues** | **silent misinterpretation** |
| `flee` failure text | `Flee failed! Goblin Scout hits you for 2 damage as you escape.` — contradicts itself | cosmetic |
| `go` bare | `You can't go  from here.` (empty direction, double space) | cosmetic |

**Worst impact: silent misinterpretation** (the swallowed `quit`, and combat state advancing with no output). **No traceback under any probe**, including empty stdin.

## Modification probe (B9)
Added a 9th room (`vault` / "Warden's Vault"), linked `cell east → vault`, placed `rusted_iron_sword` in it with its correct registry id, and changed that sword's attack 3 → 99. **`data/rooms.yaml` + `data/items.yaml` only; no Python touched.** All of it took effect: room reachable, item resolved and takeable, `use` equipped it, `Weapon Bonus: +99 Attack` in status, and combat computed `Dealt 109 damage`. Nothing broke. The probe also proves the engine is sound and that the empty world is a pure identifier problem.

Shipped tests: 8 tests, **7 pass, 1 fails** — `test_look_alias_l_is_parsed_as_look` (`'l'` is not aliased; `VALID_COMMANDS` has no `look` entry at all). Interaction-attributed: a failing test shipped in the tree. Also two identical back-to-back `SIMPLE_ACTIONS = {...}` assignments in `parser.py`.

## Attribution
model-innate: every seam above, the stranded room, the unplaced boss/NPC/weakness item, the uncalled `save_game`, the missing `equip`. interaction: the shipped test suite with one failing test, and the `README` project-structure diagram describing a `src/` layout the tree does not have. framework-coupled: layout only (excluded).

---

# FLIGHT VERDICT — ten forced choices

## PANEL A — DELIVERY
| axis | winner | justification |
|---|---|---|
| **A1 working surface** | **A** | Every command A's help lists does real work and 4 of 5 items, all 3 monsters and all 6 rooms are usable; B's `equip` and `save` do not exist, `examine`/`drop`/`use`/`attack` reject the names the game prints, and 5 of 6 items, 4 of 5 monsters, the boss and the second NPC are unreachable. |
| **A2 state integrity** | **A** | A's save round-trips stats, location, inventory, equipment and room contents (failing only on `defeated_monsters`, which resurrects the dead, and on autosaving death); B's `save_game()` is never called by anything, so `Load Game` can only ever answer `No saved game found.` |
| **A3 robustness** | **B** | B produced no traceback under any probe including empty stdin, while A crashed with `EOFError` on two ordinary paths (the title prompt and mid-combat) — and A shares B's silent-misinterpretation fault anyway, since its combat loop executes `quit`, `status` and `use` as attacks. |
| **A4 delivered scope** | **A** | A landed 6 rooms, 4 obtainable items, 3 monsters with three genuinely distinct implemented behaviours, 2 reachable NPCs, working equipment and a working save; B landed 7 rooms, 1 item, 1 monster, 1 NPC, no equipment, no save and no boss. |

**Delivery: A 3 – 1**

## PANEL B — CHARACTER
| axis | winner | justification |
|---|---|---|
| **B5 ambition** | **B** | Judged on the design attempted and not on what survived, B reached materially further — five monsters with health/attack/defense/behaviour/drop-tables/elemental weaknesses, a 120-HP named antagonist, a quest-giver with objective and reward, two NPCs with three-option dialogue trees, a defense stat, and a New/Load/Quit menu — against A's single generic six-room crawl. |
| **B6 imagination (world & voice)** | **B** | B named its game and its antagonist and gave both a cosmology, and writes lines like *"You reek of the Hollow's reach—Malgath's corruption clings to you like rot"*; A offers Entrance Hall, Long Corridor, Old Man, Librarian, an untitled game, and a banner that does not spell a word. |
| **B7 experience (felt play)** | **A** | I could explore, arm myself, watch my attack go 1→6, meet three visibly different fighters (*"The Orc Warrior holds back this turn"*) and die to the dragon in A using nothing but what it printed — whereas in B I could only start the game's single fight by guessing the internal id `goblin_scout`, knowledge a player has no way to obtain, and that fight then produced no narration at all in a world whose other six rooms print empty item lists under prose promising a journal that is not there. |
| **B8 craft (UI)** | **A** | B fails input tolerance in exactly the way this axis names — it will not accept the names it prints, across `examine`, `drop`, `use` and `attack`, rejects the `talk to <npc>` its own README documents (`You don't see 'to' here.`), calls `equip` and `save` unknown commands, and omits location from `status`; A accepts every name it prints, plus case variants, synonyms and `pick up`. |
| **B9 workability** | **B** | The modification probes tied cleanly (both are pure data edits, nothing broke), so this turns on organisation: B is five typed modules with a test suite whose catastrophic content loss is one id-rename plus two ~5-line fixes at named seams, while A is one 809-line file whose combat is a nested `input()` loop that structurally cannot host another command, carrying dead `in_combat` machinery and an orphan module-level `__init__` whose docstring contradicts the class it fell out of. |
| **B10 documentation** | **A** | Accuracy against play decides it: every command in A's Controls list works, including `talk to <npc>`, whereas B's Commands list offers `equip <item>` and `talk to <npc>`, both of which the game refuses — and B additionally claims branching conversations, a two-phase boss with a hidden weakness, and persistent save/load, none of which exist in play. (A is charged for `text-adventure = "main:run"` targeting a function that does not exist, and for claiming 8 rooms and 3 regular monsters when it ships 6 and 2.) |

**Character: A 3 – 3 (tied)**

---

# PANEL TALLIES

**Delivery: A 3 – 1 · Character: 3 – 3 (tied)**

**No PANEL SPLIT.** The panels do not point in opposite directions — Panel A favours A, and Panel B has no direction to disagree with. Recording this explicitly because a tied character panel is not the same finding as a split one: B did not out-character A overall; it won imagination, ambition and workability while losing felt play, surface craft and documentation.

---

# OVERALL: **A**

A ships a game a person can play from the title screen to their own death using only what the program prints on the glass: eight commands that do real work, three monsters with three different combat scripts, equipment that moves the numbers, and a save that restores most of the world. B ships a far better-imagined and far better-organised building that is empty — one item, one monster reachable only by guessing an internal identifier, one NPC, no boss, no save, and combat whose every line of narration is computed and thrown away by the function that asked for it.

Neither is winnable, and both deviate on the same trigger: **no win path exists anywhere in either tree** — a grep for victory language returns nothing in A's `*.py *.yaml *.md`, and nothing but `quit`'s `"GAME OVER"` in B's. That symmetry is the flight's most transferable finding.

**No CLOSE flag.** The delivery gap is wide and unambiguous, and B's character-panel strengths — real, and worth recording — do not offset an artifact whose single fight cannot be started by any name the artifact itself displays.
