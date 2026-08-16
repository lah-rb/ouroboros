# FLIGHT — structural session (new tooling) vs structural batch, muse-glimmer-30b

*Dispatched 2026-08-15 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
tier_20260814-175357/staged/arm01 (structural BATCH arm, 08-14); B =
tier_20260815-115356/staged/arm01 (structural SESSION arm, 08-15, with the
fence shadow fix, session fallback_path, resident_strip_reasoning, and the
n_ctx/4 window reserve). Same model both sides — the flight isolates
structural_mode + tooling. Candidate sat on B.*

*Dispatcher notes: packet A was normalized by hand (4 self-naming sites; the
archived staged original untouched); make_judge_packet.py lacks
--arm-identifier and passed A carrying "Muse Glimmer 30b" — same blind spot
stage.py had before 08-14, fix owed. Judge's verbatim record follows.*

---

# FLIGHT RECORD — `flight_20260815_session_vs_batch`

Rubric: TIER_RUBRIC v2.1 · Checklist: CHALLENGE v2 (47 items) · Interpreter: `/Users/lah-rb/Repos/ouroboros/.venv/bin/python` · Play in `<packet>/scratch/{A,B}/`, both artifacts copied out of their trees before play.

---

## ARTIFACT A — record

### PREMISE LINE
A castle crawl in eight one-line rooms: you walk north out of an entrance hall, are ambushed by whatever monster guards each room you step into, loot a sword, armour and an amulet, and end at a dragon on a throne of bones. It is the genre's default shape rendered at minimum resolution — the fittings are all present and none of them are dressed.

> *"A massive chamber with a throne of bones."*

### ENTRY-POINT LEDGER
| documented way | result |
|---|---|
| `python main.py` (README) | works — title screen, then first room |
| `text-adventure` console script (README + pyproject) | would fail: `load_world()` opens `world_data.yaml` by bare relative path; any cwd but the tree gives `FileNotFoundError`. Verified by running `python A/main.py` from the parent dir → traceback. *(model-innate; B has the identical fault — a wash)* |
| `pip install -r requirements.txt` (README) | **fails — A ships no `requirements.txt`.** The fallback `pip install pyyaml` on the next line works. *(model-innate)* |

### COMPLETABILITY: **UNWINNABLE**
The win screen is live code — `engine.py:41` prints `*** YOU WIN! The dragon falls and peace returns. ***` when `game_won` is set — and **nothing a player can do reaches it.** This is a balance defect, not a seam, and I established it by exhaustive play, then confirmed the arithmetic.

