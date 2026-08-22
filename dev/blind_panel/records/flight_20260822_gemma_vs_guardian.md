# GUARDIAN GATE — PART 2 (corroborating probe): gemma-4-31b vs the SITTING Guardian

*Dispatched 2026-08-22 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
tier_20260822-125650/staged/arm01/alpha (PROBE — gemma-4-31b, session
structural, grinder league, 122 min / 53 cycles, 16/31 goals, CoT
RESTORED and class-attribute fix live); B =
dev/blind_panel/anchors/v2.0/guardian-gptoss-20260803/alpha (SITTING
GUARDIAN). Probe at A (rotation honored). Scans clean.

VERDICT: Delivery **2-2 EVEN** · Character GUARDIAN 5-1 · **OVERALL:
GUARDIAN**. No panel split (an even panel has no direction to contradict).
Judge self-flags that Delivery is one axis from flipping either way (A2
and A3 are the reversible calls) but that the overall does not turn.

**THE GATE IS NOW CLOSED FROM BOTH SIDES.** Neither probe reversed its
v2.0 0-5 sweep:
  * `qwen3-next-coder-80b-a3` (the DECISIVE, confound-free probe) —
    lost, 2026-08-22.
  * `gemma-4-31b` (corroborating only, doubly confounded) — lost here.
Per the rule pre-registered 2026-08-20 before either arm ran, the
sitting guardian HOLDS and `guardian-candidate-gptoss-20260819` remains
a CANDIDATE. The gpt-oss 8-2 stands as a same-family framework gain,
not proof the anchor decayed. NOTHING IN THE LADDER MOVES.

**AND THE CONFOUND CUT THE OTHER WAY, WHICH IS THE USEFUL PART.** Gemma
came into this arm with every advantage the campaign could give it: its
thinking restored (6a87aca — the 0-5 baseline was set with the dial
silently OFF), honest CoT telemetry (829fd91), the class-attribute
repair route (19d61d2), grinder league so the wall rather than a stale
cycle cap bounds it, and a run that scored 16/31 against the previous
arm's 11/30. It still lost, and lost the character panel 5-1. So the
framework fixes are real — the ARM improved 45% — and they did not move
the artifact's PLACEMENT at all. That distinction is worth keeping:
run-shape gains and artifact quality are not the same axis.

**Conformance is where the gap is starkest: gemma 30/47 firing BOTH
triggers (17 unmet ≥ 10, AND core-loop item 31 absent) against the
guardian's 41/47 firing only core-loop.** The verbs `go`, `examine`,
`attack` and `flee` appear NOWHERE in gemma's tree — four of the five
commands its own README documents — and its combat loop reads no player
input at all: the fight scrolls past on room entry with zero decisions
in it. Its boss is unkillable in principle (150 HP against a maximum
deliverable 105, with the only armour sitting inside the boss room that
auto-fights on entry).

CHARGED, model-innate: an UNPLACED NPC ("Grog the Smith", authored with
two dialogue lines, referenced by no room); a save that omits world item
state so every take/drop/consume is undone on load; a title-screen
EOFError.
CHARGED, interaction: TWO SHIPPED FAILING TESTS — `test_examine_command`
asserts against an `examine` command the tree never implements, and the
failure message quotes the game refusing its own advertised command
(`pytest`: 2 failed, 7 passed). Plus a README pointing at a
`requirements.txt` that does not exist and an unused `rich` dependency.

THE SYMMETRY HOLDS AGAIN: both artifacts UNWINNABLE with NO WIN STATE
ANYWHERE IN EITHER TREE. That is now three consecutive flights and six
artifacts across four models and three framework eras with the same
absence. The judge's closing line is the one to keep: *"A seam bug is
what stopped me in B. It is not what stopped me in A — in A there was
nothing to stop."*

Judge's verbatim record follows.*

