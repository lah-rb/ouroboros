# FLOOR RECALIBRATION FLIGHT — devstral-2 floor rerun vs the SITTING Guardian

*Dispatched 2026-08-20 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
dev/blind_panel/anchors/v2.0/guardian-gptoss-20260803/alpha (SITTING
GUARDIAN — gpt-oss-120b-a5); B = tier_20260820-075618/staged/arm01/alpha
(CANDIDATE — devstral-2-small-24b, 2h wall, 10/31 goals, first devstral
run under the collapsed-diagnose framework + write-tests-to-fail
strategy). Candidate at B (rotation honored — previous flight had the
candidate at A). Scans clean after d8152ae (the staging block was a
false positive: the stem 'small' from devstral-2-SMALL-24b matched
ordinary item prose).

VERDICT: Delivery A 4-0 · Character A 5-1 · OVERALL GUARDIAN. No split,
no CLOSE flag. Devstral's one axis is B6 imagination ("Gothic Castle
Adventure", the moonflower garden, the inscribed locket).

WHY THIS FLIGHT EXISTS: the guardian-candidate promotion checklist
(PROVENANCE.md, guardian-candidate-gptoss-20260819) requires a floor
recalibration before the anchors move. Answer: the floor is still the
floor, decisively — devstral on the CURRENT framework loses 9-1 to the
2026-08-03 anchor the gpt-oss rerun just beat 8-2. The ladder is
monotone: floor << sitting guardian < guardian candidate.

RUN-SIDE CONTEXT the judge could not see (and which the blind record
independently corroborates): the artifact is unenterable because
devstral's diagnosis named the fix correctly ~24 consecutive times
("Add 'from models import Player'") while naming the target symbol
BARE (initialize_game); the qualified symbol table read that as
symbol-missing, routed every repair to add_symbol, and appended the
orphan module-level initialize_game the judge found at the bottom of
engine.py. Six boss consults fired; the final several DELIVERED
(provider fixed mid-run, 9ea48a1) and named the same one-line import —
the model could not cash advice it had already given itself. The judge
also found the SAME orphan-repair pattern in the GUARDIAN (a module-
level __init__ never attached to Game) — the add_symbol crack is not
devstral-specific; devstral is just the first model that could not
recover from it.

The rule-9 inverted test the acceptance gate vetoed in-run is the same
one the judge flagged: "its shipped test does not merely miss the first
one, it asserts it as the expected result and monkey-patches around it."

Judge's verbatim record follows.*

---
# FLIGHT RECORD — TIER_RUBRIC v2.1
**Packet:** `flight_20260820_floor_vs_guardian` · one judge, blind, both artifacts played · ten forced choices, two panels, tallied separately

---

# ARTIFACT A — RECORD

## Premise line *(not ranked)*
A nameless six-room stone dungeon that runs from an entrance hall up a corridor to a throne room, where a Shadow Dragon sits on top of the very key that is supposed to weaken it. It is a plain fetch-equip-fight loop with round-by-round combat, three monsters with visibly different tempers, and two old men who each say one sentence and never say anything else.

> *"Weeds choke a once-beautiful garden. A glint catches your eye among the vines."*

The title screen is a hand-drawn ASCII banner that spells no readable word (it renders roughly as `Setowte`), followed by `Welcome to the Adventure!` — the game has no name of its own.

## Entry-point ledger
| documented way in | result |
|---|---|
| `python main.py` (README) | **Works**, from any working directory (`Path(__file__).with_name("world.yaml")`) |
| `text-adventure` console script (README + pyproject) | **Broken as declared** — `pyproject.toml` says `text-adventure = "main:run"`; `main.py` defines `main()`, not `run()` |
| `pip install .[dev]` → `ruff` / `black` | dev extras declared; README also links a `LICENSE` file that is not in the tree |

## Completability class: **UNWINNABLE**
Furthest point reached: **Throne Room, Obsidian Key in hand, engaged the Shadow Dragon, dead in three rounds.**