Unavoidable damage before the boss: Goblin costs 8 HP (fought unarmed — the Armory is *behind* the Goblin's room), Troll 5, Orc 1–2. Max HP is 20 and the single `Healing Potion` heals 10, capped at max. The best possible schedule — heal in the Dark Forest at 7 HP, the only point where the full 10 lands — arrives at the Dragon on **16 HP**:

```
> HP: 17/20 | Location: Dark Forest      (potion spent optimally)
...
Turn 1 | You HP: 16/20 | Dragon HP: 40
Fire Amulet weakens the boss!  You hit Dragon for 10 damage.  Dragon hits you for 4 damage.
Turn 4 | You HP: 4/20 | Dragon HP: 15
You hit Dragon for 5 damage.  Dragon hits you for 4 damage.
You have been defeated...
```
The Dragon has 40 HP; the amulet doubles damage on **odd turns only** (`combat.py:60`, `turn % 2 == 1`), giving 10/5/10/5/10 — five player turns, so four Dragon hits at 4 = 16 damage. **You need 17 HP and can bring at most 16.** Six independent attempts, all `*** GAME OVER ***`. Furthest point reached: the Dragon Throne, boss at 15/80 HP, dead on turn 4. **No source reading was required to get there** — I reached the boss purely by play.

### CONFORMANCE: **45 / 47 — NEAR-FULL** (2 unmet; trigger not reached)
Unmet: **#16** (`help` lists `save`, which the parser has no rule for and the engine has no handler for — typing it does nothing, silently), **#45** (NPC dialogue does not branch anywhere in the tree: `options: []` for both NPCs, and `engine.py:212` always fetches the `start` node).

Presence-rule notes: #28 met — the second phase exists as a printed transition, mechanically inert. #37 met — `save.py:load_game` is authored and is **never called by anything**.

### ROOM GRAPH & PLACEMENT
**8 of 8 authored rooms reachable from `entrance`**, all visited in play (Entrance Hall → Great Hall → {Armory, Library, Kitchen} → Dark Forest → Cave → Dragon Throne). **No disconnected component. No unplaced entity**: all 5 items, both NPCs and all 4 monsters carry a room. Clean on the campaign's most common decisive defect.

### STATE INTEGRITY — **write-only; does not round-trip**
`save_game` fires once, at `engine.py:51`, after the loop ends. There is **no load path in play at all**: no `load` verb in the parser, no `load` branch in the engine, and `load_game` is dead. A second launch always starts fresh:
```
=== save.json ===  {"location_id": "hall", "player": {"health": 12, ...}, "defeated_monsters": ["goblin"], ...}
=== second launch ===  > HP: 20/20 | Location: Entrance Hall   Inventory: Empty
```
Two further faults: the save omits `room_item_ids`, so world item positions are outside the record even if it were read back; and every exit silently overwrites `save.json`, destroying the prior one. *(model-innate)*

### ROBUSTNESS TABLE
| probe | behaviour | class |
|---|---|---|
| `xyzzy` | **nothing printed at all** | silent |
| empty line / whitespace | nothing printed | silent |
| `go up` | `Can't go that way.` | clean refusal |
| `go` / `take` bare | nothing printed (parser returns `unknown`, engine has no `unknown` branch) | silent |
| `take Nonexistent` | `Item not here.` | clean |
| `equip`/`use`/`drop` absent item | `You don't have that.` | clean |
| `talk to Bob` | `No one here by that name.` | clean |
| `attack`/`flee` outside combat | nothing printed | silent |
| `save` (**advertised in `help`**) | nothing printed | silent |
| **any unrecognised input inside combat** | **executed as an attack** — `quit`, `look`, `help` all swing the sword and cost you HP | **silent misinterpretation** |
| Ctrl-D / EOF | `EOFError` **traceback**, at three separate call sites (main prompt `engine.py:38`, combat prompt `combat.py:32`, game-over prompt `engine.py:46`) | traceback |

**Worst impact: silent misinterpretation.** `parse_command` emits `attack` and `flee` actions that `_handle_command` has no branch for, and `unknown` for everything else — also unhandled. A player gets *zero* feedback for any mistake, and inside a fight a typo costs health.

### DECISIVE SEAM BUG — flee ends your run
`combat.py:36` returns `False` for a **successful flee**; `combat.py:86` returns `False` for **player death**. `engine.py:107-108` collapses both into `self.state.game_over = True`:
```
Turn 1 | You HP: 12/20 | Troll HP: 20
Combat action [attack/use/flee]: You flee successfully!

*** GAME OVER *** Type 'restart' to try again? (quit to exit)
```
One bool carrying two outcomes across a module boundary. Compounding it, flee only succeeds against `behavior: cautious` — the Troll is the **only** cautious monster, so the single monster you *can* escape is the one that kills you for escaping. *(model-innate)*

### MODIFICATION PROBE (B9) — **clean pass**
Added a ninth room (`Sunken Crypt`) as a nine-line block in `world_data.yaml` plus one `east: crypt` exit on `cave`. **Zero code files touched. Nothing broke** — walked into it in play:
```
=== Sunken Crypt ===  Waterlogged coffins line the walls.  Exits: west
```
Weapon damage change (`damage: 3` → `30` on the Rusty Sword) also data-only, no code touched.

### OTHER FINDINGS
- **Restart works.** Death → `*** GAME OVER *** Type 'restart' to try again?` → `HP: 20/20 | Location: Entrance Hall`, world reset (`Torch` back in the room). *(model-innate, credit)*
- `quit` sets `game_over`, so quitting presents you with the **death screen** and asks if you want to restart.
- The full room block re-prints after **every** command, including `status`, `take` and `talk`; `look` therefore prints the room twice.
- Combat is variance-free: the Goblin fight is five identical turns of `You hit Goblin for 2 damage. / Goblin hits you for 2 damage.`
- Monster behaviours *are* distinct and do show in play — `Orc defends and takes no action.` (defensive, even turns), Troll damage halving under 30% (cautious). *(model-innate, credit)*
- Both NPCs repeat one fixed line forever: `Elder: The dragon fears fire. Find the Fire Amulet in the library.`

---

## ARTIFACT B — record

### PREMISE LINE
A drowned lighthouse: you arrive at a salt-blasted courtyard beneath the Sable Spire and climb through lantern gallery, keeper's quarters and storm basement toward a sealed beacon where the Drowned Warden stands in a lightless beam. Monsters are visible with their HP on the room card and you choose when to engage them, and the boss cannot be touched by steel at all until you carry the lens that focuses the sun.

> *"Rows of extinguished lanterns line the walls. A faded painting hangs slightly askew, and a faint echo drifts from the shadows."*

### ENTRY-POINT LEDGER
| documented way | result |
|---|---|
| `python main.py` (README) | works |
| `pip install -r requirements.txt` (README) | **file ships** (`PyYAML>=6.0`) |
| `text-adventure` console script (pyproject) | same bare-relative-path fault as A |

### COMPLETABILITY: **WON**
```
=== Sealed Lighthouse Beacon ===
The great lantern room, sealed shut. The Drowned Warden waits in the lightless beam, water pooling at its feet.
A hostile Drowned Warden is here! HP 80/80

> You hit Drowned Warden for 30 damage. HP 50/80
> You hit Drowned Warden for 30 damage. HP 20/80
> You hit Drowned Warden for 30 damage. HP 0/80
You defeated Drowned Warden!
The lighthouse is freed. You win!
```
Reached the win on a normal play line (cutlass → coat → lens → vial → boss). I did **not** need source knowledge for the win path itself; I needed it for the verb list (below).

### CONFORMANCE: **41 / 47 — NEAR-FULL** (6 unmet; count trigger is ≥10, core loop intact)
Unmet: **#11 `flee`**, **#13 `status`**, **#14 `help`**, **#16** (no help output exists), **#17** (no status), **#33** (no restart — `grep` for restart across the tree returns nothing; on death the process simply exits).

`help`, `status` and `flee` are absent from **both** `parser.py` and `engine.py`. The parser's catch-all (`parser.py:110-114`) forwards `help` as `action='help'`; the engine's dispatch has no such branch and falls through to `print("Unknown command.")` at `engine.py:312`. This is the cross-module seam of B: the parser passes anything through, and the dispatch silently defines the real vocabulary.

### ROOM GRAPH & PLACEMENT
**8 of 8 authored rooms reachable from `entry_courtyard`**, all visited. **No disconnected component. No unplaced entity** — all 5 items, both NPCs, all 4 monsters placed (monsters carry both `room_id` and a room `monster_id`). Two harmless asymmetries: `storm_basement.north → sealed_beacon` but `sealed_beacon.south → spiral_stairwell`; `spiral_stairwell.east → keepers_quarters` with no return.

### STATE INTEGRITY — **round-trips cleanly**
Real `save` / `load` verbs, optional filename argument, full payload:
```
Game saved to save.json.
{"player_location": "foggy_dock", "inventory": ["rusted_cutlass"], "equipment": {"weapon": "rusted_cutlass"},
 "stats": {"health": 82, ...}, "rooms_items": {...}, "monsters_defeated": {"barnacle_crab": true, ...},
 "npc_dialogue_progress": {}}
=== fresh process ===  > Game loaded from save.json.  === Foggy Dock ===  (crab gone, cutlass equipped, HP 82)
```
Location, inventory, equipment, stats, **world item positions**, defeated monsters and dialogue progress all survive. Missing-file load degrades gracefully: `Load failed: [Errno 2] No such file or directory: 'nosuchfile.json'`. One gap: in-progress monster HP is not saved (`engine.py:306` admits it in a comment) — a crab left at 25/30 reloads at 30/30. *(model-innate)*

### ROBUSTNESS TABLE
| probe | behaviour | class |
|---|---|---|
| `xyzzy` | `Unknown command.` | clean refusal |
| **whitespace-only line** | **`IndexError: list index out of range` traceback, process dies** (`parser.py:112` — the `if not raw` guard runs *before* `.strip()`) | **hard crash** |
| `go up` | `Can't go that way.` | clean |
| `go` / `take` bare | `Unknown command.` / `No such item here.` | clean |
| `take nothing` | `No such item here.` | clean |
| `drop`/`equip`/`use` absent item | `You don't have that.` | clean |
| `talk to nobody` | `No one here by that name.` | clean |
| `choose 99` / `0` / `-1` / `abc` | `Invalid choice.` / `No active conversation.` | clean |
| `load nosuchfile.json` | `Load failed: [Errno 2] ...` | clean |
| `attack` with no monster | `Nothing to attack here.` | clean |
| `help` / `status` / `flee` | `Unknown command.` | dead required verb |
| Ctrl-D / EOF | **clean exit 0** (`engine.py:90-91`) | clean |
| move away mid-combat | works, monster HP persists in-process | clean |

**Worst impact: the whitespace hard crash.** Every other refusal is specific and honest.

### SEAM BUGS FOUND (none stopped me)
1. **The secret passage is unreachable.** `examine loose brick` in the Keeper's Quarters registers the new exit under the key `secret` (`engine.py:215`), which no direction in `parser.py:44-51` maps to — and the message names the wrong direction:
```
> You pry the loose brick and reveal a hidden passage to the south.
> Exits: west, south, secret
> go secret      Unknown command.
> secret         Unknown command.
```
(`south` is the Salt Cellar, unchanged.) The one genuine discovery in the game rewards you with a door that cannot be opened.
2. **The dialogue prompt refuses the input it offers.** `Choose an option number or type 'choose <n>'.` — typing `1` gives `Unknown command.`; only `choose 1` works. The prompt's own second clause is the escape hatch, so a player can recover.
3. **The Rusted Key is inert.** `description: An old iron key needed to open the Sealed Beacon door` — `grep` for a lock check returns nothing; I walked into the beacon without ever picking it up.
4. **Phase 2 never announces.** `phases: 2` and `Phase 2 summons echoes` are in the data; the engine computes `phase` (`engine.py:329`) and prints only the phase-1 immunity line, so a winning player never sees a transition.
5. **The lens clue points at nothing.** `Warden's Echo: Behind the painting in this gallery lies the Sunshard Lens.` — the lens is a plainly visible room item, and `examine painting` → `Nothing special.`
6. Dead branch of the campaign's known shape: `has_lens` also tests `equipment.get('weapon') == 'sunshard_lens'`, but the lens has `equip_slot: null` and can never occupy that slot. Harmless — it is OR'd with the inventory check that actually fires.

### THE BOSS GATE — B's best-delivered design
Arriving without the lens is a legible, instructive death rather than a mystery:
```
> Drowned Warden is immune to your attacks in phase 1!
  You hit Drowned Warden for 0 damage. HP 80/80
  Drowned Warden hits you for 10 damage. HP 72/100
  ... HP -8/100   You have been defeated...
```
The weakness item is **mandatory**, not a damage bonus, and the NPC hint (`The Warden fears true light. Find what focuses the sun.`) is load-bearing. *(model-innate, credit)*

### MODIFICATION PROBE (B9) — **clean pass**
Added a ninth room (`Tide Pool Grotto`) as a data block in `world.yaml` plus one `south:` exit on `storm_basement`. **Zero code files touched, nothing broke**, walked into it in play. Weapon damage (`5` → `25`) also data-only: `You hit Barnacle Crab for 30 damage.`

**Probe caveat that does not appear in the probe:** monster *behaviours* are keyed on the monster ids themselves (`engine.py:358/364/380` — `'barnacle_crab'`, `'ghostly_keeper'`, `'storm_rat_swarm'`), so a fifth monster gets no behaviour without a new engine branch. `combat_round` also hardcodes `'drowned_warden'` and `'sunshard_lens'` as literals despite `weakness_item_id` existing in the data and being used generically for non-boss monsters.

### OTHER FINDINGS
- Three genuinely distinct monster mechanics, all observed: crab double-attacks below half HP (`Barnacle Crab hits you for 6 damage.` twice in one round), `Ghostly Keeper flees in fear!` at half HP, rat swarm applies a +2 poison tick.
- Real branching dialogue: a 5-node graph for Finn (including a `Show cutlass` option) and a 3-node graph for the Warden's Echo, with `npc_dialogue_progress` persisted.
- Healing works mid-fight: `You use Vial of Brine-Medicine and recover 15 HP.`
- Combat is non-modal — monsters are advertised with live HP (`A hostile Ghostly Keeper is here! HP 35/35`) and you pick your fights; walking away is the de facto flee.
- Prints only what changed; the room block reappears on move and `look` only.

### ATTRIBUTION
Every finding on both artifacts is **model-innate** — logic authored in the shipped tree. No `interaction` observations: neither tree contains a shipped save file, stub tool, counterfeit linter or workaround shim. The one **framework-coupled** item is project layout (scaffold-dictated, identical for both, excluded per §4); the bare-relative-path world load is shared verbatim by both artifacts and discriminates nothing. Documentation is in comparison (v2.1 §4) and is charged on B10.

---

# THE TEN AXES

## PANEL A — DELIVERY
| axis | choice | justification |
|---|---|---|
| **A1 working surface** | **B** | A's largest promise is dead on arrival — the win screen is live code no player can reach, and its `flee` verb, when it succeeds, ends your run; B's win, save and load all fire in play despite three absent verbs. |
| **A2 state integrity** | **B** | B round-trips location, inventory, equipment, stats, room item positions, defeated monsters and dialogue progress through explicit `save`/`load`; A's save is write-only, omits world item positions, is never read by anything, and silently clobbers itself on every exit. |
| **A3 robustness** | **B** | B refuses cleanly and specifically on twelve of thirteen probes and exits cleanly on EOF, against one whitespace crash; A tracebacks on EOF at three sites *and* is silent on every unrecognised input, converting any combat typo into an attack that costs health. |
| **A4 delivered scope** | **B** | B landed multi-node dialogue graphs, three mechanically distinct monsters, a mandatory-item boss gate, working persistence and a reachable ending; A landed three more verbs and stopped at one-line NPCs, inert phases and an unwinnable boss. |

**Delivery: B 4 – A 0**

## PANEL B — CHARACTER
| axis | choice | justification |
|---|---|---|
| **B5 ambition** | **B** | B reached for a boss immune to steel until a specific item is carried, per-monster mechanical identity, a branching dialogue graph, a key-and-lock, and a secret passage revealed by examining scenery named only in room prose — A attempted none of these; that several of B's reaches are unwired is Panel A's business, not this axis's. |
| **B6 imagination** | **B** | "Sable Spire Lighthouse", the Drowned Warden in a lightless beam, a Warden's Echo whispering *"Light... light... the lens is hidden where paint is false"* — against Entrance Hall / Armory / Library / Kitchen and *"A dim hall with stone walls."* with a Goblin, an Orc, a Troll and a Dragon. |
| **B7 experience (UX)** | **B** | On every sub-question the rubric names — tense vs arithmetic combat, NPCs worth talking to, feedback, tension, sense of place — B wins: A's fights are five identical numbers with no decision and its NPCs repeat one line forever, while B's crab escalates, keeper flees, poison ticks, and the immunity wall teaches you what to do. **Charged at full weight against B and still called:** `help` and `status` are refused, so I obtained B's verb list from the README and `parser.py`, knowledge a player cannot get from inside the program; the dialogue prompt refuses the bare number it offers; and the secret passage I uncovered was an exit no input can take. **Charged against A:** it gives *zero* feedback for any mistyped command and silently spends a combat turn on it. |
| **B8 craft (UI)** | **A** | A is the only one of the two that *has* a help listing and a status line (`HP: 16/20 \| Location: Cave` / `Equipment: weapon:Rusty Sword, armor:Iron Armor`), and it accepts every display name it prints; B's surface is missing two of the four utility commands outright, which is the larger hole even against A's phantom `save` entry, total silence on unknown input and room-block reprint after every command. |
| **B9 workability** | **A** | The modification probe tied exactly — both took a ninth room and a weapon-damage change as pure data with no code touched — so this turns on organisation and reuse: A splits parser / combat / save / loader / models / engine at the boundaries you would choose and encodes behaviours as reusable archetypes (`cautious`, `defensive`) any monster can adopt, where B carries dispatch, combat, dialogue and a special-cased brick in one 384-line engine and keys behaviours on the monster ids themselves, so a new monster gets nothing without a new branch. |
| **B10 documentation** | **A** | Accuracy against play first: B's README is the *only* vocabulary source for a game with no `help`, and its headline Commands block lists three verbs — `help`, `status`, `flee` — that do not exist, while its `status shows health, equipment and location` line is flatly contradicted; A's three errors are peripheral (a "branching dialogue" overclaim, a load feature that never fires, a missing `requirements.txt` with a working `pip install pyyaml` offered on the next line) and its layout/dependency sections are fuller. |

**Character: B 3 – A 3**

---

# OVERALL: **B**

**No PANEL SPLIT.** The panels do not disagree in direction — Panel A is unanimous for B and Panel B is evenly divided, with A taking exactly the three axes that reward a checkbox surface (help/status, module separation, doc accuracy) and B taking the three that reward reach, world and felt play.

**Not flagged CLOSE.** Panel A is 4–0 and rests on the flight's headline fact: **B is completable and A is not.** A's `*** YOU WIN! ***` is written, wired to `game_won`, and unreachable by one hit point on the best play line the world admits — the amulet doubles on odd turns only, the single potion cannot cover the 16 damage the Dragon deals before it dies, and the only escapable monster ends your run for escaping. B ships an inferior surface — no help, no status, no flee, no restart, and a crash on a blank line — but it ships a game you can finish, save, reload and finish again.

The consolation for A is real and is where its three Panel B votes come from: it is the better-organised codebase, the only one with a status line, and the only one whose README does not lie about its own commands. If A's flee/death bool conflation at `combat.py:36` / `engine.py:107` were split into two return values and the potion or the Dragon's attack retuned by a single point, the flight would be much closer. As shipped, it cannot be won.
