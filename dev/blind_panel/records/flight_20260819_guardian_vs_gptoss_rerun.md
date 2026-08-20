# GUARDIAN FLIGHT — gpt-oss grinder RERUN under the collapsed-diagnose stack

*Dispatched 2026-08-19 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
tier_20260819-202151/staged/arm01 (CANDIDATE — gpt-oss-120b-a5, 2h wall,
session mission, 24/32 goals, 0 junk diagnoses); B =
dev/blind_panel/anchors/v2.0/guardian-gptoss-20260803/alpha (GUARDIAN).
Candidate at A (rotation honored). Scans clean; no self-naming.

VERDICT: Delivery A 3-1 · Character A 5-1 · OVERALL CANDIDATE. No split,
no CLOSE flag. **First Guardian win by any gpt-oss arm.** Completability:
WON — a real two-sided ending ("=== VICTORY ===" reached and reproduced)
against the anchor's UNWINNABLE (no win path anywhere in its tree).

THE LONGITUDINAL SERIES against this same anchor, same model family:
  2-8  (08-15 stub arm — escalate loophole era)
  4-6  (08-15 fixed arm — content briefs restored)
  3-7  (08-19 morning — role-semantics stack, two-shape diagnose)
  8-2  (08-19 evening — + collapsed diagnose, uncapped investigation)
Same 2h wall throughout. The run-side deltas for the final step: junk
diagnoses 11→0, goals 18/30→24/32, and use/talk/attack/flee/combat all
VERIFIED in-run — the exact verbs that lost the morning flight.

CHARGED AGAINST THE CANDIDATE, for the next round: an id-only parser that
refuses the display names it prints (B7 "nearly flipped" on it; B8 lost —
the judge routed around it with knowledge a player cannot obtain); help
omitting examine/status/go/restart (the help goal FALSE-PASSED in-run for
the second consecutive run — the help charter needs the completeness check);
combat swallowing non-combat verbs incl. a phantom "You save."; three
uncaught tracebacks (EOF at name prompt, EOF in combat, load-with-no-save);
an inert crystal-shard weakness (flavour text only, no code); zero-damage
regular monsters; shipped out.txt/tmp_output.txt litter and a 4-of-5-failing
test suite. Judge's verbatim record follows.*

---

# FLIGHT RECORD — TIER_RUBRIC v2.1, blind comparative

Packet: `flight_20260819_guardian_vs_gptoss_rerun`. Both artifacts played first, source consulted only to explain observed failures and to run the mandatory modification probe. Play was done on copies under `<packet>/scratch/A/` and `<packet>/scratch/B/alpha/`; both `world.yaml` files were restored after the mod probes (verified by diff).

---

## ARTIFACT A — record

**PREMISE LINE.** A ruin-crawl through the Ruins of the Ancient Citadel: eight rooms on a vertical map — armory, garden, courtyard, hall, library, a flooded lower level, a crypt below it, and a throne chamber at the top — where you gear up, kill three guardians, and face a warden that changes gear mid-fight. It is a small, complete arc with a real ending on both sides. Its prose: *"A cold, stone crypt houses restless bones and a faint, eerie glow emanates from a crystal shard."*

**COMPLETABILITY: WON.** Full victory reached and reproduced after restoring the tree:

```
The Ancient Citadel Warden collapses, dropping its loot.

=== VICTORY ===
With the final boss vanquished, the ancient citadel crumbles behind you.
```

**Entry-point ledger.** README documents `pip install -r requirements.txt` + `python main.py` → works. `pyproject.toml` declares `text-adventure = "main:main"`; `main.py` defines `main()` → declaration is correct.

**ROOM GRAPH + PLACEMENT.** 8 authored rooms, **all 8 reachable** from the start room (`armory`), traversed by hand: armory ↔ garden ↔ throne_chamber, armory ↔ grand_hall ↔ throne_chamber, armory ↔ courtyard ↔ library ↔ (down) crypt ↔ flooded_level ↔ (west) library. No disconnected component. **No unplaced entity**: all 5 items, all 4 monsters (3 regular + boss) and both NPCs appear in a room. Exits are asymmetric in places (garden `south` → courtyard, but armory `north` → garden) — surprising, not broken.

