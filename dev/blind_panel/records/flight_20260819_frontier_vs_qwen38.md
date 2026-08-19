# FRONTIER FLIGHT — qwen3.8-27b first arm (INCOMPLETE artifact)

*Dispatched 2026-08-19 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
tier_20260817-214857/staged/arm01 (CANDIDATE — qwen3.8-27b, first arm, 4 legs
/ 1286 min, 30/30 cycles, 23/35 goals by ledger, ended on cycle exhaustion);
B = dev/blind_panel/anchors/v2.0/frontier-sonnet-20260803 (FRONTIER anchor).
Candidate at A (rotation honored — it sat B in the Guardian flight hours
earlier). Leak record: candidate self-named its game after the framework;
normalized in the packet copy only to "Light and Shadow" from its own
tagline. FRONTIER is an out-of-band scorecard — it never moves the ladder.

WHY THIS FLIGHT: the Guardian sweep hours earlier was a REGRESSION CHECK,
not a placement (operator: qwen3.6-27b also beat the Guardian handily). The
frontier is where actual performance shows.

VERDICT: Delivery A 2 - B 2 · Character A 0 - B 6 · OVERALL B (frontier).
No panel split, no CLOSE flag.

THE BASELINE THAT MAKES THIS READABLE: the SAME anchor beat the muse
COMPLETED artifact 10-0 on 2026-08-17 (Delivery 0-4, Character 0-6). This
INCOMPLETE artifact took two Delivery axes off it — A2 state integrity (it
round-trips a MID-COMBAT save, restoring monster HP, boss phase and turn
parity; the frontier refuses to save in combat at all) and A3 robustness
(zero tracebacks across five nested EOF prompts, against the frontier's
unhandled EOFError at its title screen). Character remains 0-6, the same
sweep muse suffered: ambition, imagination, felt play, craft, workability,
documentation.

CAMPAIGN MILESTONE, in the judge's own words: "neither artifact had the
cross-module seam bug or the placement defect that has decided every prior
flight... A lost this flight on NUMBERS — a boss the player cannot beat and
a `flee` that voids the world's own gates — and on prose, not on a
mismatched identifier."

WHAT BEAT US, measured rather than estimated: the boss is winnable at
~0.3% (best cell 19/6000 in a 20,000-trial Monte Carlo against the
artifact's own combat.py, with a policy search over heal thresholds and
routes). 39 real playthroughs, zero wins. And `flee` clears combat while
leaving the player standing in the monster's room, free to loot it and walk
out — so every guard in the world is optional. Judge's verbatim record
follows.*

---

# FLIGHT RECORD — TIER_RUBRIC v2.1, two panels, ten forced choices

Packet: `/Users/lah-rb/ouroboros-runs/flight_20260819_frontier_vs_qwen38`. Both artifacts played interactively through a PTY driver (`scratch/drive.py`) and by piped stdin; play copies in `scratch/A/`, `scratch/B/`; modification probes in `scratch/A2/`, `scratch/B2/`. Originals were read-only except for `.pyc` bytecode from the graph audit, which I removed — both shipped trees are back to their original file sets.

---

# ARTIFACT A — `Light and Shadow`

## Premise line
A nine-room village-and-tower dungeon in which a Hollow King has turned his throne room into a shadow, and the two townsfolk who will talk to you exist mainly to hand you the walkthrough. The world is a functional corridor — square, mill, garden, cave, crypt, tower — and its voice is the voice of a strategy guide, right down to an NPC quoting the boss's damage range at you.

> *"When his health is broken, he becomes a shadow form. Your blade will barely touch him. Only the Dawn Prism can shatter that shadow."* — Elder Maren

## Completability class: **WINNABLE-NOT-WON**

Reached the boss repeatedly and never won. Evidence:

- **24 driven playthroughs** (`scratch/win_a.py`, near-optimal policy: flee-past everything, iron sword, prism at phase 2, heal at low HP) → **0 wins**; 13 died in the boss fight, 11 died en route.
- ~15 further manual playthroughs → 0 wins.
- A 20,000-trial Monte Carlo against the artifact's **own** `combat.py` and `world.py`, with a policy search over heal thresholds and route choices, puts the best achievable win rate at **≈0.3% (best cell 19/6000)**.

