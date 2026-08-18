# FRONTIER FLIGHT — muse-glimmer-30b COMPLETED artifact (first landed mission)

*Dispatched 2026-08-17 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
tier_20260816-150520/staged/arm01 (CANDIDATE — muse-glimmer-30b, the first
COMPLETED session-mode mission: 59/59 goals, final gate SUCCESS, 1663min,
5 documented seams incl. the operator-directed clearance of 5 artificial
re-mint goals); B = dev/blind_panel/anchors/v2.0/frontier-sonnet-20260803
(FRONTIER anchor). Candidate on A (rotation honored after sitting B in
flight_20260815_frontier_vs_arm02). Leak record: the model self-named again
(pyproject + README); normalized in the packet copy only, re-scanned clean.
FRONTIER is an out-of-band scorecard — it never moves the ladder.

VERDICT: Delivery B 4-0 · Character B 6-0 · OVERALL B. No split, no CLOSE
flag. Candidate WON its game (46/47 conformance, 9/9 rooms reachable, zero
unplaced entities, zero seam bugs — the campaign's historic killers all
absent). Decisive defect: flee permanently deletes the fled monster (boss
included) via an unserialized monster_fled set — and a SHIPPED TEST asserts
the bug as correct, in a red suite (6/29 failing) whose filenames are
quality-gate goal descriptions verbatim. Judge's verbatim record follows.*

---

I have played both artifacts to a win, run the probe batteries, walked both room graphs, and completed both modification probes. Both scratch copies were restored byte-identical; nothing was written into either packet artifact tree.

---

# ARTIFACT A — *Sunken Lighthouse*

**PREMISE LINE.** A nine-room vertical crawl up and down a drowned lighthouse, where a storm-thing called the Warden squats on the lamp and the only way to hurt it is a sun-bleached lens sealed in a vault under the flooded basement. Its world is a single declarative YAML file — rooms, items, NPCs, monsters, boss phases and the weakness link are all data — and both NPCs answer through numbered player-choice dialogue trees. Quoted prose: *"Light is its bane. Find the sun-bleached lens in the vault beneath the basement."*

**ENTRY-POINT LEDGER.**
| documented way | result |
|---|---|
| `python main.py` (from artifact dir) | works |
| `python A/main.py` (any other cwd) | **traceback** — `FileNotFoundError: 'world.yaml'`; `loader.load_world("world.yaml")` is cwd-relative |
| `adventure` console script (`pip install -e .`) | declared in `pyproject.toml` as `main:main`; not exercised (no venv writes), and would inherit the cwd fault above |

**COMPLETABILITY: WON.** Full clear: coat → salve/key → cutlass → lens → summit.
```
You hit Lighthouse Warden for 36 damage.
Lighthouse Warden enters its second phase!
...
You have defeated the Lighthouse Warden! The light is restored. You win!
```
Weakness genuinely gates the fight — same loadout without the lens deals 9/hit and dies at boss HP 30/60. **Reading the source was NOT required to reach the win.**

**ROOM GRAPH / PLACEMENT.** 9 authored rooms, **9 reachable from `foggy_shore`**, one component (Shore→Lamp→Stair→{Summit, Basement→Vault, Quarters→{Bell Tower, Crypt}}). All 5 items placed in rooms, both NPCs placed, all 4 monsters placed. **No disconnected component and no unplaced entity.** No cross-module seam bug: the room prints `Salt-Stained Oilskin Coat` and the parser accepts exactly that string; the boss weakness keys on inventory (`weak_to_item_id: lantern_lens`), not an equipment slot, so the classic slot-gate trap is avoided.

**CONFORMANCE: 46/47 — NEAR-FULL.** Unmet: **#16**. `help` enumerates 19 commands and omits `examine` and `go`, both named in the brief, plus `restart`. A new player reading A's help cannot learn that `examine` exists.

**STATE INTEGRITY.** Save/load round-trips player stats, equipment, inventory, `room_items` per room, `defeated_monsters` and `npc_progress` correctly — the coat stays taken, the crab stays dead, the Mariner stays at his advanced node. Two gaps: **monster HP is not serialized** (a crab left at 34/40 reloads at 40/40, and a boss mid-phase-2 reloads at phase 1), and the in-memory `monster_fled` set is not representable in the save at all, so the world and the save file disagree about whether a monster exists.

