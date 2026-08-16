# FRONTIER FLIGHT — muse-glimmer-30b arm02 vs the Sonnet frontier anchor

*Dispatched 2026-08-15 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
dev/blind_panel/anchors/v2.0/frontier-sonnet-20260803 (FRONTIER anchor, "The
Ashen Keep"); B = tier_20260815-115356/staged/arm02 (CANDIDATE —
muse-glimmer-30b session base + quality resume, the arm that beat the
Guardian 4-0/5-1 the same day). Candidate on B (rotation). FRONTIER is an
out-of-band scorecard per TIER_JUDGEMENTS — never moves the ladder. METHODS
§5 family-bias caveat stamped: the judge is Opus and the Frontier artifact is
Claude-authored. Judge's verbatim record follows.*

---

I have everything I need. Both artifacts played to completion, probed, and modified.

---

# BLIND COMPARATIVE FLIGHT RECORD — TIER_RUBRIC v2.1

Packet: `/Users/lah-rb/ouroboros-runs/flight_20260815_frontier_vs_arm02`
Both artifacts copied into `<packet>/scratch/{A,B}/artifact/` before play; no save file collision.

---

## ARTIFACT A — record

### PREMISE LINE
*The Ashen Keep* is a nine-room crawl through a burned castle whose lord let grief curdle him into a cinder-tyrant, where three monsters physically bar the corridors and two survivors — an archivist and a nun — each hold half the secret of the one blade that can hurt him. It is a game about listening: ordinary steel literally slides off the boss, and the only way to learn that is to talk to people more than once.

> "The Ashen King rises from his throne of fused bone. 'Another fool comes to warm my hall,' he rasps, ash sifting from his shoulders like grey snow."

### COMPLETABILITY: **WON**

```
Your Sunfire Brand flares bright -- a searing strike for 12 damage!
(The Ashen King: 0/70 HP)
The Ashen King's crown shatters. For one moment his ashen face looks almost
human again -- almost grateful -- before he crumbles to cold cinder and silence.
======================================================================
VICTORY
======================================================================
You defeated 4 foes in 13 combat rounds.
```

**Furthest point:** full victory. All 9 rooms visited, all 4 monsters engaged, both NPCs talked to their terminal line, every item class taken/equipped/used.

### CONFORMANCE: **47/47 · unmet: none · NEAR-FULL** (no trigger fires)

Verified in play, not by grep: two-phase boss live (`The Ashen King's crown cracks apart, spilling grey fire! "THEN BURN WITH ME!"`), weakness item placed in the crypt and inverting a real resistance, defeat screen with restart (`y` at the prompt reset the world to Ruined Courtyard), healing usable mid-combat, three distinct monster scripts.

### ROOM GRAPH & PLACEMENT
- 9 authored rooms, **9/9 reachable** from `courtyard`. No disconnected component.
- Registry vs placement: 4/4 monsters placed, 2/2 NPCs placed, 6/7 items placed in rooms. The 7th (`rusty_dagger`) is the starting weapon in the player's inventory by design — **not** an orphan.
- **No unplaced entity. No disconnected wing.**

### STATE INTEGRITY: clean round-trip
Save captures player stats, equipment, inventory, location *and* previous location, poison counter, defeated monsters, per-NPC dialogue stage, and per-room items/visited/monster HP/boss phase. Loading from the title screen restored the library with its breastplate already taken and Rell advancing to his *next* line rather than his first. Mid-fight save correctly refused: `You can't save in the middle of a fight!` Missing and corrupt save files both → `No valid save file found to load.`

### ROBUSTNESS TABLE (10 probes)