```
> You engage the Shadow Dragon!
> You strike for 6 damage. Shadow Dragon health is now 44.
The Shadow Dragon slashes you for 8 damage. Your health is now 12.
...
You have fallen in battle. Game over.
```

Two independent reasons it cannot be won, both confirmed in play before any source was read:

1. **The arithmetic is closed.** Player 20 HP, base attack 1; the only weapon is +3 and the only "armour" is +2 *attack*, ceiling 6. Dragon is 50 HP / 8 damage. Nine rounds needed, three survived. The one potion cannot be drunk in combat (below), and fleeing to heal loses more HP than it deals damage.
2. **Nothing in the tree can ever declare a win.** `grep -iE "win|congratul|victor|triumph"` over `game.py`, `world.py`, `entities.py` returns nothing. Killing the boss prints `The Shadow Dragon collapses!` and returns you to the prompt.

## Seam bug — yes, and it is the canonical one
**The boss weakness is gated on an equipment slot its own item type can never occupy.**

```python
# game.py, boss branch
if self.equipment["weapon"] == "boss_key":
    extra = 15
```
`world.yaml` declares `boss_key` as `type: key`. In play:
```
> equip Obsidian Key
That item can't be equipped.
> use Obsidian Key
You can't use that right now.
```
The +15 weakness damage is unreachable by construction. This is the exact failure shape the flight prompt names. **Attribution: model-innate.**

**Second seam — double placement.** `world.yaml` lists monsters under each room *and* gives every monster a `location:` field; `world.py` then appends the location-derived id on top of the room list:
```python
for monster in monsters.values():
    if monster.location in rooms:
        rooms[monster.location].monsters.append(monster.id)
```
Every monster is therefore listed twice, and killing one leaves a zombie entry behind:
```
Danger! Monsters present: Goblin Guard, Goblin Guard
> attack Goblin Guard  →  The Goblin Guard collapses!
> look
Danger! Monsters present: Goblin Guard          ← still "Danger!", room is empty
> attack Goblin Guard
You engage the Goblin Guard!                    ← then nothing; loop exits at once
```

## Room graph and placement audit
- **6 authored rooms; 6 of 6 reachable** from `start` — one component, no disconnected wing. But the brief asks eight.
- Graph: `start →N corridor`; `corridor →E armory, →W garden, →N library`; `library →N throne`.
- **UNPLACED ENTITY: `amulet` / "Silver Amulet"** — authored in `items:` and present in **no room's item list**. One of A's five items exists only in the registry; only four are in the playable world. I confirmed it by placing it myself during the modification probe, after which it appeared and could be taken.
- No unplaced monsters or NPCs (they are over-placed, not unplaced).

## Conformance tally — **41 / 47**
**Unmet: 22, 23, 31, 33, 38, 45.**

- **22** healing usable mid-combat — *absent*. The combat sub-loop reads its own input and every string except `flee` becomes an attack; there is no `use` branch in it.
- **23** three regular monsters — only **two** (`goblin`, `orc`); `dragon` is the boss.
- **31** win path on defeating the boss — *absent from the tree*.
- **33** defeat screen offers a restart — `print(...); self.write_save(); exit(0)`.
- **38** eight connected rooms — six.
- **45** NPC dialogue branches — each NPC has exactly one node (`start:`) and `triggers: {}`. The `npc_progress` / `next_node` machinery exists in `game.py`; no data drives it. Below even the presence-lenient "staged progression" floor.

**Binary verdict: SIGNIFICANTLY-DEVIATED.** Trigger is **core-loop, not count** (6 unmet is under the 10 threshold): item 31, the win link, exists nowhere in code or data.

## State integrity
Round-trips the *player* correctly and **rewrites the world**.

Saved and restored correctly: health, attack, location, inventory, equipment, per-room item lists, `defeated_monsters`, `npc_progress`. Re-equipping after load does not double-apply bonuses.

