# MODE-CONTROL FLIGHT — devstral BATCH structural (current framework) vs the SITTING Floor (batch era)

*Dispatched 2026-08-20 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
tier_20260820-155728/staged/arm01/alpha (CANDIDATE — devstral-2-small-24b,
BATCH structural, 2h, collapsed-diagnose + c0417a8, 12/31 goals); B =
dev/blind_panel/anchors/v2.0/floor-devstral-20260803/alpha (SITTING FLOOR
— same model, 08-03 framework, ALSO batch era). Candidate at A (rotation
honored). Scans clean.

VERDICT: Delivery B(Floor) 4-0 · Character 3-3 (even) · **OVERALL: B
(FLOOR)**. No split (an even panel is not a split), no CLOSE flag. The
floor anchor holds against the batch candidate.

WHY THE CANDIDATE LOST, in the judge's words: "a player cannot obtain a
single item in A" — `handle_take` matches item_id, `look` prints no item
list, and every item needs a snake_case id that appears on no surface the
program prints. Plus a process-killing AttributeError on bare `talk` (a
command its own help advertises), a save silently discarded via an
unimported Player behind a bare except, cross-room combat, monsters that
re-encounter forever (room id compared against a monster-id set), and NO
WIN OR DEFEAT STATE ANYWHERE IN THE TREE. Both artifacts scored 41/47 on
count; the candidate is SIGNIFICANTLY-DEVIATED and the anchor NEAR-FULL
purely on the missing terminal states.

THE MODE QUESTION IS SETTLED — three flights, one model, one day:
  session continuation (16/31) vs floor  → CANDIDATE wins (CLOSE)
  batch arm         (12/31) vs floor  → FLOOR wins 0-4 / 3-3
  (morning session arm, pre-prompt-fix, vs guardian → lost 1-9)
Mode was the only variable between the two current-framework arms:
same model, same framework version, same wall, same prompt fixes. The
SESSION walk produced a fully-connected 8/8 world; the BATCH arm
produced a one-way orphan holding the only healing item, an unplaced
NPC with a full three-state dialogue tree, and an unplaced chest —
the exact placement pathology the batch-era anchor also carries (its
own boss wing is stranded). Session structural is the mode for this
model. This is the campaign's cleanest single-variable mode evidence.

ALSO NEW: the candidate's dialogue engine writes back the state it just
read (dialogue_states[key] = current_state), so the boss-weakness hint
behind option 1 is permanently unreachable; and a complete
handle_examine ships as DEAD CODE shadowed by a worse inline version.
On the anchor side the judge found a defect no prior flight caught:
`.gitignore` line 50 excludes `*.yaml`, so the README's own git-clone
workflow yields a tree with no world.yaml — FileNotFoundError on
launch. Charged as interaction, and it cost the anchor B10.

Judge's verbatim record follows.*

---
# FLIGHT RECORD — TIER_RUBRIC v2.1 · packet `flight_20260820_batch_vs_floor`

Both artifacts played interactively first; source consulted only to explain observed failures and to run the mandatory placement/room-graph audit. Play was done on copies in `<packet>/scratch/A` and `<packet>/scratch/B`.

---

## ARTIFACT A — record

**PREMISE LINE.** You wake as a prisoner in a Dark Lord's dungeon, and the only door is north — through his torch-lit hallway, his armoury, and finally his throne room. A caged sorcerer and a knight who serves the Dark Lord both quietly tell you the same thing: an amulet somewhere in the dungeon was forged to unmake him.
> *"Ah, a visitor! I've been trapped here for ages. The Dark Lord who rules this dungeon has a weakness…"*

**COMPLETABILITY CLASS: NO-TERMINAL-STATES.**
A grep of the whole tree for `win|victor|congratul` returns nothing. I did kill the boss (by spamming `attack` — damage is still applied on turns the player is already dead) and the entire response was one combat line:

```
> You defeated the Dark Lord!
> You're not in combat.
```

Nothing else. Death is equally non-terminal — the run continues indefinitely past zero:

```
> The Dark Lord defeated you!
> The Dark Lord defeated you!            (×12)
> Health: -42/20
```

**CONFORMANCE: 41/47. Unmet: 23, 28, 31, 32, 33, 38. Verdict: SIGNIFICANTLY-DEVIATED — core-loop trigger.**
Item 31 (win path) is *absent from the tree*, not merely unreachable; item 32/33 (defeat screen + restart) likewise — death emits a combat message and play continues. Item 23: two monsters authored, one of them the boss, so one regular monster of the three asked for. Item 28: no boss phases in code or data. Item 38: six rooms authored, and one of those is a one-way orphan (below). The count alone (6) would have been NEAR-FULL; the missing terminal states are what fire the binary.