| # | probe | result |
|---|---|---|
| 1 | unknown command | clean — `I don't know how to 'xyzzy'. Type 'help' for a list of commands.` |
| 2 | empty input | clean — silent re-prompt |
| 3 | whitespace-only | clean — silent re-prompt |
| 4 | verb, no argument | clean — `Go where? Try north, south, east, or west.` / `Take what?` |
| 5 | nonexistent noun (take/drop/use/equip/examine) | clean — `There's no 'unicorn' here to take.` |
| 6 | talk to nobody | clean — `There's no one here to talk to.` |
| 7 | attack/flee out of combat | clean — `There's nothing to flee from right now.` |
| 8 | invalid direction `go up` | clean refusal |
| 9 | **EOF** | **MIXED** — main prompt clean (`Farewell, wanderer.`); **title menu and quit-confirmation raise `EOFError` traceback** |
| 10 | missing / corrupt save | clean, same friendly message for both |

**Worst impact:** `EOFError` traceback at two sub-prompts. Secondary: any non-`y` at the defeat prompt (`game.py:557` is a single `input()`, not a loop) exits with **no message at all**.

### MODIFICATION PROBE — passed
Added a tenth room (Cracked Well, east of the courtyard) with a new item, and changed Iron Longsword damage 6→99. **Touched exactly two files** — `adventure/world.py` (one `add(...)` block + one exit key) and `adventure/items.py` (one registry entry + one integer). No unrelated code. New room fully playable (exits, item listing, `take`, `examine`); damage change took effect (`Attack: 103`); save/load still round-tripped **including the new room**. Nothing broke.

### SEAM BUGS
**None stopped me. I completed A start to finish using only what the game printed on screen.** The one naming narrowness — the parser wants `talk to old`, not `talk to Rell` — is *disclosed by the room itself*: `Old Archivist Rell is here. (try: talk to old)`. Item matching is substring-based, so `take journal`, `equip breastplate` and `use healing draught` all work against the full printed names.

### ATTRIBUTION
- *model-innate*: EOF tracebacks at sub-prompts; `A The Ashen King blocks your way!` and `The The Ashen King was too much for you.` (double article); raw id leak in `Defeated: ravenous_cur`; README map edge error.
- *interaction*: ships `adventure/__pycache__/` with 8 `.pyc` files — build residue committed into the tree. Minor handicap, charged.
- *framework-coupled*: project layout only (excluded per §4).

---

## ARTIFACT B — record

### PREMISE LINE
*Sable Spire Lighthouse* is an eight-room exploration of a drowned lighthouse where a waterlogged Warden stands immune in the lightless beam, and the only thing that can touch it is a crystal lens hidden behind a false painting two rooms from the front door. It is a game about light against steel, told through two ghosts of the place who give you the answer the first time you ask.

> "Warden's Echo: Light... light... the lens is hidden where paint is false."

### COMPLETABILITY: **WON**

```
You hit Drowned Warden for 15 damage. HP 0/80
You defeated Drowned Warden!
The lighthouse is freed. You win!
```

**Furthest point:** full victory — and then a second, shorter one. **B is winnable in 10 commands** (`north`, `take Sunshard Lens`, `north`, `north`, `attack`×6) with no weapon, no armour, no healing item, no NPC conversation, and without fighting a single regular monster. 4 of its 8 rooms are pure optional detour.

### CONFORMANCE: **44/47 · unmet: 11, 16, 33 · NEAR-FULL** (count trigger not reached; 3 < 10)

- **#11 `flee` — ABSENT FROM THE TREE.** `grep -rn "flee"` returns only a *monster* fleeing (`engine.py:401`), a world.yaml description, and a README claim. The parser has no `flee`; the engine has no flee action. In play: `> flee` → `Unknown command.` — including inside the boss room.
- **#16 help does not list the real vocabulary.** It lists `move north/s/e/w` as *the* movement syntax (does not work), and omits `go`, `status` and `choose` (all of which do).
- **#33 restart — ABSENT FROM THE TREE.** `grep -rni "restart|new game|play again|try again"` returns nothing. Death prints one line and exits.