---
# FLIGHT RECORD — Artifact A vs Artifact B
**Instrument:** TIER_RUBRIC v2.1 · ten forced choices, two panels, tallied separately and never summed.
**Method:** Both played interactively through their own command lines before any source was opened. Source was consulted only to explain failures already observed in play; every such instance is named below.

---

## ARTIFACT A — RECORD

### Premise line
A stock high-fantasy errand: you are a nameless traveller who walks north out of a village, through a forest and a cave, into a dragon's lair, killing whatever the room contains on arrival. It is a four-room corridor with no branches and no choices, in which the player's only verbs are logistics — the fighting happens to you, not through you.
> *"A vast chamber filled with gold and the heat of a sleeping beast."*

### Entry-point ledger
| documented way to start | result |
|---|---|
| `python main.py` (README) | **Works.** Title banner, Enter-to-start, playable. Works from any cwd. |
| `pip install -r requirements.txt` (README) | **Broken** — no `requirements.txt` anywhere in the tree. |
| `pip install.` (README) | **Malformed** — missing space. `pyproject.toml` declares `rich>=13.0.0`, which no file imports. |

### Room graph walk and placement check
- **4 authored rooms, 4 reachable** from `village_square` — one component, no disconnected wing.
- **UNPLACED ENTITY:** NPC `blacksmith` / **"Grog the Smith"**, fully authored with two lines of dialogue, is referenced by **no room's `npc_id`**. He exists and can never be met. Confirmed by walking every room and by inspecting `world_data.py`.
- No unplaced items or monsters. All 3 items and all 3 monsters are placed.

### Completability class: **UNWINNABLE**
Two independent reasons, both confirmed:

1. **Arithmetically impossible.** The Ancient Dragon has 150 HP and deals 15/turn. The player has 100 max HP and, with the only weapon in the game equipped, deals 15/turn. Maximum damage deliverable before death is 105. My best stock attempt reached the lair at 69 HP and removed 75 of 150:
   ```
   You attack the Ancient Dragon for 15 damage!
   The Ancient Dragon attacks you for 15 damage!
   Your Health: 0/100
   You have been defeated in combat...
   ```
   The Leather Armor that would change this arithmetic sits **inside the boss room**, and combat fires automatically on entry, so it can never be equipped before the fight.

2. **There is no win state in the tree at all.** I raised the Iron Sword's damage in my scratch copy and killed the dragon. The entire terminal event is:
   ```
   The Ancient Dragon has been defeated!
   Victory! You defeated the Ancient Dragon.

   > (game continues normally in an empty room)
   ```
   `grep -i congratul\|victor\|"you win"\|"won the"` over the whole tree returns exactly that one line, which is the generic per-monster kill message. Nothing anywhere sets a won state.

**Furthest point reached:** Dragon's Lair, Ancient Dragon engaged, dead at 0 HP on every attempt. **Source reading was not required to get there** — but `help` was: the README's movement verb does not work (below).

### Conformance tally: **30 / 47**
**Unmet:** 3, 7, 10, 11, 22, 23, 26, 28, 29, 30, 31, 33, 36, 38, 39, 43, 46

**Verdict: SIGNIFICANTLY-DEVIATED.** Both triggers fire — 17 unmet exceeds the 20% threshold (≥10), **and** core-loop element **item 31 (win path)** is absent from the tree, not merely unreachable.

Notes on contested rows: item 3 unmet because the verb `go` is absent from the parser entirely; items 10/11 unmet because `attack` and `flee` appear nowhere in the tree; item 22 unmet because the combat loop reads no input at all; item 23 unmet because only two of the three monsters are regular (the third is the boss); item 26 unmet because `Monster` has no behaviour field and all three run one shared loop; item 36 unmet because the save omits room state, which item 35 explicitly names; item 44 counted **met** — two NPCs are authored, and the unplaced one is charged on the axes, not here.