**DECISIVE DEFECT — `flee` permanently deletes the monster, boss included.**
```
> attack
You hit Lighthouse Warden for 2 damage.
> flee
You flee from combat.
> look
Lighthouse Summit
Exits: down
> attack
There's nothing to attack here.
> down
> up
Lighthouse Summit ... Exits: down          # the Warden is gone for good
```
`engine.py:325` adds the id to `self.monster_fled`, which is cleared only by `restart` (line 134) and never persisted. One `flee` on the summit makes the run **permanently unwinnable in-session**, silently. Attribution: **model-innate** in the code — but **interaction** in the ledger, because A's own shipped test ratifies it:
```python
def test_flee_from_combat_removes_monster_and_returns_message():
    ...
    assert "barnacle_crab" in engine.monster_fled
    assert engine._monster_in_room() is None
```
That test **passes**. The suite green-lights the bug.

**OTHER PLAY DEFECTS.** Healing is infinite and free — `Vial of Kelp Salve` is never consumed and costs no turn (`use` three times, still in inventory, monster never retaliates). Monsters do not guard: you walk into and out of any fight at will, so `flee` has no purpose it does its job for. `Rusted Key` is inert — the "hidden chamber revealed by the rusted key" opens with an empty inventory. Combat is deterministic: identical damage numbers every round.

**SHIPPED TESTS (interaction).** 29 tests, **6 failing**: title screen / initial room description, help contents, examine-from-inventory, and both death-restart tests. Filenames read as a harness defect log (`test_untested__flee_command_was_not_exercised_by_the_`, `test_no_title_screen_appears_before_play_begins`). `pytest` is declared a **runtime** dependency in `pyproject.toml`.

**ROBUSTNESS BATTERY (A).**
| # | probe | behaviour | impact |
|---|---|---|---|
| 1 | unknown command `xyzzy` | `You don't see any yzzy here.` | **silent misinterpretation** — `x` prefix-matches examine |
| 2 | `frobnicate the thing` | `I don't understand that.` | clean |
| 3 | empty input | `I don't understand that.` | clean, noisy |
| 4 | EOF in play | `Goodbye.`-less clean exit 0 | clean |
| 5 | EOF at start | clean exit 0 (no title prompt exists) | clean |
| 6 | `go sideways` / bare `go` | `I don't understand that.` / `You can't go south.` | clean |
| 7 | invalid item targets ×5 | `No item named 'unicorn'.` etc. | clean |
| 8 | mid-combat `up`/`talk`/`save` | all succeed; you walk out of the fight free | **silent misinterpretation** (fight abandoned, no cost) |
| 9 | malformed `save.json` → `load` | **traceback** `json.JSONDecodeError` | **crash** |
| 10 | valid JSON, wrong shape → `load` | **traceback** `KeyError: 'inventory'` | **crash** |
| + | 5000-char input / CJK / `LOOK` / multi-space | all absorbed; whitespace normalised | clean |

**Worst impact: two uncaught tracebacks on `load`, plus a cwd-dependent startup traceback.**

**MODIFICATION PROBE (A) — clean.** Added a tenth room (`tide_cellar`, east of the Crypt, with an item) and changed `rusted_cutlass` damage 7→40. **One file touched, `world.yaml`, zero Python.** Worked first try; new room reachable, item takeable, `ATK 45` confirmed, and the save's `room_items` picked up the new key automatically. This is A's genuine strength.

---

# ARTIFACT B — *The Ashen Keep*

**PREMISE LINE.** A nine-room ruined keep laid out as its own difficulty curve around a central stairwell, where a grieving lord has calcified into a cinder tyrant and two ghosts — a burned priestess and a living archivist — each hold half the answer, with a journal as a third redundant path to the same clue. Combat is randomized and stateful: a sentinel that braces behind its shield, a cur that goes frenzied below half health, a widow that poisons you. Quoted prose: *"The Ashen King's crown shatters. For one moment his ashen face looks almost human again -- almost grateful -- before he crumbles to cold cinder and silence."*

**ENTRY-POINT LEDGER.**
| documented way | result |
|---|---|
| `python main.py` | works, title menu `[N]/[L]/[Q]` |
| `python3 main.py` | same |
| `python B/main.py` (any other cwd) | **works** — no cwd coupling |
| `[L]` load from title | works, restores mid-run state |

**COMPLETABILITY: WON.** Full clear on the third serious attempt (I died twice first — genuinely).
```
Your Sunfire Brand flares bright -- a searing strike for 16 damage!
The Ashen King's crown cracks apart, spilling grey fire! "THEN BURN WITH ME!"
...
VICTORY
You defeated 4 foes in 13 combat rounds.
```
Weakness genuinely gates: the same fight with the Iron Longsword reads `Your Iron Longsword connects, but the blow feels blunted -- 6 damage.` and kills you. **Reading the source was NOT required to reach the win** — both NPCs and the journal name the Sunfire Brand and the crypt.