Three defects:
1. **Defeated monsters resurrect at full health.** `defeated_monsters: ["goblin"]` is written and read back, but `load_state` never uses it to clear room monster lists. A room I had cleared came back with both duplicate entries alive.
2. **There is no `save` and no `load` command** (`> save` → *"I don't understand that command."*). It autosaves on `quit`, on Ctrl-D, and on death; it autoloads on start, unprompted. There is no "new game".
3. **Death autosaves the corpse.** After dying I relaunched and got:
```
Press Enter to begin...Loaded saved game.
Throne Room ...
> status
Health: -4
```
The player is silently resurrected at −4 HP in the boss room, with no in-game route back to a fresh start — you must know to delete `savegame.json`. **Attribution: model-innate.**

The intended repair for this is *in the file and never wired* — a module-level orphan `def __init__(self)` sits at the bottom of `game.py`, docstring: *"It no longer attempts to automatically load a saved game."* It is at module scope, never attached to `Game`, and its body uses an API (`self._current_room_key`, `self._create_world()`) the class does not have. **Attribution: interaction.**

## Robustness battery
| # | probe | result | class |
|---|---|---|---|
| 1 | `xyzzy` | `I don't understand that command.` | clean refusal |
| 2 | empty line | ignored, reprompt | clean |
| 3 | `go up` | `You can't go that way.` | clean |
| 4 | bare `go` / `examine` / `use` | `Go where?` / `Examine what?` / `Use what?` | clean |
| 5 | `take unicorn` | `No such item here.` | clean |
| 6 | `attack ghost` | `No such monster here.` | clean |
| 7 | `equip Obsidian Key` | `That item can't be equipped.` | clean |
| 8 | `GO NORTH` uppercase | works | clean |
| 9 | `go north east` | moves north, extra token discarded | silent misinterpretation (minor) |
| 10 | **EOF / Ctrl-D** | top level: `Exiting game.` + autosave (clean). **Inside combat: `EOFError` traceback** | **traceback** |
| +1 | non-attack input in combat (`help`, `quit`, `use Healing Potion`) | **silently consumed as an attack turn** | **silent misinterpretation** |
| +2 | `take sword` vs `equip sword` | `take` refuses the short form, `equip` accepts it | inconsistent tolerance |

**Worst impact:** uncaught `EOFError` in the combat loop. **Runner-up, and the one that shapes play:** every non-`flee` input in combat becomes an attack, so you cannot quit, check status, or drink a potion during a fight.

## Modification probe (B9) — **clean**
Two changes, both **pure `world.yaml` edits, zero Python touched**:
1. Ninth room `crypt` ("Sunken Crypt") added, linked `garden →S crypt / crypt →N garden`, given the orphan `amulet`. Reached it, looked, took the Silver Amulet.
2. `rusted sword` `attack: 3 → 9`.

Nothing broke. `> equip Rusty Sword` → `Attack: 10`. The loader defaults missing keys, so a partial room definition is tolerated. This is the strongest single fact in A's favour on B9.

## Other charged findings
- **Armour is not armour.** `Iron Shield` has `stats: {attack: 2}`. Controlled test, same monster, with and without the shield equipped: `The Goblin Guard attacks you for 3 damage.` both times. `entities.py` declares `Armor.defense_bonus` and nothing ever sets or reads it; combat has no defence term at all. *Model-innate.*
- Behaviours are real and legible: `The Orc Warrior holds back this turn.` vs `The Goblin Guard attacks you for 3 damage.` A fourth branch (`coward`) is authored and used by no monster.
- Healing works and caps correctly (14 → `recover 6 health` → 20).
- The weakness item is placed **in the boss's own room**, so the librarian's hint is spent before you can act on it.
- `flee` outside combat: `There's nothing to flee from right now.` — but the branch reads `self.in_combat` / `combat_flee_requested`, vestigial state the real combat loop never sets.

---

# ARTIFACT B — RECORD