### State integrity
Explicit `save` / `load` commands. Player stats, inventory, equipment, `defeated_monsters` and `npc_dialogue_state` all round-trip correctly — I killed the Green Slime, saved, relaunched, loaded, and re-entered the Forest Path with no fight. NPC dialogue index round-trips exactly (`{"village_elder": 2}` → third line on resume).

**Defect: the save contains no world item state, so loading rewrites the world.** I took the Health Potion and *drank it*, then saved and reloaded in a fresh process:
```
> (after load)
--- Forest Path ---
You see: Health Potion          ← the potion I consumed is back
--- Inventory ---
Your pockets are empty.
```
Every take, drop and consumption is undone on load. Save file is 251 bytes; there is no `rooms` key.

### Robustness battery
| # | probe | result | impact |
|---|---|---|---|
| 1 | unknown command (`xyzzy`) | `I don't understand that command. Type 'help'...` | clean refusal |
| 2 | empty input | silent re-prompt | clean |
| 3 | whitespace only | clean refusal | clean |
| 4 | move with no exit (`move up`) | `You cannot go up from here.` | clean refusal |
| 5 | verb with no argument (`move`, `take`) | `I don't understand that command.` | clean, but mislabels a missing target as an unknown verb |
| 6 | nonexistent item (take/drop/use/equip) | `That item isn't here.` / `You aren't carrying that.` | clean, specific |
| 7 | `talk to Grog the Smith` (not in room, in no room) | **Elder Thorne answers.** | **silent misinterpretation** — `talk` is parsed as a no-target verb and the target is discarded |
| 8 | case variation | `MOVE NORTH` → `You cannot go NORTH from here.` | direction argument is case-sensitive; item names are not |
| 9 | mid-combat command | **not probeable** — combat accepts no input | n/a (delivery failure, charged A1/A4) |
| 10 | EOF / Ctrl-D | at `> ` prompt: clean `Forcing exit...`; **at the title screen: uncaught `EOFError` traceback** | **worst impact: traceback out of `main.py:14`** |

**Worst impact: uncaught EOFError traceback at the title screen.**

### Modification probe (B9)
Added a fifth room (`Old Well`, hung east off `village_square` with an item) and changed the Iron Sword's damage 10 → 40. **Touched exactly one file, `world_data.py`: one new `Room(...)` entry, one new key in an existing `exits` dict, one integer.** Nothing else edited, nothing broke — the room was reachable and described correctly on the first run, and the damage change propagated (`You attack the Ancient Dragon for 45 damage!`). Clean probe.

### Interaction-labelled findings (in comparison per §4)
- **Two shipped FAILING tests.** `tests/test_examine_command.py` asserts against an `examine` command the tree never implements. `pytest` gives `2 failed, 7 passed`, with the failure message literally quoting the game refusing its own advertised command:
  ```
  AssertionError: Expected item description 'A shimmering red liquid that restores health.'
  to be in output, but got: I don't understand that command.
  ```
  The model wrote a red test for a missing feature and shipped it red.
- `pyproject.toml` declares an unused `rich` dependency; the README points at a `requirements.txt` that does not exist.

### Attribution
- Missing `go`/`examine`/`attack`/`flee` verbs; non-interactive combat; 4 rooms / 3 items; unplaced blacksmith; no win state; title-screen EOFError — **model-innate**.
- Shipped failing tests; phantom `requirements.txt`; unused `rich` dependency — **interaction**.
- Project layout — **framework-coupled, excluded per §4**.

---

## ARTIFACT B — RECORD

### Premise line
You wake in a stone keep with a draft coming from the north and work outward through a corridor into an armory, a ruined garden and a library, gathering a sword, a shield and a rumour, before climbing to a throne room where a Shadow Dragon waits and an obsidian key lies on the floor. It is a small hub-and-spokes dungeon built around one promise — that the key will unmake the dragon — which the game states twice and then cannot keep.
> *"A dimly lit hall with stone walls. A faint draft whispers from the north."*