**ROOM GRAPH & PLACEMENT AUDIT.**
- 6 rooms authored. **5 reachable** from `prison_cell`.
- **Disconnected component:** `hidden_vault` declares `north: dungeon_hallway`, but `dungeon_hallway` has no exit back into it. It is a one-way orphan — and it holds `potion_of_healing`, the game's only healing item, so healing is unobtainable.
- **Unplaced entity:** `fallen_knight` — a fully authored NPC with a three-state branching dialogue tree, in **no room's `npcs` list**. Half the NPC content ships in a room that does not exist.
- **Unplaced entity:** `locked_chest` — in the item registry, in no room's `items` list.

**SEAM BUGS — and yes, a seam bug is what stopped me.**
1. **Item ids vs display names.** `handle_take` matches `item_id == item_name`. Every printed surface uses the display name; `look` prints no item list at all; the Prison Cell description does not mention a sword. So:
   ```
   > take Rusty Sword        →  There is no rusty sword here to take.
   > take rusty_sword        →  You took the Rusty Sword.
   ```
   **A player cannot obtain a single item in this game.** I got the sword by reading `world.yaml` for the snake_case keys — knowledge a player has no way to obtain.
2. **`talk to` cannot match.** The parser splits on the first space, so `talk to caged sorcerer` reaches `handle_talk` with target `"to caged sorcerer"`, which is compared against `npc.name` (`"Caged Sorcerer"`). Both the README and the in-game help document `talk to [npc]`.
   ```
   > talk to caged_sorcerer  →  There is no to caged_sorcerer here to talk to.
   > talk caged sorcerer     →  Caged Sorcerer says: Ah, a visitor! …
   ```
   I found the working form by reading `parser.py` against `engine.py`.
3. **Room id compared to a monster-id set.** `handle_move` gates the encounter on `new_room.id not in self.state.defeated_monsters`, while `handle_attack` adds `monster.id` to that set. Defeated monsters therefore re-encounter forever — verified: killed the Skeleton, walked south and back, `You entered Dungeon Hallway and encountered a Skeleton!`
4. **Dialogue never advances.** `handle_talk` writes `dialogue_states[key] = current_state` — the state it just read. The numbered options print but nothing accepts them (`talk 1` → *"There is no 1 here to talk to."*). The boss-weakness hint lives behind option 1 and is permanently unreachable.
5. **Two divergent `examine`s.** A complete `handle_examine` method sits below the dispatcher as dead code; the inline version that actually runs looks up `npc.dialogue['default']['response']`, a key no NPC has, so `examine caged sorcerer` returns *"You see nothing unusual."*

**STATE INTEGRITY — fails, and the reset rewrites the world.**
No `save` or `load` command exists (`save` → *"I don't understand that command."*). `load_game()` runs at startup, references an unimported `Player`, and hides the resulting `NameError` behind `except Exception: return None`. A hand-written valid save was silently discarded:

```
save.json: health 11, attack 9, location armory, weapon rusty_sword
> status → Health: 20/20  Attack: 5  Location: Prison Cell
```

In-session: combat is not room-scoped — I killed the Dungeon Hallway skeleton *from the Prison Cell*. `start` (a command the help advertises) builds a new Player while the world keeps its mutated monster HP and the combat engine stays bound to the discarded player, producing an invulnerable ghost. Round-trip: **FAILS.**

**ROBUSTNESS BATTERY (worst impact: process-killing traceback on an advertised command).**

| # | probe | result |
|---|---|---|
| 1 | unknown command (`XYZZY`) | clean refusal |
| 2 | empty input | clean refusal |
| 3 | whitespace only | clean refusal |
| 4 | verb, no arg (`take`/`use`/`equip`/`examine`) | clean refusal |
| 5 | **`talk` with no arg** | **uncaught `AttributeError: 'NoneType' object has no attribute 'lower'` — process dies, all progress lost** |
| 6 | invalid direction (`go up`) | clean refusal |
| 7 | move into a wall | *"You can't go that way."* |
| 8 | take nonexistent item | clean refusal |
| 9 | `attack`/`flee` outside combat | *"You're not in combat."* — but see cross-room combat above |
| 10 | EOF / Ctrl-D | uncaught `EOFError` traceback |

Also: uppercase accepted; long garbage refused cleanly; `inventory` is parsed by the parser and has no dispatch branch, so it refuses itself.