## Premise line *(not ranked)*
The Gothic Castle Adventure: eight rooms of a ruined castle — entrance hall, library, moonlit courtyard, throne room, hidden armory, dungeon, a secret garden of moonflowers — arranged as two branches that converge on the Dark Sovereign's chamber, with a silver locket hidden in the garden and two NPCs who tell you where to find it. It is the better-imagined world of the pair and, as shipped, no one can enter it.

> *"A hidden oasis of greenery in the heart of the castle. Moonflowers glow faintly in the darkness, casting an ethereal light."*

## Entry-point ledger
| documented way in | result |
|---|---|
| `python main.py` from inside the artifact dir | Title screen appears; **`new` raises `NameError` and kills the process** |
| `python main.py` from any other directory | **`FileNotFoundError: 'world.yaml'`** before the title screen — `world.py` does `open("world.yaml")` against the CWD |
| `continue` at the title | `Please type 'new'...` (no save exists); with a save, `Game continuation not yet implemented` |
| `pip install -e .` → `text-adventure` (`main:main`) | symbol is correct; same runtime failure |

## Completability class: **UNWINNABLE**
Furthest point reached **as shipped: the title prompt.** The first and only command the title screen offers is fatal:

```
Welcome to the Gothic Castle Adventure!
Type 'new' to start a new game.
> new
Traceback (most recent call last):
  ...
  File ".../engine.py", line 16, in initialize_game
    player = Player(health=30, attack=5, defense=2)
NameError: name 'Player' is not defined. Did you mean: 'player'?
```

`engine.py` line 3 reads `from models import Direction, Item, NPC`. `Player` is defined in `models.py` and is not on that import line. **This is a one-token seam bug that makes the entire artifact unenterable.** *Model-innate.*

**A shipped test asserts this bug as correct behaviour.** `tests/test_game_starts_with_a_title_screen_and_allows_the_p.py`:
```python
try:
    # This should raise NameError because Player is not imported yet
    engine.initialize_game()
    assert False, "Expected NameError but game initialized successfully"
except NameError as e:
    assert 'Player' in str(e), ...
# After the fix (importing Player), this should work
# We'll simulate the fix by adding the import dynamically
sys.modules['engine'].Player = Player
```
The defect was diagnosed precisely, encoded as the *expected* result of a green test, monkey-patched around in-memory, and the one-line import was never applied. **Attribution: interaction** — and per CHARGE WHAT SHIPS this is in comparison, as a handicap.

### Judge-side diagnostic (disclosed; does not change the shipped record)
To characterise delivered scope behind the wall I applied two minimal edits **in my scratch copy only**, and the shipped state above remains the record's truth:
1. added `Player` to the `models` import;
2. `initialize_game` looks up `self.world_data["rooms"].get("start")` — **there is no room with id `start`**; every room in `world.yaml` is `entrance_hall`, `library`, … The code comment says `# Start in the first room (assuming 'start' exists)`. Without edit 1 you never see it; with edit 1 you get `ValueError: Start room not found in world data`. **A second, fully independent fatal seam** of the same identifier-mismatch family. I repointed it to `entrance_hall`.

Even after both repairs the game **still has no win**: the boss dies to one keystroke and play simply continues.
```
> attack The Dark Sovereign
You defeated the The Dark Sovereign!
> stats
Health: 3, Attack: 5, Defense: 2
> look
A massive chamber ... At its center stands a towering figure cloaked in shadow ...
```
No victory screen, no terminal state, and the room still describes the boss standing there.

## Seam bugs — yes, six of them, and one stopped me cold
1. `Player` not imported → `new` is fatal. **Decisive.**
2. `"start"` vs `entrance_hall` → fatal even once (1) is fixed.
3. `save_game` / `load_game` called in `engine.py`, never imported → `> save` and `> load` both traceback and kill the process.
4. **Parser emits `target` where the handler reads `direction`.** `go`→`move` sets `target="north"` and leaves `direction=None`; the move handler reads `command.direction`. So `go north` — the exact form in the brief and the README — fails, and only bare `north` works.
   ```
   > go south
   Please specify a direction to move.
   > south
   You move south. You stand in a vast hall ...
   ```