### Entry-point ledger
| documented way to start | result |
|---|---|
| `python main.py` (README) | **Works.** ASCII-art `Set` banner, Enter-to-begin, playable. Works from any cwd (`world.yaml` is resolved via `Path(__file__)`). |
| `text-adventure` console script (README, listed **first**) | **Broken.** `pyproject.toml` declares `text-adventure = "main:run"`; `main.py` defines `print_title` and `main`, and no `run`. |
| `pip install .` | Fine — `PyYAML>=6.0` is the one real dependency and is actually used. |

### Room graph walk and placement check
- **6 authored rooms, 6 reachable** from `start` — one component, no disconnected wing. (The brief asks for eight.)
- **UNPLACED ENTITY:** item `amulet` / **"Silver Amulet"** is fully authored in `world.yaml`'s items list with a name and description, and appears in **no room's `items` list**. It exists and can never be found. Confirmed by walking every room and by inspecting the data file.
- All 3 monsters and the other 4 items are placed — **but every monster is placed twice** (see the seam bug below).

### Completability class: **UNWINNABLE**
Two independent reasons, both confirmed:

1. **THE SEAM BUG — the boss weakness is gated on a slot its own item type can never occupy.** This is what stopped me, and it is exactly the archetype the campaign keeps finding. The Old Man says *"The key to the dragon's heart lies hidden"*; the Librarian says *"Seek the obsidian key; it may turn the tide"*; the key's own description says *"Legends say it can weaken the dragon."* You find it, you take it, and then:
   ```
   > take Obsidian Key
   You pick up the Obsidian Key.
   > equip Obsidian Key
   That item can't be equipped.
   > use Obsidian Key
   You can't use that right now.
   > attack Shadow Dragon
   You strike for 6 damage. Shadow Dragon health is now 44.
   The Shadow Dragon slashes you for 8 damage. Your health is now 12.
   ```
   The weakness check in `game.py:742` reads `if self.equipment["weapon"] == "boss_key":` — it fires only when the key is in the **weapon slot**, while `equip_item` accepts only `Weapon` and `Armor` instances and `boss_key` is `type: key`. Two internally reasonable files that cannot meet. **I learned this by reading `game.py`; a player has no way to obtain it** — every in-game signal insists the key is the answer.

   Arithmetic without the weakness: player 20 HP / attack 6 max, dragon 50 HP / attack 8 rising to 12. Best line, including fleeing to drink the one potion in the game, removes 36 of 50 HP. There is no second potion and no regeneration.

2. **There is no win state in the tree at all.** I raised the Rusty Sword's attack in my scratch copy and killed the dragon, second phase and all:
   ```
   You strike for 41 damage. Shadow Dragon health is now 9.
   The Shadow Dragon roars! It enters a furious second phase, increasing its attack!
   The Shadow Dragon slashes you for 12 damage. Your health is now 8.
   You strike for 41 damage. Shadow Dragon health is now 0.
   The Shadow Dragon collapses!

   > (game continues normally)
   ```
   `grep -i win\|victor\|congratul` over `game.py` returns nothing but `defeated_monsters`. Nothing anywhere sets a won state.

**Second seam bug — every monster is doubled.** `world.yaml` places monsters twice: in each room's `monsters:` list *and* via each monster's own `location:` field. `world.py` honours the room lists and then appends by `location` on top, so every room reports its monster twice:
```
Danger! Monsters present: Goblin Guard, Goblin Guard
Danger! Monsters present: Shadow Dragon, Shadow Dragon
```
Both entries are the same object id, and combat removes only one on death — so after killing the dragon the room still lists a second one, which cannot be fought at all (its health is already 0, so the combat `while` never runs):
```
Danger! Monsters present: Shadow Dragon
> attack Shadow Dragon
You engage the Shadow Dragon!
> (nothing; falls straight through)
```

**Furthest point reached:** Throne Room, Obsidian Key in inventory, Shadow Dragon at 32/50, dead at −4 HP. At stock stats I never saw phase two; I only confirmed the second phase exists after modifying the world file.