**MODIFICATION PROBE.** Added a seventh room (`crypt`, `east: dungeon_hallway`) plus one `west: crypt` line on the hallway, and changed `rusty_sword.attack_bonus` 2→9. **Two edits, one file (`world.yaml`), zero code touched, worked first try** — `Attack: 14`, room enterable and describable. Nothing broke. Note the authoring blind spot: the new room's contents would be invisible in play, because `look` prints no exits or items.

**FURTHEST POINT.** Boss Chamber; Dark Lord killed. **Reading the source was required** — without `world.yaml` I could not have taken any item, and without `parser.py` + `engine.py` I could not have talked to the one placed NPC.

**ATTRIBUTION.** *model-innate:* all five seam bugs, the absent win/defeat states, the orphan room, the unplaced knight and chest, the dead shadowed `examine`. *interaction:* two shipped tests, neither importable as laid out (`ModuleNotFoundError: engine`), and one that asserts `"A rusty sword that has seen better days."` against a `world.yaml` reading `"A basic sword…"` — it fails on assertion even with the path fixed, so it was never run; `start` returning `None` into a `print`. *framework-coupled:* layout, `pyproject.toml`, `.python-version` (excluded / identical). Everything above is charged regardless of label.

---

## ARTIFACT B — record

**PREMISE LINE.** A cave crawl: you step into a dark, damp mouth in the rock, arm yourself from a hallway that branches to an armoury and a treasure room, and fight your way toward a Dark Lord waiting at the end of a dungeon corridor. The corridor does not connect to anything you can stand in.
> *"The air is thick with dark energy. A massive boss stands before you."*

**COMPLETABILITY CLASS: UNWINNABLE.**
The win link exists and is correct — `if monster_id == "boss": self.victory = True; self.game_over = True`. The boss is in `boss_room`, which is reachable only from `dungeon`, which nothing in the playable component links to. I cleared every reachable room and every reachable exit; the boss cannot be reached from `entrance` by any sequence of moves.

**CONFORMANCE: 41/47. Unmet: 3, 23, 26, 28, 33, 38. Verdict: NEAR-FULL.**
Item 3: there is no `go` verb — `go north` → *"I don't understand that command."*; movement is bare direction words only. Item 23: two regular monsters (Goblin, Dragon) of the three asked for. Item 26: all three monsters carry `behaviour: aggressive` — literally the shared script the brief asked models to avoid. Item 28: no boss phases anywhere. Item 33: death terminates but offers no restart. Item 38: nine rooms authored, four in the start component. Six unmet is under the 10-item threshold and the core loop (terminal win *and* terminal defeat) is present in code, so NEAR-FULL stands.

**ROOM GRAPH & PLACEMENT AUDIT — this is the decisive defect.**
- 9 rooms authored. **4 reachable**: `entrance → hallway → {treasure_room, armory}`.
- **Disconnected component:** `{dungeon, boss_room, secret_room}` — the entire boss wing. It contains **the final boss** and **`legendary_sword`, the boss-weakness item** (*"the only weapon that can defeat the boss"*). No room in the reachable set has an exit into it.
- **One-way orphans:** `garden` (`south: armory`, but `armory` has no `north`) and `library` (`north: hallway`, but `hallway` has no exit to it). Both can be exited from and never entered.
- **Unreachable NPCs:** `wise_man` (secret_room), `gardener` (garden), `librarian` (library). Only `guard` is placed in reachable rooms — and the guard is dead too, for a different reason:

**SEAM BUG — `condition: none`.** Every NPC's first dialogue line is authored with `condition: none`. The engine's condition dispatch handles `talked_once`, `has_item`, `killed_monster` and `visited_room`, then falls through to `else: current_index += 1; continue` — where `continue` continues the **outer `for npc_id in self.current_room.npcs` loop**, not the dialogue selection. The print is skipped, the loop exits, and control reaches the not-found line. Two internally reasonable files; one unhandled key between them:

```
(standing in Entrance, whose npcs list is [guard])
> talk to Guard   →  There is no guard here.
```

The entire conversation layer — four NPCs, the boss-weakness hint, the guard's four-state gate — is inert. Compounding it, `show_room` prints Exits, Items and Monsters but **never NPCs**, so a player has no evidence any NPC exists.

**Second (dead-code) seam:** a duplicate module-level `equip_item` sits after the class, gated on `item.type == "equippable"` — a type no item in `world.yaml` has. It is never called; the bound method that runs is correct.