5. **`talk to X` keeps the `to`.** `split(maxsplit=1)` gives `target="to elder librarian"`, compared against `npc.name`.
   ```
   > talk to Disgruntled Knight
   There is no to disgruntled knight here to talk to.
   > talk Disgruntled Knight
   This castle is cursed! The Dark Sovereign has claimed it for his own.
   ```
6. **The boss weakness compares the display name against the id.** `combat.py`: `if item.id == "silver_locket" and self.monster.name.lower() == "boss":` — the monster's `name` is `The Dark Sovereign`; `boss` is its *id*. The locket is inert forever. Carried it into the fight; nothing happened.

## Room graph and placement audit
- **8 authored rooms; 8 of 8 reachable** from `entrance_hall` — one component, correctly built, meets the brief. `entrance_hall →N library →W throne_room →N secret_garden` and `entrance_hall →E courtyard →N armory →E dungeon →N final_chamber`.
- **No unplaced items, monsters or NPCs** — all 5 items, all 5 monsters and both NPCs appear in a room's list. B's data placement is clean; A's is not.
- **But 0 of 8 are reachable in the shipped artifact**, and the *starting* room the engine asks for is an id that has no data block anywhere — the placement defect is on the engine side of the seam rather than the data side.

## Conformance tally — **33 / 47**
**Unmet: 5, 11, 13, 14, 16, 17, 22, 26, 28, 31, 32, 33, 37, 47.**

- **5** `drop` — parser maps it; `handle_command` has no branch; nothing in the tree drops an item.
- **11** `flee` — the string appears nowhere in any `.py` file.
- **13/16/17** `status` and `help` — no handler for either. `stats` exists and shows health/attack/defense only: no equipment, no location, and it never reflects an equipped weapon (`Attack: 5` while wielding a +8 broadsword).
- **22** healing mid-combat — `CombatEngine.fight()` is a closed `while True:` that takes no input.
- **26** distinct behaviours — all five monsters carry `behavior: aggressive`, and `combat.py` never reads the field. Literally one shared script.
- **28** boss two phases — absent from code *and* data.
- **31** win path — nothing distinguishes the boss's death from any other monster's.
- **32/33** defeat screen + restart — see below.
- **37** state loads from JSON — `load_game()` ends `# For now, we'll return None to indicate the function is not yet fully implemented` and returns `None`. Nothing anywhere reconstructs a `GameState`.
- **47** combat narrated — one result string for an entire fight; no rounds, no blows, no health readout.

**Binary verdict: SIGNIFICANTLY-DEVIATED.** *Both* triggers fire: 14 unmet ≥ 10 **and** multiple core-loop elements absent from the tree (win link, `flee`, boss phases, state loading).

## State integrity
**None.** There is no working persistence in either direction.
```
> save
Traceback ... NameError: name 'save_game' is not defined
> load
Traceback ... NameError: name 'load_game' is not defined
```
`saver.py` contains a complete, correct `save_game` serialiser that nothing can reach, and a `load_game` that is a declared stub. `main.py`'s `continue` branch: `# In a real implementation, we would restore the saved game state here` → `print("Game continuation not yet implemented")`. `GameState.set_dialogue_progress` exists and is never called, so the second dialogue stage authored for both NPCs is dead — I got `first_encounter` twice in a row from the same librarian.

## Robustness battery
*As shipped, probe 1 ends the program; the table below is the post-diagnostic reading, disclosed as such.*