### Conformance tally: **41 / 47**
**Unmet:** 22, 23, 31, 33, 38, 45

**Verdict: SIGNIFICANTLY-DEVIATED.** The count trigger does **not** fire (6 < 10); the **core-loop trigger does** — item 31 (win path on defeating the boss) is absent from the tree.

Notes on contested rows: item 22 unmet because the combat loop handles only `flee` and routes literally everything else to a basic attack — mid-combat item use exists nowhere; item 23 unmet because only two of three monsters are regular; item 29/30 counted **met** under the presence rule (the weakness check and the placed key both exist in the tree; the miswiring is charged on the axes); item 45 unmet because although `talk_to` implements a `{node}_next` progression mechanism, `world.yaml` authors exactly one dialogue node per NPC and empty `triggers`, so the shipped behaviour is a fixed single line — which the operator's ruling places explicitly *below* the "staged progression MEETS" bar; item 38 unmet at 6 rooms.

### State integrity
Save contract is the richer of the two — it includes `rooms` item lists, `defeated_monsters`, `npc_progress`, equipment and location, and room items round-trip correctly (I took the sword, left the shield, reloaded, and found exactly the shield).

Three defects:

1. **Defeated monsters resurrect.** `defeated_monsters` is written to the save and restored into the set, but nothing rebuilds the room monster lists from it — `load_world()` re-places every monster from scratch. I killed one Orc Warrior, saved, relaunched:
   ```
   (before save)  Danger! Monsters present: Orc Warrior
   (after reload) Danger! Monsters present: Orc Warrior, Orc Warrior
   ```
2. **There are no `save` or `load` commands.** Neither verb is in the help, the parser, or the dispatcher — `> save` returns `I don't understand that command.` Saving happens only on quit/EOF/death; loading happens **silently and unconditionally at launch**. There is no reset, and no way to start a new game except deleting `savegame.json` by hand, which nothing documents and `.gitignore` hides.
3. **Death is persisted, and then resumed.** `initiate_combat` calls `write_save()` and then `exit(0)` on death. The next launch silently reloads the corpse:
   ```
   Press Enter to begin...Loaded saved game.
   > status
   Health: -1
   Attack: 1
   Location: Long Corridor
   ```
   You walk the dungeon at −1 HP, in a game that offers no restart.

### Robustness battery
| # | probe | result | impact |
|---|---|---|---|
| 1 | unknown command (`xyzzy`) | `I don't understand that command.` | clean refusal |
| 2 | empty input | silent re-prompt | clean |
| 3 | whitespace only | silent re-prompt | clean |
| 4 | move with no exit (`go up`) | `You can't go that way.` | clean refusal |
| 5 | verb with no argument | `Go where?` / `Attack what?` / `Examine what?` / `Talk to whom?` | clean, specific, better than A |
| 6 | nonexistent item | `No such item here.` / `You don't have that item.` | clean |
| 7 | `talk to nobody` | `No one here by that name.` | clean refusal |
| 8 | case variation (`GO NORTH`, `TAKE nothing`) | fully case-insensitive; bare `north` also works | clean |
| 9 | **mid-combat command** | `use Healing Potion` during a fight → **you swing your sword instead**; potion still in inventory, no message | **silent misinterpretation on the core mechanic** |
| 10 | EOF / Ctrl-D | at `> ` prompt: clean `Exiting game.` + save; **at title screen: uncaught `EOFError`; inside combat: uncaught `EOFError` from `game.py:680`** | **worst impact: two tracebacks, one of them inside combat** |

**Worst impact: uncaught EOFError inside the combat loop, plus a silent misinterpretation that converts every mistyped combat command into a wasted attack turn.**

### Modification probe (B9)
Added a seventh room (`Flooded Cellar`, hung south off `garden`, and used it to place the orphaned Silver Amulet) and changed the Rusty Sword's attack 3 → 40. **Touched exactly one file, `world.yaml`: one room block, one exit line, one integer. No Python edited.** Room reachable, amulet takeable, `Attack: 41` in status. Clean probe.