**ROOM GRAPH / PLACEMENT.** 9 authored rooms, **9 reachable from `courtyard`**, one component. All 4 monsters placed (`armory`/`storeroom`/`crypt`/`throne`), both NPCs placed, and of 7 registry items 6 are placed in rooms with `rusty_dagger` issued to the player at start — **no unplaced entity, no disconnected component.** No cross-module seam bug; the game actively discloses its own parser tokens in-room (`Sister Maren is here. (try: talk to sister)`) and also accepts the full printed name.

**CONFORMANCE: 47/47 — NEAR-FULL.** No unmet items. (#45 satisfied on the presence-lenient ruling — staged progression, not player-choice branching.)

**STATE INTEGRITY.** Best I have seen on this brief. `savegame.json` carries player stats, equipped gear, inventory, `location` **and** `previous_location`, `poisoned_turns`, `defeated_monsters`, `npc_stage`, and a per-room block with `items`, `visited`, **`monster_hp`, `guard_active`, `monster_turns_taken`** and boss phase. Round-trip verified from the title menu: HP 16/30, dead cur, Maren at stage 2 (third line, not first), taken items still gone. Monster HP also persists across fleeing and re-entering (`18/32` retained). `save` is refused mid-fight — `You can't save in the middle of a fight!` — exactly as the README states.

**DECISIVE DEFECT — none in play; the worst is an entry-point EOF.** `EOF`/Ctrl-D **at the title prompt** raises an uncaught `EOFError` in `game.py:86`; EOF during play is clean (`Farewell, wanderer.`).

**OTHER BLEMISHES.** `The The Ashen King was too much for you.` — the defeat template prepends an article the name already has. `status` prints raw ids (`Defeated: ravenous_cur`). Collapsed internal whitespace is not normalised (`take   Old   Journal` → `There's no 'old   journal' here to take.`). Phase-2 narration says *"His wounds knit shut with ember and smoke"* but no HP is restored (30/70 before and after). Any non-`y` answer at the restart prompt quits rather than re-prompting.

**ROBUSTNESS BATTERY (B).**
| # | probe | behaviour | impact |
|---|---|---|---|
| 1 | unknown command `xyzzy` | `I don't know how to 'xyzzy'. Type 'help' for a list of commands.` | clean, named |
| 2 | `frobnicate the thing` | same form, verb named | clean |
| 3 | empty input | silent re-prompt | clean |
| 4 | EOF in play | `Farewell, wanderer.` exit 0 | clean |
| 5 | **EOF at title prompt** | **traceback** `EOFError` | **crash** |
| 6 | bad title choice `Z`/`9` | `I didn't understand that. Try N, L, or Q.` + re-render | clean |
| 7 | invalid targets ×6 + `attack`/`flee`/bare `go` with no target | `Take what?` / `There's no 'unicorn' here to take.` / `Go where? Try north, south, east, or west.` | clean, actionable |
| 8 | mid-combat `move` / `talk` / `save` | `You can't just walk away -- the Hollow Sentinel won't let you. (Try 'flee'.)` / `Not while you're fighting for your life!` / `You can't save in the middle of a fight!` | clean refusal |
| 9 | malformed `savegame.json` → `load` | `No valid save file found to load.` | **clean refusal** |
| 10 | valid JSON, wrong shape → `load` | loads with defaults, no crash | tolerant |
| + | 5000-char input / CJK / `LOOK` / `Go NORTH` / partial names | all absorbed; `take journal` accepted | clean |

**Worst impact: one uncaught traceback, on EOF at the title prompt only.**

**MODIFICATION PROBE (B) — clean.** Added a tenth room (`ossuary`, east of the Crypt) and changed `rusty_dagger` value 2→40. **Two files, both the obvious ones**: `adventure/world.py` (one `add(...)` call + one exit key) and `adventure/items.py` (one field). Worked first try; `ATK 44` confirmed, new room reachable, and save/load round-tripped it. Nothing unrelated broke. Structurally, `Monster` exposes a `take_turn(self, player, turn_number)` hook overridden by all four monsters — a new behaviour is a subclass, not an edit to a dispatcher.

---

# FLIGHT VERDICT — ten forced choices

## PANEL A — DELIVERY

| axis | choice | justification |
|---|---|---|
| **A1 working surface** | **B** | A's `flee` — a brief-mandated verb — permanently deletes the monster it flees, boss included, silently making the run unwinnable; A's healing item is never consumed and costs no turn; A's monsters do not guard and its Rusted Key is inert. Everything B offers worked when invoked, including the resistance→weakness flip, the brace/frenzy/poison behaviours, and finite turn-costing healing. |
| **A2 state integrity** | **B** | B serializes `monster_hp`, `guard_active`, boss phase, `poisoned_turns`, `previous_location` and `visited`, and reloads them exactly; A drops monster HP and boss phase entirely on save/load, and its `monster_fled` world mutation has no representation in the save at all, so world and file disagree. |
| **A3 robustness** | **B** | A: two uncaught tracebacks on `load` (malformed and wrong-shape JSON), a third on any non-native cwd, and a silent misinterpretation (`xyzzy` → `examine yzzy`). B: one traceback (EOF at the title prompt), zero silent misinterpretations, and a clean refusal on the same malformed-save probe that crashes A. |
| **A4 delivered scope** | **B** | Absolute landed volume: B ships 7 items to A's 5, four stateful monster AIs with turn counters and status effects, a title menu with load, quit confirmation, victory statistics, a redundant third hint channel, and a 140-line accurate README, with zero third-party dependencies. |

## PANEL B — CHARACTER

| axis | choice | justification |
|---|---|---|
| **B5 ambition** | **B** | Judged on the design each set out to build: B specified a 3-turn shield cycle that halves an incoming blow, a sub-half-health frenzy granting an extra action, a probabilistic poison with duration, a failable flee, movement-gating combat, and a resistance that inverts into a weakness — A's data model has no field capable of expressing a status effect or a turn cycle, its three behaviours being one-line `heal`/`flee`/`aggressive` tags. A reached further in exactly one place, and I record it: A attempted true branch-and-converge dialogue with numbered player choices, a mechanic B never attempted. |
| **B6 imagination** | **B** | Ignoring whether any of it works: B's premise (a lord whose grief curdled into a cinder tyrant, two ghosts each holding half the answer, a boss who looks "almost grateful" at the end) and its prose are markedly richer than A's competent, terse lighthouse; B's non-obvious idea — the same clue arriving three redundant ways, conversation, conversation, and a journal, so no player is stranded — is the better one. |
| **B7 experience (felt play)** | **B** | B's combat is tense — randomized, monsters block the exit, healing is finite and costs a turn, and it killed me twice before I found armour; A's is arithmetic, with identical damage every round, infinite free healing, and fights you can simply walk out of. B's NPCs pay you for returning ("Talk to them more than once"); A's terminate after one exchange, and choosing branch 2 makes branch 1 unreachable forever. Neither artifact required source-reading to win, and I say so for both. |
| **B8 craft (UI)** | **B** | Input tolerance is the mechanism: B accepts `take journal` for `Old Journal` and prints its own parser hints in-room, while A refuses `take coat` and `take Oilskin Coat` for a name it just printed as `Salt-Stained Oilskin Coat`. B's help is complete and matches play; A's omits `examine` and `go`. A also has no room description at game start and misparses `xyzzy`. B's counter-blemishes (`The The Ashen King`, raw `ravenous_cur` in status, no multi-space normalisation) are cosmetic against those. |
| **B9 workability** | **B** *(closest axis)* | Both mod probes succeeded first try, and A's was cleaner in isolation — one YAML file, zero Python. But taken as one question, A's behaviour lives in a 405-line `engine.py` whose `process_command` is a single ~290-line if/elif spanning lines 112–402, and it ships a **red** 29-test suite (6 failing) that additionally *asserts the flee bug as correct*, so a newcomer cannot tell their breakage from the inherited kind. B's seams are where you would put them — seven cohesive modules, a `Monster.take_turn` hook, per-type registries — and a new behaviour is a subclass rather than another branch in the dispatcher. |
| **B10 documentation** | **B** | Accuracy first: A has two claims contradicted by play (`quit` "exits to the title screen" — it exits the process; the `adventure` entry point, which cannot work from any cwd but the source directory) in 59 lines. B has one — the ASCII map is consistently east-west mirrored and hangs the Courtyard off the Armory — inside 140 lines that are otherwise unusually accurate: the command block matches `help` verbatim, all three monster behaviours match what they do, and "Saving is disabled mid-fight" is true. Then completeness, where B is far ahead. |

---

## TALLIES

**Delivery: B 4–0 · Character: B 6–0**

**OVERALL: B.**

**No PANEL SPLIT** — both panels point the same way.

**No CLOSE flag.** B wins on the two questions the instrument separates: it delivers more (A ships a decisive, silent unwinnable-state bug in a brief-mandated verb, two `load` tracebacks and a cwd-fragile entry point; B ships one EOF traceback at a prompt) *and* it reaches further (stateful per-monster AI, status effects, combat-gated movement, full combat-state serialization). A's real, recordable strengths — a genuinely data-driven world file that made the modification probe a one-file YAML edit, and the only true player-choice dialogue tree in the pair — are not enough to turn any single axis, but B9 is the axis where A came closest and would be the first to move under a second judge.

**On the campaign's standing expectation:** the decisive defect here was **not** a cross-module seam bug. Both room graphs are single components, both have zero unplaced entities, and both parsers accept the names their rooms print (A grudgingly, in full only). A's decisive defect is intra-module — `flee` semantics inside `engine.py`, ratified by A's own passing test.