| # | probe | result | class |
|---|---|---|---|
| 1 | **`new` (shipped)** | **`NameError` traceback, process dies** | **traceback — fatal** |
| 2 | `xyzzy` | `I don't understand that command.` | clean |
| 3 | empty line | ignored, reprompt | clean |
| 4 | `go up` | `Please specify a direction to move.` (a direction *was* given) | misleading refusal |
| 5 | `take unicorn` | `There is no unicorn here.` | clean |
| 6 | `talk nobody` | `There is no nobody here to talk to.` | clean |
| 7 | **`attack ghost`** | **`You defeated the Skeleton Guard!`** — target ignored, a real monster killed | **silent misinterpretation** |
| 8 | `equip banana` | `You don't have banana.` | clean |
| 9 | `help` (advertised by the program itself) | `I don't understand that command.` | false advertising |
| 10 | `save` / `load` / **EOF** | `NameError` / `NameError` / `EOFError` — all three kill the process | **traceback ×3** |
| +1 | **dying** | `You have been defeated.` then play continues at `Health: -6`; I walked around dead and fought again to −15 | **silent misinterpretation** |
| +2 | **losing a fight deletes the monster** | `if "defeated" in result:` matches *both* `You defeated the X!` and `You have been defeated.` — after the boss killed me, the boss was gone: `There's nothing to attack here.` | **silent misinterpretation** |

**Worst impact:** uncaught `NameError` on the one command the title screen tells you to type. Post-repair, the worst is the substring test that erases a monster from the world when the *player* loses to it.

## Modification probe (B9) — **crashed twice before it began, once during**
Before I could probe at all I had to repair two shipped defects (`Player` import, `"start"`). Then:

1. **Ninth room.** Added `crypt` with `id / title / description / connections` and linked `armory →W crypt`. Result — the whole program died *before the title screen*, inside `GameEngine.__init__`:
   ```
   File ".../world.py", line 52, in create_world
       items=room_info["items"],
   KeyError: 'items'
   ```
   `world.py` indexes `room_info["items"]`, `["monsters"]`, `["npcs"]` with no defaults. I had to hand-write `items: []`, `monsters: []`, `npcs: []`. It then worked (`You move west. A flooded crypt beneath the armory.`).
   Second trap: I first linked the room as `south_west`, which the parser has no concept of, so the room was authored, loaded and permanently unreachable with no warning.
2. **Weapon damage** `rusted_broadsword` `attack: 8 → 20` — worked, but is invisible to the player: `stats` still reports `Attack: 5`.

Module boundaries themselves are good (`models` / `parser` / `world` / `game_state` / `combat` / `saver` / `engine` / `main`, ~814 lines, each with one job) — but the identifiers crossing four of those boundaries are wrong, the data loader will not tolerate its own format, and `world.py`'s CWD-relative `open("world.yaml")` means the most reusable-looking module cannot be imported from anywhere else.

`engine.py` also ends with the same orphan-repair pattern as A — a module-level `def initialize_game(self)` that sets `self.player` (an attribute the class never uses) and references the still-un-imported `Player`.

---

# THE TEN FORCED CHOICES

## PANEL A — DELIVERY

| axis | choice | justification |
|---|---|---|
| **A1 working surface** | **A** | Every one of A's fourteen advertised commands responds correctly when invoked; B has no `help`, no `status`, no `drop`, no `flee`, `save`/`load` traceback, and as shipped nothing at all works because `new` raises `NameError`. |
| **A2 state integrity** | **A** | A's save round-trips player, location, inventory, equipment and per-room items (it does resurrect defeated monsters and autoloads a −4 HP corpse); B has *no* working persistence — `save` and `load` both crash the process and `load_game()` returns `None` by design. |
| **A3 robustness** | **A** | A's worst outcomes are one `EOFError` in combat and non-attack input becoming an attack; B tracebacks on start, on `save`, on `load` and on EOF, kills a real monster when you attack a nonexistent one, and lets you keep playing at −15 health. |
| **A4 delivered scope** | **A** | A delivers a navigable world with working inventory, equipment, dialogue, interactive combat and persistence (41/47); B delivers a title prompt (33/47). |

**Panel A tally: A 4 – 0**

## PANEL B — CHARACTER