**But the probe path is trapped.** A content author adding a monster the obvious way — into a room's `monsters:` list — gets it silently doubled, because `world.py` also appends every monster by its `location` field. The doubling I observed in play is precisely what this modification surface does to you.

### Workability hazards found while probing
- `game.py` is **809 lines / 30KB**, holding save, load, describe, move, dispatch, help, status, inventory, take, drop, examine, use, equip, talk and the whole combat loop in one class.
- **A stray `def __init__(self)` at module level, line 784**, outside the class, whose docstring says *"It no longer attempts to automatically load a saved game; loading must be performed explicitly"* — describing behaviour the real `__init__` at line 32 does not have. It is dead, and it is a decoy pointed at exactly the bug a maintainer would come looking for.
- A dead `flee` branch in `handle_command` gated on `self.in_combat` / `combat_flee_requested`, neither of which is ever set anywhere.
- `entities.Armor` declares `defense_bonus`; `world.py` never populates it and `world.yaml` gives the Iron Shield `attack: 2`. Armour in this game **increases your attack and does nothing to incoming damage** — verified in play (goblin hit for 3 with and without the shield). The item description was written around the bug: *"A sturdy shield that adds confidence to your strikes."*

### Attribution
- Weakness gated on the weapon slot; monster doubling; armour-adds-attack; 6 rooms; single-node dialogue; no win state; broken `main:run` console script; save-on-death — **model-innate**.
- Stray module-level `__init__` and the dead `in_combat` branch — **interaction** (repairs authored at the wrong scope and left in the tree; both describe fixes that never landed).
- Project layout — **framework-coupled, excluded per §4**.

---

## THE TEN FORCED CHOICES

### PANEL A — DELIVERY

**A1 · working surface → B.** Every one of B's 15 help-listed commands works when invoked, on top of a functioning interactive combat loop; A's own README offers five commands (`go`, `examine`, `attack`, `flee`, `talk to <npc>`) of which four exist nowhere in the tree and the fifth discards its argument, and A's entire combat system has no player-facing surface to work.

**A2 · state integrity → A.** A's load round-trips player, equipment, defeated monsters and NPC progression cleanly and only reverts room items; B's round-trip resurrects monsters you killed, has no `save`/`load`/reset commands at all, and persists death into a silently auto-loaded game at `Health: -1` from which there is no in-game escape.

**A3 · robustness → A.** On the axis's own ordering — traceback worst, then silent misinterpretation — A has one traceback (title screen) and a harmless silent misinterpretation (`talk` ignores its target), while B has two tracebacks including one reachable mid-fight and a silent misinterpretation sitting on the core combat loop that converts `use Healing Potion` into a wasted attack with no feedback.

**A4 · delivered scope → B.** B landed 6 rooms, 5 items, 15 commands, four distinct monster behaviour scripts, an interactive combat loop and a two-phase boss that verifiably escalates, against A's 4 rooms, 3 items, 12 commands, one shared monster script and a boss with no phases and no weakness.

### PANEL B — CHARACTER

**B5 · ambition → B.** B set out to build a YAML-authored world with a loader, per-monster behaviour scripts (including a `coward` script no monster even uses), an interactive per-round combat loop, a two-phase escalating boss, a weakness item with a two-NPC hint chain, per-room persistence and a dialogue-node progression mechanism; A set out to build a linear four-room walk with automatic combat and reached for none of those — its README claims a multi-phase boss and unique monster behaviours that appear nowhere in the code, which is a claim, not a reach.

**B6 · imagination (world & voice) → B.** Both worlds are stock fantasy, but B's place-writing is genuinely observed — *"A faint draft whispers from the north"* is environmental direction rather than decoration, *"Weeds choke a once-beautiful garden. A glint catches your eye among the vines"* has a real eye in it — and the obsidian-key rumour gives the world a thread two NPCs pull on, where A's elder only says go north and be careful.