**Core-loop note:** I considered firing the core-loop trigger on `flee`, since the brief names it as one of the two combat verbs. I did not: terminal states (win and defeat) and the central mechanic chain (explore → gear → weakness item → boss) are all present and all worked. I record the judgment explicitly because it is arguable.

Item 28 is scored **MET on presence** per the checklist's authored-and-inert rule (`phases: 2` in data, "Phase 2 summons echoes" in the boss description) — the deadness is charged on the axes below.

### ROOM GRAPH & PLACEMENT
- 8 authored rooms, **8/8 reachable** from `entry_courtyard`. No disconnected component.
- Registry vs placement: 5/5 items placed, 4/4 monsters placed, 2/2 NPCs placed.
- **No unplaced entity. No disconnected wing.** Neither artifact carries the campaign's most common decisive defect.

### STATE INTEGRITY: **does not round-trip**
Player HP, location, inventory, equipment, room items and NPC dialogue progress all restore correctly. But **monster HP is not persisted.** Saved with the Barnacle Crab at 10/30; after `load` in a fresh process:

```
=== Foggy Dock ===
A hostile Barnacle Crab is here! HP 30/30
```

`monsters_defeated` stores booleans only. Boss phase has no state at all. Loading rewrites the world's combat state.

### ROBUSTNESS TABLE (10 probes)

| # | probe | result |
|---|---|---|
| 1 | unknown command | clean — `Unknown command.` |
| 2 | empty input (bare Enter) | clean — `Unknown command.` |
| 3 | **whitespace-only (one space or tab)** | **TRACEBACK — `IndexError: list index out of range` at `parser.py:120`; process dies** |
| 4 | verb, no argument | clean |
| 5 | nonexistent noun | mostly clean; `examine unicorn` → `Nothing special.` (silent misinterpretation) |
| 6 | talk to nobody | clean — `No one here by that name.` |
| 7 | attack out of combat / flee | `Nothing to attack here.`; **`flee` → `Unknown command.`** |
| 8 | **`move north` where north exists** | **SILENT MISINTERPRETATION — `Can't go that way.`** |
| 9 | EOF | clean — silent exit, no traceback |
| 10 | save/load edges | load missing/corrupt clean (`Load failed: ...`); **`save /tmp/nope/deep/x.json` → `FileNotFoundError` traceback, process dies** |

**Worst impact:** two tracebacks reachable by ordinary typing that destroy an unsaved session. The whitespace crash is the severe one — `parse_command` guards `if not raw` but then `raw.strip()` empties the string, and `parts[0]` explodes on the fallback path. A stray space bar ends the game.

### MODIFICATION PROBE — passed, cleanest of the two
Added a ninth room (Tide Pool, west of the courtyard) with a new item, and changed Rusted Cutlass damage 5→99. **Touched exactly one file — `world.yaml`. No Python at all.** New room fully playable; damage change took effect (`You hit Barnacle Crab for 104 damage`). Nothing broke. The data/code split is real and it is B's strongest engineering property.