| axis | choice | justification |
|---|---|---|
| **B5 ambition** | **A** | Judged on what each set out to build: A attempted a two-phase boss escalation, a weakness item dealing bonus damage, four distinct per-monster combat scripts, interactive round-by-round combat with mid-fight flee, and world-item state in the save — B attempted *none* of those four (no phases in code or data, one identical `behavior` string on all five monsters that nothing reads, a closed auto-resolve `while True` with no player input, no `flee` anywhere); B's greater reach is in scale and module structure, which is real but is two more rooms and a second dialogue line against the harder machinery. |
| **B6 imagination** | **B** | B has a name, a place and a voice — "Gothic Castle Adventure", The Dark Sovereign, a moonlit secret garden of glowing moonflowers, a locket inscribed *"Light shall banish the darkness"*, and a Disgruntled Knight who says *"Only the brave (or foolish) dare challenge him"* — against A's unnamed game, nonsense ASCII banner, and rooms called Entrance Hall, Long Corridor, Armory, Library, Throne Room. |
| **B7 experience (felt play)** | **A** | A's `look` shows name, description, NPCs, items, monsters *and* exits, and combat gives you a per-round blow-by-blow with behaviour flavour; B's `look` prints only the description — never exits, items, monsters or NPCs — so **I found the broadsword, the room graph and the exit directions only by reading `world.yaml`, and learned that `go north` and `talk to X` are broken only by reading `parser.py`, none of which a player can do** — and as shipped a player's entire experience is one traceback. A is charged here too: I needed the source to learn that `take` demands `rusty sword` while `equip` accepts `sword`, and A's combat offers exactly one real choice per round. |
| **B8 craft (surface)** | **A** | A's help lists fourteen genuinely-real commands, `status` gives health/attack/location/weapon/armor, and every failure has a specific message (`Go where?`, `That item can't be equipped.`); B's own banner says *"Type 'help' for a list of commands"* and `help` answers `I don't understand that command.`, `status` does not exist, `stats` omits equipment and location and ignores the weapon you are holding, the title screen prints twice, and the boss dies as *"the The Dark Sovereign"*. |
| **B9 workability** | **A** | The probe decides it: A's ninth room and weapon change were pure `world.yaml` edits with zero Python touched and nothing broken, while B needed two shipped defects repaired before it would run at all and then died with an unguarded `KeyError: 'items'` on its own first data edit — B's module boundaries are where I would put them, but four of them carry mismatched identifiers, its loader will not accept its own format, and its CWD-relative `open("world.yaml")` makes the cleanest-looking module the least portable. |
| **B10 documentation** | **A** | Both READMEs are contradicted by play (A: "8 rooms" for 6, "branching dialogue" for one fixed line, a `main:run` console script that does not exist; B: "Use `help`", "Save and load functionality"), but A's Controls block matches the shipped help verbatim and gets a stranger to a running game and a correct vocabulary, whereas B's README never mentions `new` — the only command that starts it — and so cannot get a stranger past the prompt. |

**Panel B tally: A 5 – 1**

---

# OVERALL

**Delivery: A 4 – 0 · Character: A 5 – 1**

**OVERALL: A**

**No panel split** — both panels point the same way. **No CLOSE flag**: the margin is not within noise on either panel. B wins exactly one axis, imagination, and wins it clearly; that is the whole of its case.

The two artifacts fail in the same family and at different depths. Both are killed by cross-module identifier seams, and both ship a dead orphan function at module scope that reads as a repair written and never attached — A's `__init__` that would have stopped the auto-load, B's `initialize_game` that would have created the Player. A's seams cost it the win condition and the weakness item but leave a world you can walk through, fight in and save; B's seams cost it the *program*, three times over — `Player`, `"start"`, and `save_game`/`load_game` — and its shipped test does not merely miss the first one, it asserts it as the expected result and monkey-patches around it rather than adding the import. B authored the better castle: eight rooms correctly connected, every entity placed, the only real prose in the packet, and the only game in the packet with a name. Nobody can get into it.