**B7 · experience (felt play) → B.** B has combat you can actually play — you pick targets, monsters differ (*"The Orc Warrior holds back this turn"*), the boss roars into a second phase — whereas A's central mechanic is a wall of arithmetic that scrolls past on room entry with zero decisions in it; **I charge B at full weight for the two obstacles I only cleared with source knowledge — that the boss weakness needs `equipment["weapon"] == "boss_key"`, which a player can never learn and never satisfy, and that the unlabelled combat sub-prompt silently eats every command but `flee` — and B still wins, because A has no felt play in its combat to damage.**

**B8 · craft (UI) → B.** Input tolerance is the mechanism this axis names and B is markedly better — a synonym map (`move`/`walk`/`run`, `get`/`pick up`, `inspect`, `inv`, `stats`, `hit`/`fight`), bare-direction shortcuts, full case-insensitivity, and specific missing-argument messages (`Go where?`, `Attack what?`) — against A, which is case-sensitive on direction arguments (`You cannot go NORTH from here`), silently discards `talk`'s target, and reports a missing target as an unknown verb; B's unlabelled combat prompt and denominator-less `Health: -1` status are real craft failures but do not close that gap.

**B9 · workability → A.** Both modification probes were clean single-file data edits, so the probe splits nothing — but A's logic is properly factored (parser / combat / persistence / engine / models / world_data, 281-line engine) with no decoy code, while B is an 809-line god-object carrying a **dead module-level `__init__` at line 784 whose docstring describes the exact autoload behaviour a maintainer would come to fix**, a dead `in_combat` flee branch nothing sets, a declared-and-never-populated `defense_bonus`, and a duplication trap laid directly on the content-editing path.

**B10 · documentation → B.** Accuracy against play decides this outright: A's README misstates the movement verb a player types first (`go` does not exist) plus four more commands, and claims unique monster behaviours, a multi-phase boss, a branching narrative and a `requirements.txt` — none of which are in the tree — while B's README lists 15 commands that all work and a multi-phase boss that verifiably exists, and is wrong on three counts (8 rooms vs 6, branching dialogue, a `main:run` console script that does not exist).

---

## PANEL TALLIES

> ### **Delivery: A 2 · B 2 (EVEN)**  ·  **Character: B 5 · A 1**

## OVERALL FORCED CHOICE: **B**

**NO PANEL SPLIT.** The panels do not disagree on direction — Panel A is dead even, so there is no delivery direction for the character panel to contradict. Delivery split the way it did for a real reason worth recording: B delivers a larger and more functional surface (A1, A4) while A is the tidier machine at the edges (A2, A3), and neither artifact delivers the thing the brief is actually about — **both are UNWINNABLE, and neither has a win state anywhere in its tree.** Character is not close: B reached materially further, wrote the better world, is the only one of the two that contains playable combat, and tells fewer lies about itself; A takes B9 alone, on organisation and on B's decoy code.

**SELF-FLAG: not CLOSE overall.** Panel B is B's at 5–1, and even under the most generous re-reading of the two axes I argued hardest (B7, B8) it cannot fall below even, so the overall does not turn. **The Delivery panel, however, is one axis from flipping in either direction** — A2 and A3 are the two calls a second judge could plausibly reverse, which would make Delivery 3–1 or 1–3 without changing the overall.

**The decisive defect in each, for the record.** A: no win condition exists anywhere in the tree, and the boss cannot be killed even in principle (150 HP against a maximum deliverable 105). B: a textbook cross-module seam bug — the boss weakness fires only on `equipment["weapon"] == "boss_key"`, and `boss_key` is `type: key`, which `equip_item` refuses, so the one item two NPCs and its own description tell you will beat the dragon can never enter the slot the check reads. **A seam bug is what stopped me in B. It is not what stopped me in A — in A there was nothing to stop.**