**SEAM BUG — did it stop me? No, but it walls a real player.** `take`/`equip`/`drop`/`use`/`talk` call `self._take_item(args[0])` — first token only, matched against **ids** — while `_handle_examine` joins all args and matches *"item IDs **and** item display names (case-insensitive)"*. Two resolution policies in one file:

```
> take Sharp Steel Sword
There is no 'sharp' here.
> examine Sharp Steel Sword
Sharp Steel Sword:
A finely forged steel sword, its edge still gleams despite the ages.
> take steel_sword
You take the Sharp Steel Sword.
```

`take sword`, `take steel`, `take breastplate`, `take amulet`, `take shard`, `take gleaming_crystal_shard` and `talk grizzled_guard` are all refused. **I routed around this with knowledge a player has no way to obtain** — I guessed the snake_case convention, then read the id that `inventory` prints, which you can only see *after* a successful take. A player who types the name the room printed is stopped in room one.

**Second decisive defect — combat modality.** `attack` enters a modal `Combat>` loop where non-combat verbs are swallowed as a wasted turn, including a phantom save:

```
Combat>
--- COMBAT TURN ---
You save.
The Restless Skeleton attack.
Your health: 100/100
```

Nothing was saved. `flee`, `attack` and `use` do work in that loop (`use` heals and costs no turn).

**Third — the boss weakness is absent.** `grep -niE 'weak|vulnerab|crystal_shard|ancient_amulet'` over all `.py` returns **nothing**. The Gleaming Crystal Shard is designated only in flavour text. Measured: boss fight with the shard in inventory and without it both deal exactly `20 damage` per hit.

**Fourth — the regular monster tier is inert.** Skeleton (atk 5), Giant Rat (atk 4) and Cursed Statue (defensive, never swings) all deal **0** damage against base defence 5. Three of four fights are unloseable: `The Restless Skeleton attack. / Your health: 100/100`. Only the boss is a real fight, and it is a good one — a narrated phase change and a genuine loss condition.

**Monsters print as raw ids**: `A hostile presence looms: final_boss` / `cursed_statue`.

**STATE INTEGRITY: full round-trip.** Killed the Cursed Statue, saved, reloaded: `defeated_monsters: ['cursed_statue']`, `grand_hall.monster_defeated: True`, and on reload the statue is **gone from the room**. Location, health, attack, defence, equipment, inventory, room item lists and per-NPC `dialogue_index` all restore. World not rewritten. (Wart: rooms with no monster serialize `monster_defeated: true`.)

**ROBUSTNESS (worst impact: uncaught traceback on `load` with no save file — a help-advertised command on a fresh install).**

| probe | A |
|---|---|
| unknown command | clean — `I don't understand that command.` |
| empty / whitespace-only | ignored, clean |
| invalid direction | clean — `You can't go 'up' from here.` |
| take nonexistent | clean — `There is no 'nothing' here.` |
| equip item not held | clean |
| talk to absent NPC | clean |
| verb with no argument | clean — `Take what? Specify an item id.` |
| 300-char garbage | clean refusal |
| case (`ATTACK`, `Go North`) | tolerant |
| EOF main prompt | clean exit |
| EOF at name prompt | **TRACEBACK** `EOFError` in `GameEngine.__init__` |
| EOF inside combat | **TRACEBACK** `game.py:564 raw = input("\nCombat> ")` |
| `load`, no save present | **TRACEBACK** `game.py:624 loaded = load_game()` |
| non-combat verb in combat | **silent misinterpretation** — wasted turn, `You save.` lies |