**STATE INTEGRITY — unreachable, but sound where it exists.**
No `save`/`load` command is wired (`save` → *"I don't understand that command."*), so no round-trip is possible in play. The `save_game`/`load_game` methods themselves are internally correct for what they cover — health, attack, defense, inventory, equipment, location, completed monsters — and re-derive `current_room` from the saved location. Not covered: room item lists and `dialogue_state`, and killed monsters would return to `room.monsters` after a reload since those lists come fresh from YAML. **In-session state is coherent**: killed monsters vanish from the room and from `look`, items move between room and inventory correctly, equipment bonuses apply once and show in `status`. Round-trip: **UNREACHABLE**, but the code would not rewrite the world.

**ROBUSTNESS BATTERY (worst impact: EOF traceback).**

| # | probe | result |
|---|---|---|
| 1 | unknown command | clean refusal + pointer to help |
| 2 | empty input | clean skip, no message, no crash |
| 3 | whitespace only | clean skip |
| 4 | verb, no arg (`take`/`use`/`equip`/`examine`/`attack`/`talk`) | clean refusal, no crash |
| 5 | invalid direction (`up`) | clean refusal |
| 6 | move into a wall | *"You can't go that way."* |
| 7 | take nonexistent item | *"There is no rusty sword here."* |
| 8 | attack absent monster | *"There is no dragon here."* |
| 9 | **`flee` outside combat** | **silent misinterpretation — teleports you through the first-listed exit with no combat check** (`> flee` in the Entrance → *"You fled to the Hallway."*) |
| 10 | EOF / Ctrl-D | uncaught `EOFError` traceback |

Also: uppercase accepted; item names case-insensitive; long garbage refused cleanly; healing overshoots max health (`Health: 29` on a 20 base); equipping a second weapon stacks its bonus without removing the first.

**MODIFICATION PROBE.** Added a fifth reachable room (`crypt`, `south: hallway`) plus one `north: crypt` line on the hallway, and changed `sword.stats.attack` 5→12. **Two edits, one file (`world.yaml`), zero code touched, worked first try** — `Attack: 17`, room enterable, and `look` correctly showed `Exits: south, east, west, north`. One thing did break: my first attempt used `northwest`, which `show_room` happily advertised in the exits line but the dispatcher refused, because direction words *are* the verbs and the four cardinals are hardcoded in `handle_command`'s `elif` chain. Adding a non-cardinal direction requires a code edit.

**FURTHEST POINT.** Treasure Room, fully cleared: Rusty Sword and Wooden Shield equipped, Health Potion drunk mid-fight, Goblin and Dragon both killed, Magic Amulet taken and equipped, all four reachable rooms and every exit exhausted. **Reading the source was not required for any of that** — I played it as a player. I needed the source only to learn *why* every `talk to` fails and *that a boss exists at all*.

**ATTRIBUTION.** *model-innate:* the `condition: none` seam, the disconnected boss wing, the one-way garden/library, the missing `go` verb, all-`aggressive` behaviours, the 433-line god class and its 100-line four-times-copy-pasted `talk_to_npc`, the dead `equip_item`. *interaction:* `.gitignore` line 50 `*.yaml`, which excludes the game's entire world file — verified with `git add -A`, world.yaml is not staged, and a tree assembled the way the README's own `git clone` workflow would produce dies at `FileNotFoundError: 'world.yaml'`. *framework-coupled:* layout, `pyproject.toml`, `.python-version` (excluded / identical). All charged regardless of label.

---

# THE TEN AXES

## PANEL A — DELIVERY

| axis | choice | justification |
|---|---|---|
| **A1 working surface** | **B** | B's take/drop/use/examine/equip/attack/flee all work on the names it prints; A's take and examine accept only never-printed snake_case ids, its advertised `talk to` can never match, `start` prints `None`, and `inventory` parses to no handler. |
| **A2 state integrity** | **B** | A silently discards a valid `save.json` (unimported `Player`, swallowed by a bare `except`), re-encounters monsters it recorded as defeated by comparing a room id to a monster-id set, fights across rooms, and its `start` reset leaves an orphaned player at −42 HP in a world it no longer owns; B's in-session state is coherent and its unreachable save/load is at least internally correct. |
| **A3 robustness** | **B** | A kills the process on bare `talk` — a command both its README and its own help advertise — where B refuses every malformed input cleanly; both share the EOF traceback, and B's worst extra is a silent `flee` teleport. |
| **A4 delivered scope** | **B** | B lands a playable loop end to end — arm up, equip, heal mid-fight, kill two monsters, terminal defeat — while A lands movement and combat only, with zero items obtainable and no terminal state of any kind. |