### SEAM BUGS — three, plus a dead promise
A seam bug is not what stopped me (B's win path is short and unguarded), but three shaped play, and **I resolved two by reading the tree, which a player cannot do**:

1. **help vs parser, on movement.** Help says `move north/s/e/w`. `parser.py` has no `move` verb — the string falls through the generic tail (`action = parts[0]`, direction `None`) and `engine` prints `Can't go that way.` A player following the help concludes the exit is closed. I found `go north` by trial and learned *why* `move` fails by opening `parser.py`.
2. **README vs tree, on `flee`.** Documented; nonexistent. Confirmed by grep after play showed `Unknown command.`
3. **`phases: 2` vs `engine.py:365`.** The engine computes `phase = 2 if hp <= max_hp // 2 else 1` and then uses it for exactly one thing — gating phase-1 immunity. There is no phase-2 behaviour, no transition message, and no "echoes". Worse, the variable can never matter: **with** the lens, phase 1 is not immune; **without** it, damage is 0 forever so HP never falls and phase 2 is unreachable. Without the lens the boss is an unkillable soft-lock that grinds you down 10 HP a turn.
4. **`Rusted Key`** — `An old iron key needed to open the Sealed Beacon door.` There is no lock anywhere in the tree. I walked into the boss room with an empty inventory. `use Rusted Key` → `Nothing happens.`

### ATTRIBUTION
- *model-innate*: the whitespace and save-path tracebacks; all four seams above; monster HP not saved; the hardcoded `if found_npc == 'wardens_echo'` conditional-dialogue path that scans for any node id containing `'lens'`.
- *interaction* — **charged, per CHARGE WHAT SHIPS**, and cutting both ways:
  - **Handicap:** ships `tests/` with **1 of 5 failing on checkout** (`npc_dialogue_progress must remain a string node id`). The tests are named after brief requirements, and two are gate-shaped rather than behaviour-shaped: the "title screen" test `print("Sable Spire Lighthouse")` **inside the test body** and then asserts on its own print; the dialogue test's key assertion is an or-chain (`assert lens_aware or "Light... light..." not in lowered or "hidden where paint is false" not in lowered`) that is nearly always true. They also drive the engine with bare ids (`talk to wardens_echo`, `take sunshard_lens`) — a surface the player-facing parser accepts only by coincidence, so the suite does not test what a player touches.
  - **Advantage:** B is the only artifact that declares its dependency and gives an install path — `requirements.txt` (PyYAML), `pyproject.toml`, `.gitignore`.
- *framework-coupled*: project layout only (excluded).

### One genuine win for B worth recording
Warden's Echo has **real inventory-conditional dialogue**, verified in play:

```
[without lens]  Warden's Echo: Light... light... the lens is hidden where paint is false.
                  1. Ask about the boss
                  2. Ask about the lens
[with lens]     Warden's Echo: Behind the painting in this gallery lies the Sunshard Lens. Bring it to the beacon.
```

Finn offers a real numbered option menu with player choice. That is a stronger dialogue *design* than A's stage counter — though Finn's `3. Show cutlass` branch is unconditioned and fires verbatim (`Ah, you found the keeper's blade`) with an empty inventory, and the lens-aware node is a dead end that drops progress to `None`.

---

# THE TEN FORCED CHOICES

## PANEL A — DELIVERY

| axis | winner | justification |
|---|---|---|
| **A1 working surface** | **A** | Every verb A's help lists works; B's help names `move` as the movement syntax and it fails, `flee` is documented and absent, the two-phase boss is inert, the key is inert, and the shipped suite is 1/5 red. |
| **A2 state integrity** | **A** | A round-trips monster HP, boss phase, poison, NPC stages and per-room items exactly; B resurrects a wounded crab from 10/30 back to 30/30 on load and stores no boss phase at all. |
| **A3 robustness** | **A** | B's failures are reachable by pressing the space bar (`IndexError`, process dead) and by `save <bad path>` (`FileNotFoundError`, process dead); A's are EOF-only at two sub-prompts and never fire under ordinary typing. |
| **A4 delivered scope** | **A** | A landed 9 rooms, a live two-phase boss with a resistance that inverts, three real combat scripts plus a poison system, 3-stage dialogue on both NPCs and a third lore route; B landed 8 rooms with an inert phase system, an inert key, no flee, and no restart. |

**Delivery: A 4–0.**

## PANEL B — CHARACTER

| axis | winner | justification |
|---|---|---|
| **B5 ambition** | **B** | Judged on reach and not on wiring: B set out for a YAML-data-driven world with a loader/model layer, a dialogue **graph** with numbered player choices and inventory-conditional branching, a generic `weakness_item_id` on every monster, a keyed door, and arbitrary save paths — A set out for a linear dialogue ladder and one bespoke hardcoded boss. B reached further in kind; that most of it did not survive is Panel A's charge, not this one. |
| **B6 imagination** | **A** | A's keep is denser and far more voiced ("banners burned to lace", the king's face "almost human again -- almost grateful"), and its central conceit is non-obvious and doubled: the weakness *inverts* a resistance, and three separate routes — journal, nun, archivist — carry the same clue. B has a lovely premise and one great line, but most rooms are a single competent sentence and both terminal states are undecorated. |
| **B7 experience (felt play)** | **A** | A's monsters block corridors so every room is a decision and combat genuinely swings; B's monsters do not block, so you stroll past all of them to a boss that is 3 rooms and 10 commands from the start, winnable naked. **And I routed around two walls in B with knowledge no player can obtain** — I learned `move north` fails by reading `parser.py`'s fallback, and confirmed `flee` does not exist by grepping the tree. In A I needed the source for nothing; the game even prints `(try: talk to old)` and `Tip: people in this keep know things.` |
| **B8 craft (UI)** | **A** | A's help is complete and accurate, its errors suggest the fix (`You can't just walk away -- the Ravenous Cur won't let you. (Try 'flee'.)`), and item matching is substring-tolerant. B's help lists a verb that fails and omits three that work, `status` hides attack and defense, no partial names are accepted, `examine unicorn` answers `Nothing special.`, room ids leak (`Entry Courtyard (entry_courtyard)`), and dialogue options renumber so `choose 2` becomes `Invalid choice.` |
| **B9 workability** | **A** | *Closest axis on this panel — the probe itself favours B* (one YAML file, zero code, versus A's two source files). But the axis is logic organisation, project organisation and reusability as one question: B's data format **lies about what it controls** — `phases` is loaded and never read, `weakness_item_id` is overridden by `if monster_id == 'drowned_warden'`, behaviours are an `elif` chain keyed on each monster's own id, and the dialogue loader has no `condition` field at all, so the one conditional NPC is `if found_npc == 'wardens_echo'` plus a substring scan for `'lens' in nid.lower()`. A second conditional NPC or a fourth behaviour means surgery on `engine.py`. A's behaviours and NPCs are subclasses with hooks, items are a declarative registry, and a newcomer's first `pytest` does not report a failure. |
| **B10 documentation** | **A** | Accuracy is judged first. A's README survived every behavioural claim I tested — the save-contents description matched `savegame.json` field for field, "saving is disabled mid-fight" is true, all three monster behaviours and the boss's half-health phase flip are exactly right; its one error is an ASCII diagram that hangs the courtyard off the Armory. B's README is contradicted by play four times (`flee`, "help lists valid commands", "branching dialogue" on an NPC that answers `No options.`, "two-phase final boss"), though its Setup section is a real and unmatched plus. |

**Character: A 4–1.**

---

# TALLIES AND OVERALL

> **Delivery: A 4–0 · Character: A 4–1**

## OVERALL: **A**

**No PANEL SPLIT** — both panels point the same direction.

**No CLOSE flag on the overall.** A takes 8 of 10 axes, every delivery axis, and three of five character axes decisively. The separation is not marginal: A was won using nothing but what the screen printed, round-trips its world exactly, and cannot be crashed by typing; B ships two session-killing tracebacks (one of them a stray space bar), loses monster state on load, documents a combat verb that does not exist, and hangs a "two-phase boss" off a variable that can never change.

I flag **B9 alone as close** — B's modification probe was strictly cleaner than A's (one data file, no code), and B's project organisation and dependency declaration are genuinely better. Had the axis been the probe alone, it would have gone to B. It went to A on the deeper reusability question, because three of B's declared-generic data fields are decorative and every real extension lands in `engine.py`.

The one clear thing B reached further for is recorded on **B5** and stands: a data-driven world with a player-choice dialogue graph is a more ambitious design than A's hardcoded classes and stage counter. It is also the axis where nearly none of the reach survived contact — which is precisely why the two panels are reported apart and never summed.