**MODIFICATION PROBE — PASS, data-only.** Ninth room: added a `belfry` block to `data/world.yaml` and one `up: belfry` line to `library`. Zero code touched, worked first try (`Cracked Belfry / Exits: down`). Weapon damage: `attack: 10` → `55` in the same file, status read `Attack: 65`. Nothing broke.

**CONFORMANCE: 43/47 — NEAR-FULL.** Unmet: **16** (help omits `examine`, `status`, `go`, `restart`), **29** (boss weakness appears in neither code nor any data field — borderline, it is named in flavour prose; the verdict is NEAR-FULL either way), **33** (defeat screen offers no restart; a bare `restart` verb exists but is never offered and the process exits), **45** (dialogue is one fixed block printed verbatim forever — below the "staged progression" floor the operator's ruling set).

**Attribution.** Id-only parser, inert weakness, zero-damage monsters, combat swallow — *model-innate*. Shipped `out.txt` and `tmp_output.txt` debug transcripts, and a `tests/` suite that is **4 of 5 failing** — *interaction*. Project layout — *framework-coupled, excluded*.

---

## ARTIFACT B — record

**PREMISE LINE.** A compact dungeon laid out as a corridor hub — entrance, corridor, armory east, garden west, library north, throne beyond — where an Old Man and a Librarian both point you at an obsidian key before you meet a Shadow Dragon. It is a clean, conventional crawl that stops one mechanic short of being a game. Its prose: *"Shelves of ancient tomes tower above you. Knowledge hangs heavy in the air."*

**COMPLETABILITY: UNWINNABLE.** Two independent reasons, either alone sufficient.

**(1) There is no win path anywhere in the tree.** `grep -rniE 'victor|congratul|you win|winner|triumph|game_won'` over every `.py`, `.yaml` and `.md` returns **nothing**. Killing the boss prints `The {name} collapses!` and `break`s. `defeated_monsters` is written at line 700 and read at line 156 and **never consulted for anything**. This is checklist item 31 absent, and it fires the core-loop trigger.

**(2) The boss weakness is gated on a slot its own item type can never occupy** — the campaign's canonical seam bug, verbatim:

```python
# Check for weakness item in inventory
if self.equipment["weapon"] == "boss_key":
    extra = 15
```

`boss_key` is `type: key`. In play:

```
> take Obsidian Key
You pick up the Obsidian Key.
> equip Obsidian Key
That item can't be equipped.
```

The comment says *inventory*; the code reads the weapon slot. **A seam bug is exactly what stopped me.**

Arithmetic confirms it independently: maximum reachable attack is 6 (base 1 + Rusty Sword 3 + Iron Shield 2), the dragon has 50 HP and hits for 8 (12 in phase two), the player has 20 HP, the single Healing Potion caps at 20 and **cannot be drunk in combat**. Furthest point reached: Throne Room, Obsidian Key in inventory, dragon at 32/50, dead on turn three.

```
> You strike for 6 damage. Shadow Dragon health is now 32.
The Shadow Dragon slashes you for 8 damage. Your health is now -4.

You have fallen in battle. Game over.
```

**Entry-point ledger.** `python main.py` → works. README instructs `pip install .` then `text-adventure`; `pyproject.toml` declares `text-adventure = "main:run"` and **`main.py` defines no `run`** (it defines `main`). The documented console-script entry point is broken.

**ROOM GRAPH + PLACEMENT.** **6 authored rooms**, not eight. All 6 reachable from `start`; no disconnected component. **UNPLACED ENTITY: `amulet` / "Silver Amulet"** — authored in the `items:` block of `world.yaml`, listed in no room's `items:` and granted by nothing. It is unobtainable in play; a pure room-to-room walk would never find it. Also authored-and-unused: behaviour class `coward` (implemented in the combat loop, used by no monster). Only **two regular monsters** exist (goblin, orc); `dragon` is the boss.

**Second seam bug — every monster is doubled.** `world.yaml` places monsters twice, once in each room's `monsters:` list and once via each monster's own `location:` field, and `world.py` honours both (line 127 copies the room list, lines 133–135 append by location). Result:

```
Danger! Monsters present: Goblin Guard, Goblin Guard
Danger! Monsters present: Shadow Dragon, Shadow Dragon
```

Killing one leaves its twin standing.

**Third — combat silently converts everything into an attack.** The loop handles `flee` and falls through to attack for all other input. `xyzzy`, `status`, and critically `use Healing Potion` all swing your weapon:

```
> attack Goblin Guard
You engage the Goblin Guard!
> You strike for 1 damage. Goblin Guard health is now 9.
The Goblin Guard attacks you for 3 damage. Your health is now 17.
```
(the strike above is the response to `status`.)

**Fourth — the armour adds attack**: `Iron Shield`, `type: armor`, `stats: {attack: 2}`.

**STATE INTEGRITY: the world is rewritten on load.** Killed the Orc Warrior, quit (autosave), relaunched. The save is correct — `"defeated_monsters": ["orc"]` — and the load reads it into a set that nothing applies:

```
--- Adventure Begins ---
Armory
Danger! Monsters present: Orc Warrior, Orc Warrior
```

The orc is back, and doubled again. Room *items* are restored (line ~163); room *monsters* are not. Health, attack, location, inventory and equipment do round-trip. There are no `save`/`load` commands at all — both return `I don't understand that command.`; persistence is autosave-on-quit/death and autoload-on-start only, and `game.py:809` carries a stale comment claiming the opposite (*"No automatic load_state call"*).

**ROBUSTNESS (worst impact: silent misinterpretation in combat — `use Healing Potion` silently attacks instead, destroying the one mechanic that could have prolonged a fight).**

| probe | B |
|---|---|
| unknown command | clean — `I don't understand that command.` |
| empty / whitespace-only | ignored, clean |
| invalid direction | clean — `You can't go that way.` |
| take nonexistent | clean — `No such item here.` |
| equip non-equipment | clean — `That item can't be equipped.` |
| talk to absent NPC | clean — `No one here by that name.` |
| verb with no argument | clean — `Attack what?` / `Go where?` / `Take what?` |
| 300-char garbage | clean refusal |
| case (`ATTACK`, `Go North`) | tolerant |
| EOF main prompt | clean — `Exiting game.` |
| EOF inside combat | **TRACEBACK** `game.py:680 raw = input("> ")` |
| corrupt / partial `savegame.json` | recovers, but **silently** — no warning, progress gone |
| unknown command in combat | **silent misinterpretation — performs an attack** |

**MODIFICATION PROBE — PASS, data-only.** Ninth room: added a `belfry` block to `world.yaml` plus `up: belfry` on `library`; worked first try. Weapon damage `attack: 3` → `33`; status read `Attack: 34`. No code touched, nothing broke.

**CONFORMANCE: 41/47 — SIGNIFICANTLY-DEVIATED (core-loop trigger, item 31: no win path exists anywhere in the tree).** Unmet: **22** (the combat loop has no `use` branch — healing mid-fight is absent, not merely unreachable), **23** (two regular monsters, not three), **31** (no win path — the trigger), **33** (no restart offer; `exit(0)`), **38** (6 rooms, not 8), **45** (dialogue node machinery exists but `triggers: {}` is empty for both NPCs, so no branch is authored).

**Attribution.** Weakness-slot seam, doubled placement, missing win path, unplaced amulet, 6 rooms — *model-innate*. Broken `main:run` console-script declaration and a README asserting counts the shipped data contradicts — *interaction*. Layout — *framework-coupled, excluded*.

---

## THE TEN FORCED CHOICES

**PANEL A — DELIVERY**

- **A1 working surface → A.** A's offered surface works once invoked (movement, equip, use, talk, attack, flee, save, load, restart, unequip, a victory and a defeat screen); B's central chain does not — no win path, an unequippable weakness key, `use` dead in combat, and no save/load commands despite shipping JSON persistence.
- **A2 state integrity → A.** A round-trips the whole world including defeated monsters and per-NPC dialogue index; B saves `defeated_monsters` and then rewrites the world on load, resurrecting the orc *and* re-duplicating it.
- **A3 robustness → B.** Both are flawless at the main prompt, but A carries three uncaught tracebacks (EOF at the name prompt, EOF in combat, `load` with no save file — a help-advertised command on a fresh install) against B's one, and A's combat swallow additionally prints a phantom `You save.` that saved nothing.
- **A4 delivered scope → A.** 8 rooms vs 6, 5 placed items vs 4 reachable, 3 regular monsters vs 2, explicit save/load/restart/unequip commands, a real victory screen and a boxed defeat screen — all absent or thinner in B.

**PANEL B — CHARACTER**

- **B5 ambition → A.** Judged on what each set out to build: A reached for a vertical eight-room map, a separate combat engine with a `BossPhase` enum, behaviour-driven monsters, explicit save/load, restart, unequip, per-NPC dialogue indices and a test suite; B's one reach A never attempted — a coded +15 weakness bonus — is a single mis-wired `if`, and does not outweigh the rest.
- **B6 imagination → A.** A has a named world with sensory, place-specific prose and item text that does narrative work (*"A fragment of a luminous crystal, said to weaken the ancient citadel's guardian"*) plus a quest hook from the Guard; B opens on *"Welcome to the Adventure!"*, has no title, no proper noun and a corridor-hub of competent but generic rooms.
- **B7 experience → A**, and it nearly flipped. **Naming the wall: A prints `Sharp Steel Sword` and accepts only `steel_sword`, prints `Grizzled Guard` and accepts only `guard`; I got past that with knowledge a player has no way to obtain** (guessing the snake_case convention, then reading the id `inventory` prints only *after* a successful take), and it counts at full weight here. It loses anyway, because behind that door A delivers a real map with vertical descent, a narrated boss phase change and a fight you can genuinely lose, whereas B — smooth for ten minutes — ends in a three-turn unloseable-in-reverse dragon with an inert quest item, doubled monsters in every room, a potion that cannot be drunk, and no ending at all.
- **B8 craft/UI → B.** This is the axis A's wall is charged on and B takes it cleanly: B accepts the display names it prints, A refuses them; B's help lists exactly the vocabulary that exists while A's omits `examine`, `status`, `go` and `restart`; B names monsters and locations by display name while A prints `A hostile presence looms: final_boss` and `Location: armory`. (A keeps the better status block and the only distinct `Combat>` prompt, and ships `out.txt`/`tmp_output.txt` litter — not enough.)
- **B9 workability → A.** Both mod probes passed identically data-only, so it turns on organisation: A extracts `combat.py`, `ui.py` and `save_load.py` out of a 23K `game.py`, while B is a 30.8K god-module holding help text, status, combat, dialogue and persistence together — and B's seams are in worse places (placement authored through two mechanisms that both fire, a `defeated_monsters` set written and read and never used, a weakness check on the wrong dict).
- **B10 documentation → A.** Accuracy first: B's README asserts *"8 rooms, 5 items, 2 NPCs, 3 regular monsters"* against a shipped world of 6 rooms, 4 reachable items and 2 regular monsters, and its documented install path `pip install .` → `text-adventure` is broken by a `main:run` that does not exist; A's README overreaches once ("branching dialogue") and is otherwise true, with a correct entry point.

---

## TALLIES

**Delivery: A 3–1 · Character: A 5–1**

**OVERALL: A.**

No PANEL SPLIT — both panels point the same way. **Not close, no CLOSE flag**: A is WON with a full state round-trip; B is UNWINNABLE with no win condition anywhere in its tree, and one of its five authored items exists in no room. A's decisive weakness (a parser that refuses the names it prints) is a wall in front of a finished game; B's decisive weakness is that there is no game behind the door.