**Panel A tally: B 4–0.**

## PANEL B — CHARACTER

| axis | choice | justification |
|---|---|---|
| **B5 ambition** | **B** | Judged on what each set out to build and not on what survived: B reached for 9 rooms, 4 NPCs, 7 items, 3 monsters, a 50-HP boss behind a hidden secret room gating a legendary weapon, four kinds of world-state dialogue condition (`has_item`/`killed_monster`/`visited_room`/`talked_once`) with a skip-ahead search for the next satisfiable line, and both a win and a lose terminal — against A's 6 rooms, 2 NPCs, 5 items, 2 monsters, a fixed-state dialogue tree, and no terminal states designed at all. |
| **B6 imagination (world & voice)** | **A** | A has an actual premise — you are the prisoner — and its non-obvious idea is a knight *in the Dark Lord's own service* who hands you the means to kill his master (*"Beware the Dark Lord… he is not what he seems"*), where B's boss room describes itself as containing "a fearsome boss" and its nine rooms are a noun list (Garden, Library, Armory) with no reason to be in a cave. |
| **B7 experience (felt play)** | **B** | Judging the player's experience and not mine: in A a player cannot pick up one item — `look` lists nothing, the room prose names a "tattered cloak" the parser refuses, and I obtained the sword only by reading `world.yaml` for `rusty_sword`, and reached the one NPC only by reading `parser.py` to learn that the documented `to` must be dropped; both walls stand at full weight against A. B lists exits, items and monsters every turn, reports the monster's remaining HP each round, and let me clear its whole reachable world without opening a file. |
| **B8 craft (UI)** | **B** | A refuses every name it prints — the explicit input-tolerance failure — advertises `start` (prints `None`) and `talk to` (never matches), omits inventory from `status`, and echoes raw targets back (*"There is no to old man here to talk to"*); B accepts display names case-insensitively, its help matches what its dispatcher actually takes, and its refusals are uniform. A's only craft win is the ASCII banner, which spells "SPECTU" and emits a `SyntaxWarning` on every launch. |
| **B9 workability** | **A** | The modification probe tied — both took one `world.yaml` edit and no code — so this turns on organisation: A splits parser, combat, saver, world, models and state into real modules with the seams where you would put them, against B's single 433-line class whose 100-line `talk_to_npc` copy-pastes the same skip-ahead loop four times over a `_check_condition` helper directly beneath it, and B's `.gitignore` excludes its own world file. A's hazards (dual `self.state`/`self.game_state`, the swallowed `NameError`, a dead-but-correct `handle_examine` shadowed by a worse inline one) are matched in kind by B's dead `equip_item` and hardcoded direction list. |
| **B10 documentation** | **A** | Accuracy against play first: B's README's very first control line, `go [direction]`, is refused outright by the program, and following B's documented `git clone` install yields a tree with no `world.yaml` and an immediate `FileNotFoundError` — a stranger cannot run it at all. A's README gets movement and the entry point right; both then over-claim equally (save/load, `talk to`, defeating the boss), and A additionally over-claims a win that does not exist. |

**Panel B tally: A 3 – B 3.**

---

# OVERALL: **B**

**Panel tallies, reported separately and not summed: Delivery B 4–0 · Character A 3 – B 3.**

**No PANEL SPLIT.** The panels do not disagree on direction — Panel A is a clean sweep for B and Panel B is dead even, which is a different thing from a split and does not halt a placement. What the even Character panel means is worth stating: A is the better-*imagined* and better-*organised* artifact, and B is the more *ambitious* and far more *usable* one. A wrote a real premise, two NPCs with motives including a traitor, and eight modules with sane boundaries; B wrote a bigger world with a world-state dialogue engine and actual terminal states, and shipped it inside one sprawling class with its data file gitignored.

**No CLOSE flag.** The overall is not within noise. The decisive fact is not a tally but a floor: **a player cannot obtain a single item in A** — `look` lists nothing, and every item requires an internal snake_case id that appears on no surface the program ever prints. I only got past that with `world.yaml` open. B, by contrast, I played as a player from the entrance to a cleared treasure room without reading anything. Both artifacts are stopped by their own defects — A by a pair of parser/handler seam bugs, B by a disconnected component that strands its entire boss wing, its boss-weakness item and three of its four NPCs — but A additionally has no win condition anywhere in its tree and no terminal state on death, which is why A is SIGNIFICANTLY-DEVIATED and B is NEAR-FULL on the same 41/47 count.
