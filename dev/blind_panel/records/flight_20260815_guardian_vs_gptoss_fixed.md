# GUARDIAN FLIGHT — gpt-oss session arm WITH the content-brief fix

*Dispatched 2026-08-15 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
tier_20260815-185314/staged/arm01 (CANDIDATE — gpt-oss-120b-a5, session walk
with the content-brief fix 03ffa69 and the reasoning strip live; real 8-room
world; 20/30 goals paused at the 2h wall with combat/persistence/endgame
untested); B = dev/blind_panel/anchors/v2.0/guardian-gptoss-20260803/alpha
(GUARDIAN). Same model both sides. Candidate on A (rotation — the PRIOR
gpt-oss session arm sat B).

SIBLING FLIGHT: flight_20260815_guardian_vs_gptoss_session.md — the same
arm WITHOUT the fix (2-room stub world, 28/32 charters) lost that flight
Delivery 2-2 / Character 0-6, "not close". Read the two together; the goal
counts (28/32 stub vs 20/30 real) are design-relative and NOT comparable.

MOVEMENT vs that sibling: Character 0-6 -> 3-3 (candidate takes imagination,
workability, documentation); Delivery 2-2 -> 1-3 (candidate LOSES ground —
its combat is atomic and printless and `flee` is undispatched, the back half
the wall cut off). Overall still the anchor, but now SELF-FLAG: CLOSE where
the sibling was explicitly not close. Judge's verbatim record follows.*

---

I have completed play, probes, and modification probes on both. Final record follows.

---

# BLIND FLIGHT RECORD — `flight_20260815_guardian_vs_gptoss_fixed`

Rubric: TIER_RUBRIC v2.1 (ten forced choices, two panels, tallied separately, never summed). Checklist: CHALLENGE v2, 47 items, presence-not-reachability.

Both artifacts were copied into `<packet>/scratch/A` and `<packet>/scratch/B` before play (both write `savegame.json` into their own tree). Modification probes ran in `scratch/A_mod` and `scratch/B_mod`. Interpreter: the permitted binary only.

---

## ARTIFACT A — RECORD

### Premise line
A misty mountain pilgrimage: you start in a fog-bound village square with a sword lying in the open, and climb through a shrine, a cave system and a hidden grotto to a black fortress where a Shadow Dragon waits at the peak. It is a *place* first and a game second — eight rooms with weather, smell and geography, two hermit-ish NPCs, and a crystal shard the Elder says will break the darkness.

> "A quiet square shrouded in mist. Lanterns flicker, casting wavering shadows. The scent of pine and distant smoke fills the air."

### Entry-point ledger
| documented route | result |
|---|---|
| `python -m src.main` (README) | works — title art, then a `new` / `load` / `quit` menu |
| `pip install .` → `text-adventure` (README) | not attempted (no install inside the wall); console script is declared in `pyproject.toml` |

Title screen: an ASCII wordmark whose letterforms do not resolve into a readable word (reads approximately "Sombers.te"), then `Welcome to the Adventure!`. The game never names itself.

### Completability class: **UNWINNABLE**
Not for lack of reaching the boss — I killed it. There is no win state anywhere in the tree. `grep -rniE "you win|victor|congrat|triumph|wins the game"` over the whole artifact returns **nothing**, and `_cmd_attack` has no boss branch: the dragon dies through the same generic path as a wolf.

```
Monsters present: Shadow Dragon
Exits: south

> You have defeated the Shadow Dragon!

> Health: 61
...
Location: Shadow Dragon's Lair
```

The game then simply continued. Furthest point reached: the Shadow Dragon's Lair, boss defeated, amulet on the floor, play carrying on as if nothing happened. **Reading the source was not required to get there** — the whole map is walkable with `go` and no gate stopped me.

Death terminal state exists and fires: `You have been slain...` then `sys.exit(0)` — one line, no screen, no restart, and it **autosaves the corpse**.

### Room graph and placement
- **8 authored rooms, 8 reachable from the start room, one component.** Village Square → (N) Ancient Shrine → (E) Cave Entrance → (N) Echoing Cavern → (E) Waterfall Grotto → (N) Dark Fortress → (N) Boss Lair; Village Square → (E) Tower Ruins → (N) Dark Fortress. No disconnected wing.
- **UNPLACED ENTITY: `leather_armor` / "Patched Leather Armor".** It is in the item registry with working stats (`defense: 3`) and appears in **no room's item list**. It is the artifact's only armour, so the armour mechanic — which is fully implemented and correct — can never be exercised in play. This is the campaign's classic unplaced-entity defect, and it cost exactly one YAML line (my modification probe placed it and it worked immediately).
- All 3 regular monsters, the boss, both NPCs and the other 4 items are placed.