The arithmetic behind that: boss 100 HP, best weapon gives attack 10, so 8 player actions minimum (5 attacks → phase 2 at 50, prism → 20 and back to phase 1, 2 attacks). The boss deals 8–14 raw, −3 for the only armour in the game, ~8/round over 10 rounds ≈ 80 damage. Total player HP pool is 30 + potion 25 + herb 12 = 67, before any damage taken getting there. The game is losable by construction, not by bad luck.

**The win screen is real and does fire.** I confirmed it on my *modified* copy (`scratch/A2`, iron sword attack 8→14 — my B9 probe edit), not on the shipped tree:

```
The Hollow King shatters, and dawn spills into the throne room.
========================================
Victory
========================================
The Hollow King is broken, and dawn returns to the tower.
You defeated: Hollow King.
```

Furthest point on the shipped tree: **Hollow Throne, boss driven to 50/100 and into shadow form**, prism spent, dead at 0 HP.

> `You hit Hollow King for 10.` / `The Hollow King's body dissolves into shadow form!` / `You collapse. Darkness closes in.`

**Did I need the source to get there? No.** Maren gives the crypt route verbatim; Aldric gives every monster's behaviour and the boss's exact numbers. I read the source only *after* failing, to explain why I could not win.

## Conformance: **47 / 47 — NEAR-FULL**
Unmet: **none**. Launch (1–2), command surface (3–17), combat (18–33), state and content (34–47) all present and, with the exceptions charged on the axes, working. Item 45 is met at the strong end of the gradation — genuine multi-node choice trees, not staged lines.

## Room graph and placement
**9 authored rooms, 9 reachable from `village_square`.** All exits are bidirectional; zero one-way edges. **6/6 items placed**, **4/4 monsters placed** (Cave Hall, Crypt, Tower Top, Hollow Throne), **2/2 NPCs placed**. **No unplaced entity, no disconnected component, no id seam.** I checked both forms explicitly (traversal for components, registry-minus-placements for orphans). **A seam bug is not what stopped me.**

## State integrity
Round-trips, and round-trips the *hard* case. `save` works **mid-combat** and persists the combat block; `load` drops you back inside the same fight with monster HP, player HP, boss phase and the turn-parity counter intact:

```
save.json → "combat": {"boss_phase": 1, "monster_id": "stone_golem", "turn": 2}
load      → A Stone Golem (43 hp) is here.  Health: 18/30
            attack → Stone Golem winds up and grazes you for 3.   (even turn = half damage)
```
Restart resets the world completely. Corrupt and missing saves give named, path-specific errors. **Defect:** the README claims you can load from the title screen; `_title_screen` breaks on any non-`quit` input, so typing `load` there silently starts a new game.

## Robustness battery
| probe | behaviour | worst impact |
|---|---|---|
| unknown command | `I don't understand that.` | clean refusal |
| empty / whitespace | silent re-prompt | clean |
| bare verbs | `Take what? You see: Rusty Sword` / `Examine what? items: Rusty Sword; people: Elder Maren.` | clean, with affordances |
| bare `go` | `You can't go that way.` | clean but unhelpful |
| invalid direction / `go up` | `You can't go that way.` | clean |
| nonexistent item | `You don't see that here.` | clean |
| non-equippable | `You can't equip that.` | clean |
| attack/flee with no monster | `You aren't fighting anyone.` | clean |
| mid-combat `go`/`take`/`look`/`inventory` | all → `You can't do that while fighting.` | **wrong category** — `inventory` is not a verb at all |
| **`use` healing item at full HP** | `You use the Healing Potion, but you are already at full health.` — **and the item is destroyed** | **silent resource loss** |
| EOF at title / room loop / `choice>` / combat / defeat prompt | exits rc=0, no output | **no traceback anywhere** |
| corrupt save | `Could not load: Save file at …/save.json is corrupt and could not be parsed.` | clean |
| missing save | `Could not load: No save file found at …` | clean |
| 130-char junk, numerics, case variants, partial names (`take sword`, `talk to maren`) | all handled | clean |