### Seam bugs (was a seam bug what stopped me? No — an absence was)
1. **Load silently disarms you.** `Player.from_dict` restores `_weapon_id`/`_armor_id` but not the bonuses, with the comment *"Game code can later call `equip` with the proper Item instances."* No game code ever does. The save file is correct (`"equipped_weapon": "iron_sword"`); the load is not.
2. **`flee` is advertised and undispatched.** `parser.py:60` knows the verb, `parser.py:121` puts it in the help text, `combat.py` has a `_attempt_flee` helper that is never called — and `game.py`'s dispatch has no `flee` branch, so it falls through to "I don't understand that command."
3. **Three overlapping dispatch layers** (`process_command`, `handle_input`, `_process_command`), with `talk` bolted onto one of them under the comment `# New 'talk' verb handling` because the generic one lacks it.

### State integrity
Round-trips **the world** correctly — defeated monsters stay dead, moved items stay moved, location persists — and it has a real reset (`new`) plus a graceful `No saved game found. Starting a new game instead.`

Two defects: (a) equipment is dropped on load, silently reverting Attack 10 → 5 with the sword still in inventory; (b) the save embeds an entire copy of the world (5.9 KB), so a stale save **overrides edited world data** — after renaming a room in `world.yaml`, loading still printed the old name.

```
=== run1 ===  > You wield the Sturdy Iron Sword.   (Attack: 10)
=== run2 ===  > Health: 98 / Weapon: none / Attack: 5
              > You carry: Sturdy Iron Sword
```

### Robustness battery
| probe | behaviour | class |
|---|---|---|
| `xyzzy` | "I don't understand that command." | clean refusal |
| empty line | nothing, re-prompt | silent ignore |
| whitespace only | nothing, re-prompt | silent ignore |
| `go up` | "You can't go up from here." | clean refusal |
| bare `go` | "I don't understand that command." | clean but loses the arg-missing case |
| `take/examine/use/equip nothing` | four distinct targeted refusals | clean refusal |
| `attack ghost` | "No monster named 'ghost' here." | clean refusal |
| `flee` (no combat exists to be in) | "I don't understand that command." | **dead advertised verb** |
| EOF at the game prompt | "Exiting." + autosave | clean |
| **EOF at the first prompt (new/load menu)** | **uncaught `EOFError` traceback** | **traceback** |
| case / partial names | `take sturdy iron sword` ✓, `take STURDY IRON SWORD` ✗, `take sword` ✗ — but `drop STURDY IRON SWORD` ✓ | inconsistent between commands |

Worst impact: the traceback at the entry prompt. Worst in-play impact: the dead `flee`.

### Other play findings
- **Combat is atomic.** `combat.py` runs the entire fight inside one call and prints nothing; one `attack` yields one line of outcome. No rounds, no numbers, no mid-combat anything. Its own docstring: *"fleeing is not implemented in this minimal engine."*
- **NPC dialogue never advances.** Data authors two nodes per NPC; `_cmd_talk` prints *"the first line of dialogue for simplicity"*, forever. `examine <npc>` leaks the raw structure and, with it, the hint the dialogue was meant to gate: `Village Elder: intro: The darkness in the Dark Fortress can be broken with a crystal.; hint: Seek the hidden grotto; the shard lies there.`
- **Dropping an equipped weapon leaves it equipped**, bonus intact (`Weapon: Sturdy Iron Sword / Attack: 10` with the sword on the floor).
- **3 of 6 shipped tests fail** (`AttributeError: 'dict' object has no attribute 'name'`).

### Conformance
**41 / 47.** Unmet: **16, 22, 31, 33, 35, 47.**
(16 help omits `status`/`use`/`equip`/`talk to` and prints literal `\n` escapes; 22 no mid-combat interaction exists anywhere; 31 no win path in the tree; 33 no restart offered; 35 NPC dialogue progression tracked nowhere; 47 combat is narrated nowhere.)
Verdict: **SIGNIFICANTLY-DEVIATED** — trigger is **core-loop, item 31** (not count: 6 unmet is under the 10 threshold).

### Modification probe (B9)
Added a ninth room (`sunken_crypt`, hung off the shrine) **and** changed the sword from attack 5 → 50, **touching one file: `data/world.yaml`**. No code touched, nothing broke. The new room was reachable and I used it to place the orphaned armour, which then worked (`Defense: 2 → 5`). Clean pass.