Worst impact: **silent destruction of a healing item at full health** — in a game this tight, that is one of three heals gone with no undo. **Zero tracebacks across every probe.**

## Modification probe (B9)
`world.py` **only**, three edits: added `"east": "sunken_vault"` to `cave_entrance`, added a tenth room dict, changed `iron_sword` attack 8→14. Worked immediately — exits line, room render, take/drop and save all absorbed the new room. **Nothing broke, nothing else touched.** A's world is one read-only nested dict with `state.py` copying the mutable half out; that split is clean.

## Attribution
- *model-innate*: the balance failure, the flee semantics, the full-health potion consumption, the generic prose, the 906-line `engine.py`.
- *interaction*: a shipped `pyproject.toml`, `Makefile`, `LICENSE`, `.gitignore`, ruff+pytest config — and a `tests/` package containing only `__init__.py`, i.e. an advertised test suite that does not exist. Charged (CHARGE WHAT SHIPS): real furniture, one hollow piece.
- *framework-coupled*: layout only — excluded.

## Decisive defect
Not a seam bug. Two things:
1. **The guard system is optional.** `flee` prints `You escape!`, clears combat, and leaves you **standing in the monster's room**, free to loot it and walk out — combat only re-triggers on room *entry*. I took the Iron Sword off the Wraith's floor at full health without landing a blow:
   > `A Wraith (25 hp) is here.` → `flee` → `You escape!` → `take Iron Sword` → `You take the Iron Sword.`
2. **The boss cannot be beaten.** ~0.3%, measured.

---

# ARTIFACT B — `The Ashen Keep`

## Premise line
A burned-out keep whose tyrant is a grieving lord gone to cinder, guarded by three creatures that each fight to their own rule, and undone only by the weakest blade in the game. Two dead-and-living witnesses each hold half the secret, and a journal in the great hall drops it a third way for players who would rather read than talk.

> *"This keep belongs to the Ashen King now — once a just lord, now a cinder-hearted tyrant. He burned this shrine, and everyone in it. Myself included."* — Sister Maren

## Completability class: **WON**

Won on the **first complete driven attempt** (`scratch/win_b.py`), and nearly lost doing it — down to 11/30 with no draughts left:

```
Your Sunfire Brand flares bright -- a searing strike for 18 damage!
(The Ashen King: 18/70 HP)
The Ashen King's crown cracks apart, spilling grey fire! "THEN BURN WITH ME!"
His wounds knit shut with ember and smoke -- he grows more savage!
...
The Ashen King's crown shatters. For one moment his ashen face looks almost human
again -- almost grateful -- before he crumbles to cold cinder and silence.
======================================================================
VICTORY
======================================================================
You defeated 4 foes in 15 combat rounds.
```

**Did I need the source? No.** I did not open a single source file before winning. The journal, Maren and Rell all point at the Sunfire Brand and the crypt.

I also ran the **negative** case to test the README's claim, and it holds: with the Iron Longsword the boss resists and you die.
> `Your Iron Longsword connects, but the blow feels blunted -- 5 damage.` → `YOU HAVE FALLEN`

## Conformance: **47 / 47 — NEAR-FULL**
Unmet: **none**. Item 45 met at the *weaker* end of the gradation — staged topic progression, no player choice — which the v2.1 ruling makes presence-lenient; the gradation is charged on B6/B7.

## Room graph and placement
**9 authored rooms, 9 reachable from `courtyard`.** All exits bidirectional, zero one-way edges. **4/4 monsters placed** (Armory, Storeroom, Crypt, Throne), **2/2 NPCs placed** (Shrine, Library), **7/7 item keys accounted for** — 6 in rooms, `rusty_dagger` in the starting inventory (verified in play: `Rusty Dagger [equipped]`). **No unplaced entity, no disconnected component, no id seam.** Both forms checked.