### Attribution
Unplaced armour, absent win path, atomic combat, frozen dialogue, load-drops-equipment: **model-innate**. Shipped failing tests and a README claiming tests are "when they are added": **interaction** (build-harness relationship, shipped in the tree, in comparison). Project layout: **framework-coupled**, excluded per §4.

---

## ARTIFACT B — RECORD

### Premise line
A compact stone dungeon: entrance hall, corridor, armoury, garden, library, throne room — you arm yourself off the racks, get past goblins and an orc, and face a Shadow Dragon that genuinely rears into a second phase at half health. It is a *system* first and a place second: the fight has rounds, numbers, monster temperaments, and an obsidian key the Librarian tells you will turn the tide.

> "Weeds choke a once-beautiful garden. A glint catches your eye among the vines."

### Entry-point ledger
| documented route | result |
|---|---|
| `python main.py` (README) | works — title art, `Press Enter to begin...`, straight into play |
| `pip install .` → `text-adventure` (README) | not attempted; console script declared in `pyproject.toml` |

Title screen: an ASCII wordmark that likewise does not resolve into a readable word (reads approximately "Setowte"), then the identical `Welcome to the Adventure!`. This game does not name itself either.

### Completability class: **UNWINNABLE**
Twice over, and I proved both in play.

1. **Arithmetically impossible.** Player: 20 max HP, attack 1, +3 sword, +2 shield = 6. Boss: 50 HP, 8 damage a round rising to 12 in phase two. Nine strikes needed; two survivable. The one potion heals to a 20 cap. Hit-and-run works (boss HP persists across flees, 50 → 44 → 38) but costs 8 HP per strike.
2. **The weakness is unreachable by construction — this is the seam bug that stopped me.** The boss's only weakness path is `if self.equipment["weapon"] == "boss_key"` (`game.py:742`, +15 damage). But `equip_item` writes `equipment["weapon"]` only for `isinstance(target_item, Weapon)`, and `boss_key` is `type: key`. In play:

```
> You pick up the Obsidian Key.
> That item can't be equipped.
> You can't use that right now.
```

The item's own description promises it ("Legends say it can weaken the dragon"), the Librarian's hint points at it, the boss code reads for it — and no input can ever put it in that slot. **This is exactly the archetype: a boss gated on an equipment slot its own item type can never occupy.**

3. And even if it died, **there is no win state**: the whole-tree grep for victory text returns nothing. The dragon would print `The Shadow Dragon collapses!` and the game would carry on.

Furthest point reached: Throne Room, key in hand, boss brought to 32/50 by hit-and-run, dead at the third engagement. **I read the source only to explain the equip refusal I had already hit in play.**

### Room graph and placement
- **6 authored rooms, 6 reachable, one component.** Entrance Hall → Long Corridor → {Armory E, Garden W, Library N} → Throne Room. No disconnected wing — **rooms 7 and 8 were never written**. The README claims 8.
- **UNPLACED ENTITY: `amulet` / "Silver Amulet".** In the item registry, in no room's item list. It has no mechanical role, so the cost is content rather than function — but it is the same defect class as A's.
- **DOUBLE-PLACEMENT SEAM (world.yaml ↔ world.py).** Rooms author `monsters: [goblin]` *and* monsters carry `location: corridor`; `load_world()` then appends every monster to its room a second time. Every monster room permanently displays its monster twice, and killing one leaves an **immortal ghost** that the room labels as a live threat forever:

```
> You strike for 6 damage. Goblin Guard health is now 0.
The Goblin Guard collapses!
> Long Corridor ... Danger! Monsters present: Goblin Guard
> You engage the Goblin Guard!      (combat ends instantly, no rounds)
> Long Corridor ... Danger! Monsters present: Goblin Guard
```

Every cleared room in the game lies to the player from then on, including the Throne Room.

### State integrity
Player state round-trips **well** — health, location, inventory and equipment all restored (`Attack: 6 / Weapon: Rusty Sword / Armor: Iron Shield`). Everything else fails:

- **The world resurrects.** `defeated_monsters: ["goblin"]` is written to the save and **ignored on load**; rooms are rebuilt from YAML. The goblin came back at full 10 HP and I farmed it again. Monster HP is not persisted at all, so a wounded boss heals to 50 every launch.
- **There is no reset.** No menu, and `new`, `restart`, `save`, `load` are all "I don't understand that command." The game auto-loads on launch, always.
- **Death bricks the install.** On death it writes the corpse (`"health": -1`) and exits. The next launch silently loads it: `Loaded saved game.` → `Health: -1`. You walk around as a corpse and every fight instantly re-ends. The only recovery is deleting a file the game never mentions. A new player who walks north and attacks (attack 1 vs a 10 HP goblin) dies at the first fight and hits this on their second launch.
- There is a shipped, **orphaned module-level `def __init__(self)`** at `game.py:784–809`, outside the class, whose docstring states the constructor *"no longer attempts to automatically load a saved game… loading must be performed explicitly."* The live constructor still auto-loads. A repair was written and never landed, and the tree now documents the opposite of its own behaviour.

### Robustness battery
| probe | behaviour | class |
|---|---|---|
| `xyzzy` | "I don't understand that command." | clean refusal |
| empty line | nothing, re-prompt | silent ignore |
| whitespace only | nothing, re-prompt | silent ignore |
| `go up` | "You can't go that way." | clean refusal |
| bare `go` | "Go where?" | clean, targeted |
| `take/examine/use/equip nothing` | four distinct refusals | clean refusal |
| `attack ghost` | "No such monster here." | clean refusal |
| `flee` outside combat | "There's nothing to flee from right now." | clean, targeted |
| EOF at prompt / at `Press Enter` | "Exiting game." + autosave | clean |
| **any non-`flee` input inside combat** | **silently executed as an attack** — `status`, `look`, `use Healing Potion`, `xyzzy` each consume a round and take a hit | **silent misinterpretation** |
| case / partial names | `take sword` ✓, `take IRON SHIELD` ✓, `talk to old man` ✓, `equip shield` ✓ | strong tolerance |

Worst impact: the combat-loop input collapse — it is documented in the function's own docstring as intended ("Any other command is treated as a basic attack"), and it is the mechanism that makes the healing item unusable in a fight.

No tracebacks anywhere in any probe.

### Other play findings
- **Combat works and reads well**: nested prompt, per-round damage and HP both ways, and the four behaviour branches are genuinely distinct and visible — the defensive orc prints "holds back this turn" on a coin flip, and a `coward` branch that flees exists (unused by the data).
- **The two-phase boss is real code**: at half HP it sets `phase_two`, adds +4 attack, and announces *"The Shadow Dragon roars! It enters a furious second phase, increasing its attack!"* I never saw it fire — I cannot get the boss below 25.
- **Armour buffs attack, not defence.** `Iron Shield` has `stats: {attack: 2}`; equipping it took me 1 → 3 with no damage reduction. Stats change, so the brief's letter is met; the word "armor" is not.
- **The potion is consumed at full health for 0 effect** with no warning: "You use the Healing Potion and recover 0 health."
- NPC dialogue: a real progression mechanism in code (`{node}_next`), and exactly one authored line per NPC, so it never advances.

### Conformance
**43 / 47.** Unmet: **22, 31, 33, 38.**
(22 no mid-combat item path exists — every non-flee input is an attack; 31 no win path in the tree; 33 no restart offered; 38 only 6 rooms authored against "at least eight".)
Verdict: **SIGNIFICANTLY-DEVIATED** — trigger is **core-loop, item 31**, with item 38 a second, independent content shortfall.

### Modification probe (B9)
Added a seventh room (`crypt`, hung off the garden) **and** changed the sword from attack 3 → 30, **touching one file: `world.yaml`**. No code touched, nothing broke; the room was reachable and I used it to place the orphaned Silver Amulet, which then worked. Clean pass — identical outcome to A's.

### Attribution
Equip-slot seam, double placement, missing rooms, absent win path, resurrection on load, dead-save zombie: **model-innate**. The orphaned module-level `__init__` documenting a repair that never landed: **interaction** (a build-harness edit shipped in the tree; in comparison, as a handicap). Project layout: **framework-coupled**, excluded.

---

# FLIGHT VERDICT

## PANEL A — DELIVERY