## State integrity
Round-trips cleanly and captures more of the *static* world than A: per-room monster HP, boss phase, `guard_active`, `monster_turns_taken` (so the Sentinel's brace cycle survives), `poisoned_turns`, per-NPC `npc_stage`, `visited` flags, `previous_location`. Verified end to end:

```
save at 6/30 HP, sentinel at 21 → title [L] → Health: 6/30, Healing Draught x2, Old Journal,
Great Hall → go east → A Hollow Sentinel blocks your way! (21/32 HP)
```
Restart from the defeat screen resets the world completely (30/30, starting kit, courtyard). **Deliberate limit:** `You can't save in the middle of a fight!` — documented, but it means B declines the round-trip case A handles.

## Robustness battery
| probe | behaviour | worst impact |
|---|---|---|
| unknown command | `I don't know how to 'xyzzy'. Type 'help' for a list of commands.` | clean, echoes the token |
| empty / whitespace | silent re-prompt | clean |
| bare verbs | `Go where? Try north, south, east, or west.` / `Equip what?` | clean; bare `examine` prints the room (loose) |
| nonexistent item | `There's no 'unicorn' here to take.` | clean |
| non-equippable / mis-used | `The Old Journal isn't something you can equip.` / `You can't use the Old Journal like that. (Try 'equip' or 'examine'.)` | clean |
| attack/flee/talk with nothing there | `There's nothing here to fight.` / `There's nothing to flee from right now.` | clean |
| mid-combat `go` | `You can't just walk away -- the Ravenous Cur won't let you. (Try 'flee'.)` | clean, and a real gate |
| mid-combat `save` | `You can't save in the middle of a fight!` | clean |
| **EOF at the title screen** | **`EOFError: EOF when reading a line`, full stack trace, rc=1** | **TRACEBACK** |
| EOF in room loop / at defeat prompt | `Farewell, wanderer.`, rc=0 | clean |
| corrupt / missing save | `No valid save file found to load.` then redraws the menu | clean, less specific than A |
| bad title choice | `I didn't understand that. Try N, L, or Q.` | clean |
| 130-char junk, numerics, case variants, `talk to sister maren` / `talk to rell` | all handled | clean |

Worst impact: **an unhandled `EOFError` traceback at the very first prompt.** This is a one-line seam: `run()` wraps its `input()` in `except (EOFError, KeyboardInterrupt)`; the sibling `show_title_screen()` (`adventure/game.py:86`) does not. Zero silent misinterpretations elsewhere.

## Modification probe (B9)
Two files. `adventure/world.py`: one `add(...)` call for a tenth room plus `"east": "vault"` on the library. `adventure/items.py`: `iron_longsword value=6 → 14`. Both took effect immediately; the save schema absorbed the new room key with no migration. **Nothing broke, nothing else touched.**

## Attribution
- *model-innate*: the prose, the resistance-inversion boss, the poison system, the monster class hierarchy, the title-screen EOF gap, `The The Ashen King`.
- *interaction*: no packaging, no lint config, no tests, no LICENSE — nothing beyond `main.py` + `adventure/` + README. Charged as a handicap on maintainer furniture.
- *framework-coupled*: layout only — excluded.

## Decisive defect
The title-screen `EOFError`. Cosmetic-severity in interactive use, loud and ugly in any non-interactive one.

---

# THE TEN FORCED CHOICES

## PANEL A — DELIVERY

| axis | winner | justification |
|---|---|---|
| **A1 working surface** | **B** | Everything B offers works, including the win, the resistance/weakness inversion and both load paths; A's guard system is nullified by `flee` leaving you in the room to loot it, its healing items are destroyed if used at full health, and its win path is offered and unreachable in 39 playthroughs. |
| **A2 state integrity** | **A** | A round-trips the strictly harder case: a save taken mid-fight restores player HP, monster HP, boss phase and turn parity and resumes inside the same fight, and its load errors name the file and the reason; B sidesteps that case by refusing to save in combat, and its extra breadth (visited flags, poison timer) is over cosmetic state. |
| **A3 robustness** | **A** | A survived EOF at five distinct nested prompts and every junk/bare-verb/bad-save probe with rc=0 and **zero tracebacks**; B dumps a full Python stack trace with rc=1 on its first prompt — B's refusals are better *worded*, but wording is B8 and charging it here would be the double-count the rubric warns against. |
| **A4 delivered scope** | **B** | A wrote more lines and more dialogue nodes, but B landed more *working systems* — poison DoT, per-monster tactical rules, a boss that heals and double-strikes on phase change, a weakness that inverts a resistance, both load entry points, quit confirmation, inventory, direction shortcuts, victory statistics — while three of A's shipped surfaces (guards, title load, the advertised test suite) did not land. |

**Delivery: A 2 – B 2.**

## PANEL B — CHARACTER

| axis | winner | justification |
|---|---|---|
| **B5 ambition** | **B** | B reached for more distinct systems and a more non-obvious centre: a boss whose ordinary-weapon *resistance* flips into a severe weakness for the game's weakest blade, a phase change that heals him, a persistent poison effect, per-monster tactical AI as polymorphism, and one secret routed three ways; A's only place of greater reach is its genuine numbered dialogue tree, which is one subsystem against six. |
| **B6 imagination (world & voice)** | **B** | A is "a small dungeon of light and shadow" with rooms whose descriptions restate their exits and an NPC who recites damage ranges; B gives a grieving lord gone to cinder, a nun's ghost kneeling in the shrine he burned her in, "banners burned to lace", "candle-wax… in long-cold rivulets", and a boss who looks "almost human again — almost grateful" as he dies. |
| **B7 experience (felt play)** | **B** | B's combat shows the enemy's HP every round, narrates each monster's signature move, gates progress honestly and peaks on the crown cracking; A's is arithmetic you lose — the first reachable monster hits for 17 against 30 max HP, you fight blind because `status` omits enemy HP, and the tension collapses either way because `flee` lets you walk past every guard and take its treasure. **No knowledge wall is charged against either artifact: I reached my furthest point in both without opening a source file.** A's wall is balance, not information, and it is charged here. |
| **B8 craft (UI)** | **B** | B has the banner, the menu, the rule-lined status block, a combat prompt that lists its own legal verbs, in-room parser hints, a quit confirmation, a farewell, and errors that quote the offending word; A's bare-verb affordance lists are genuinely good but it has no real title, no enemy HP in combat, `A Elder Maren is here.`, a silent exit, and `inventory` misreported as a combat restriction. B's own blemishes (`The The Ashen King`, `a 'old journal'`) are the same small class. |
| **B9 workability** | **B** | The probe split evenly and cleanly (A one file, B two, neither broke anything), so the axis turns on logic organisation: B is a seven-module package whose monsters are subclasses each owning their own `take_turn`, so a fourth monster is a new class and one registry line; A dispatches behaviour by string through if/elif inside shared `combat.py` and puts every command handler, both end screens and save/load into a single 906-line `engine.py`. A's shipped packaging is a real point in its favour and its empty-but-advertised `tests/` cancels much of it. |
| **B10 documentation** | **B** | A's README makes three claims play contradicts — title-screen load (the title screen has no such option and swallows `load` into a new game), `attack <monster>` "starts" combat (combat starts on room entry; `attack` outside a fight returns `You aren't fighting anyone.`), and `make test` (the `tests/` package is empty); B's makes one — its ASCII map is mirrored on both branches and hangs the courtyard off the armory — while three other B claims I specifically tested (blunted ordinary blade, the Cur's sub-half-HP double bite, save disabled mid-fight) all held. |

**Character: A 0 – B 6.**

---

# OVERALL: **B**

**Not a PANEL SPLIT.** The panels do not disagree on direction — Panel A is level at 2–2 and Panel B is unanimous for B. No panel points toward A.

**No CLOSE flag.** B is the only artifact I could finish, it won on the first complete attempt, and it took six of six on character. A's two Panel A wins are real and narrow — a genuinely harder save/load case and a clean traceback record — but they sit against an unwinnable boss and a guard system any player can walk past.

Worth recording for the campaign: **neither artifact had the cross-module seam bug or the placement defect that has decided every prior flight.** Both graphs are fully connected, both registries fully placed, both parsers accept the names their rooms print. A lost this flight on **numbers** — a boss the player cannot beat and a `flee` that voids the world's own gates — and on prose, not on a mismatched identifier.