- **A1 · working surface → B.** The command the player types most does something real in B (rounds, numbers, differentiated monsters, a working `flee`) and is a printless black box in A, where `flee` is advertised and undispatched and the only armour is unobtainable; B's ghost monsters and unkillable boss are severe but narrower than A's dead verbs and invisible core mechanic.
- **A2 · state integrity → A.** A has a real `new`/`load`/`quit` reset and round-trips the world (dead monsters stay dead), losing only equipment on load; B has no reset at all, resurrects everything you killed, and auto-loads a `-1 HP` corpse that leaves the game permanently unplayable with no in-game recovery.
- **A3 · robustness → B.** B took ten probes with zero tracebacks and better-targeted refusals ("Go where?", "There's nothing to flee from right now"); A threw an uncaught `EOFError` at its very first prompt, parse-errors on a verb its own help advertises, and matches item names inconsistently between `take` and `drop`.
- **A4 · delivered scope → B.** B landed the harder machinery — an interactive turn loop, four working behaviour branches, real two-phase boss code, weakness logic, `npc_progress` persistence — and scores 43/47 to A's 41/47; A's counterweights are two extra rooms and a test suite half of which fails.

**Delivery: B 3 – 1**

## PANEL B — CHARACTER

- **B5 · ambition → B.** The rubric's own example is decisive here: B *wrote* the two-phase transition, its announcement, and a +15 weakness bonus and failed to wire the slot; A wrote the string `behavior: two_phase` in a YAML file and nothing else, and its flee mechanic exists only as a helper its author labelled unimplemented.
- **B6 · imagination (world & voice) → A.** A imagines a place with weather, smell and a route up a mountain — mist, pine smoke, a cracked altar, a chamber behind a veil of water — plus a Hermit Scholar with an unfulfilled quest offer; B is a competent, entirely standard-issue dungeon of Entrance Hall / Long Corridor / Armory / Throne Room.
- **B7 · experience (felt play) → B.** B produces actual play — tension, per-round feedback, an orc you can *feel* being defensive, a flee-and-return tactic I discovered by playing — where A produces none at any point: you type `attack`, one line returns, you never see a number, never heal, never flee, and killing the dragon changes nothing. Charged against A at full weight: A's help omits `status`, `use`, `equip` and `talk to`, and **I only used those four verbs because the checklist told me to try them — a player is told they do not exist.** Charged against B: the first-death brick is the worst single player outcome in this flight, and it is why this was the hardest call on the card.
- **B8 · craft (UI) → B.** B's help is complete and accurate, its matching takes exact, partial and any case, and its errors are targeted; A's help renders as a single run-on line of literal `\n` escapes, omits four working verbs, advertises a dead one, and `examine <npc>` dumps the raw dialogue dict — leaking the very hint the dialogue system was built to gate.
- **B9 · workability → A.** The modification probe tied exactly (one YAML file each, no code, nothing broke), so this turns on organisation: A separates parser/player/combat/world/game into modules with reusable classes and ships tests, while B is an 809-line, 30 KB god-file that also ships an orphaned module-level `__init__` asserting the opposite of the program's real behaviour — A's three overlapping dispatch tables are a real cost, but not that cost.
- **B10 · documentation → A.** Accuracy first: B's README makes a flat, load-bearing claim about the deliverable's size that play contradicts in ninety seconds — "8 rooms" against 6 authored — plus "branching dialogue" that never branches; A's contradicted claim ("Run tests (when they are added)" beside six shipped test files) *under*-states, and its module map is accurate. B's accurate Controls block is the better artefact for a stranger and nearly saves the axis, but the rubric orders accuracy ahead of completeness.

**Character: 3 – 3 (level)**

---

## PANEL TALLIES

**Delivery: B 3 – 1 · Character: 3 – 3**

No **PANEL SPLIT**: the panels do not disagree in direction. Panel B is level, not opposed — A takes imagination, workability and documentation; B takes ambition, felt play and surface craft.

## OVERALL: **B**

B delivers more of what it offers, refuses more cleanly, and reaches further on the mechanics the brief actually asks for — a turn-based fight you can read, monsters with tempers, a boss that changes at half health, a weakness item with real code behind it. A is the better-imagined world, the better-organised codebase, and the more honest README, and it is the one where the boss can actually be killed — but you kill it by typing one word and watching one line, and then nothing happens, which is the whole of A's play in a sentence.

The two artifacts fail in the same place for the same reason: **neither tree contains a win path anywhere**, both fail checklist item 31, and both are therefore SIGNIFICANTLY-DEVIATED on the core loop. Both also shipped exactly one item authored into the registry and into no room.

## **SELF-FLAG: CLOSE**

A1 and A3 were both near-calls that could have gone the other way, Panel B is dead level at 3–3, and B's dead-save brick is a defect severe enough that a second judge could reasonably let it carry B7 and invert the overall. The overall rests on a 3–1 Delivery panel with two of those three won narrowly.
