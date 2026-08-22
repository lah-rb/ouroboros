# FRONTIER SCORECARD — qwen3.8-27b (COMPLETED artifact) vs the FRONTIER anchor

*Dispatched 2026-08-22 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
tier_20260819-224515/staged-completed/alpha (CANDIDATE — qwen3.8-27b,
session structural, ran to mission COMPLETED: 44/44 goals including 3
quality-harvested, ~36h traced across 10 legs, 109 work cycles, 96.9%
prefix reuse, ZERO degenerations); B =
dev/blind_panel/anchors/v2.0/frontier-sonnet-20260803 ("The Ashen Keep",
claude-sonnet one-shot). Candidate at A (rotation honored). Scans clean
after 82ae1f2 — the anchor's .pyc carried its own build path, naming
its role and model.

VERDICT: Delivery **2-2 (TIE)** · Character B 5-1 · OVERALL: FRONTIER.
Not flagged CLOSE. OUT-OF-BAND scorecard, not ladder-bearing. METHODS §5
family caveat applies (Opus judge, Claude-authored frontier).

**THIS IS THE BEST LOCAL ARTIFACT THE CAMPAIGN HAS PRODUCED, by a
distance, and the numbers are not close to any prior local result:**
  * **WON** — reached and reproduced its own VICTORY ("The sun sigil
    rings out… The Sunken King shatters… the Hollow Crown's curse drains
    away into the deep"). Only the second WON local artifact ever, and
    the first that also converts everything else.
  * **47/47 conformance, NEAR-FULL, zero unmet items.** No prior local
    artifact has scored a clean sweep. The sitting guardian scores 41/47
    SIGNIFICANTLY-DEVIATED against this same rubric.
  * **11/11 rooms reachable, ZERO unplaced entities** (8/8 items, 4/4
    monsters, 3/3 NPCs). The campaign's most common decisive defect —
    seven prior artifacts including one of our own anchors — simply
    absent.
  * **ZERO tracebacks across the entire robustness battery**, including
    EOF at every prompt and a deliberately corrupted save. The FRONTIER
    crashed twice (uncaught EOFError at the title prompt, rc=1, and at
    the quit confirm).
  * **No seam bug stopped the judge.** First flight in the campaign
    where BOTH sides pass the placement audit outright.

**IT TIED THE FRONTIER ON DELIVERY, 2-2**, winning A3 robustness (the
traceback sweep) and A4 delivered scope (11 rooms vs 9, 8 items vs 7,
3 NPCs vs 2, plus 6 genuine passing tests and a real pyproject/ruff
setup against the anchor's none). It lost A1 working surface and A2
state integrity — the latter on the one real gap the judge found:
**monster HP is not saved**, so loading heals a wounded monster
(13/18 wolf restored to 18). That is the single most actionable defect
in the artifact.

**IT LOST CHARACTER 1-5**, and the reasons are worth keeping because
they are design reasons, not delivery ones: the anchor reached further
(save-persistent poison, turn-indexed monster scripts, a boss whose
RESISTANCE flips into the weakness so the special item is a weapon you
must still fight with), wrote characters with interiority, and shows
enemy HP every combat round. qwen's sigil, though given three
phase-dependent code paths, ENDS the encounter rather than shaping it,
and its combat never shows enemy HP unless the player thinks to type
`look` mid-fight. Its one Character win is B9 workability — the engine
returns List[str] instead of printing, which is WHY it is testable, and
it ships 6 real tests pinning design invariants.

CHARGED FOR THE NEXT ROUND: the article is baked into the monster name
so every combat line reads "You hit A Cave Wolf for 9 damage"; the
throne-room prose calls the boss "The Void King" while the monster and
all dialogue call him "The Sunken King"; `look <arg>` misroutes to
examine; bare `talk to` auto-targets; `equip` of an already-equipped
item says "You don't have that"; healing at full HP consumes the item
for 0 health.

Judge's verbatim record follows.*

---
## PER-ARTIFACT RECORDS

---

# ARTIFACT A — "The Hollow Crown"

**PREMISE LINE.** A rain-drowned village and the flooded keep above it, where a crowned corpse called the Sunken King is waking under black water; you gather rusted gear from eleven soaked rooms, pry a hint out of three half-mad locals, and go break his armour open. It is a water game — every room is wet, and the one thing that kills him is a shard of silver that rings like a bell when rain touches it.
> *"Tombstones rise from ankle-deep water. A bone amulet hangs from a nail above the oldest grave, and the dark forest path can be seen through a broken arch to the north."*

**COMPLETABILITY: WON.** Full route: Square → Mill (Iron Sword, Cuirass) → Village (Elder Maren ×3) → Shrine (Healing Draught) → Forest (Cave Wolf) → Mausoleum (Sun Sigil) → Keep Gate (Gate Goblin) → Throne Room.
> `The sun sigil rings out, a bright sacred sound across the drowned throne room.` / `The Sunken King shatters as the resonance tears through his armor.` / `The floodwater stills, and the Hollow Crown's curse drains away into the deep.` / `VICTORY`

**CONFORMANCE: 47/47 · unmet: none · NEAR-FULL.** No trigger fires. All 15 verbs present and working; `help` matches the real vocabulary including `save`/`load`; boss `void_king` has `phase2_threshold: 30` and `weak_to: sun_sigil`; win, defeat and restart all present.

**ROOM GRAPH & PLACEMENT.** 11 authored rooms, **11/11 reachable** from `crossroads` in one component. **Zero unplaced entities**: 8/8 items placed, 4/4 monsters placed (`cave_wolf`, `swamp_wraith`, `gate_goblin`, `void_king`), 3/3 NPCs placed. No dangling exits. **No seam bug stopped me** — the parser accepts every name it prints, plus partials (`take salve`, `equip sword`) and NPC first names (`talk to maren`).

**STATE INTEGRITY — one real gap.** Save captures location, health, base_attack, inventory, equipment, `defeated`, `fled`, `npc_progress`, `outcome`, and per-room `room_items`. Round-trip verified: dead wolf stayed dead, taken item stayed taken, untaken item stayed placed. **But monster HP is not in the save.** Wounded the wolf to 13/18, fled, saved, reloaded in a fresh process:
> `You hit A Cave Wolf for 5 damage.` / `A Cave Wolf has 13 health left.`

— i.e. the wolf was restored to a full 18 and the load healed it. Loading rewrites that much of the world. Restart-after-defeat resets cleanly.

**ROBUSTNESS TABLE**

| probe | result | impact |
|---|---|---|
| unknown command `xyzzy`, `!!!` | `I don't understand 'xyzzy'. Try help.` | clean refusal |
| empty / whitespace input | re-prints room | benign misinterpretation |
| bare `go` | `You can go: North, East, South, West.` | clean, helpful |
| invalid direction `go up` | `You can't go that way.` | clean refusal |
| case `TAKE`, `GO N` | works | tolerant |
| bare `take`/`examine` | `Take what?` / `Examine what?` | clean refusal |
| unknown noun (take/drop/use/equip) | 4 distinct correct messages | clean refusal |
| **bare `talk to`** | silently talks to whoever is present | **silent misinterpretation** |
| **`look extra words`** | routed to examine → `You see nothing special.` | **silent misinterpretation** |
| `attack`/`flee` with no monster | `There is nothing here to attack.` | clean refusal |
| move mid-combat | `You can't move while fighting. Attack or flee.` | clean refusal |
| save mid-combat | `You can't save in the middle of a fight.` | clean refusal |
| missing save | `No saved game found.` | clean refusal |
| corrupt save | `The saved game is corrupted.` | clean refusal |
| **EOF (Ctrl-D) at every prompt** — title, main, mid-combat, defeat | `You leave the rain and the ruin behind.`, rc=0 | **zero tracebacks anywhere** |

Worst impact observed: silent misinterpretation. **No traceback was produced by any probe.**

**MODIFICATION PROBE — clean pass.** Added a 12th room (*The Drowned Belfry*, off the Chapel) and changed Iron Sword damage 4→9. **Touched one file: `world.json`.** Zero code edits. New room walkable, item takeable, `status` showed `Attack: 14 (Iron Sword +9)`, and save/load absorbed the new room automatically (`belfry` appeared in `room_items`). Nothing broke.

**OTHER FINDINGS.** Three genuinely distinct monster scripts — guard (`raises its guard and blocks your attack for 0 damage`), drain (`feeds on your wounds and recovers 2 health`), lunge. Weakness item has three code paths: on a normal monster `It has no effect on this creature.`; out of combat `It hums faintly, but nothing happens.`; in boss phase 1 it *forces* the transition (`armor shatters and the spectral phase begins`); in phase 2 it kills. Fleeing permanently unlocks a monster's room (`fled` list) — a real systems idea. Tests: **6 passing**, driving the public command path and asserting design invariants (`engine.combat.phase == 2` after using the weakness item on the armoured boss). Blemishes: the article is baked into the monster name so *every* combat line reads `You hit A Cave Wolf for 9 damage`; the throne-room prose calls the boss **"The Void King"** while the monster and all dialogue call him **"The Sunken King"**; `equip` an already-equipped item → `You don't have that.`; healing at full HP consumes the item for `0 health`; combat never shows enemy HP unless you happen to type `look` mid-fight.

**ATTRIBUTION.** *model-innate:* world, prose, article bug, Void/Sunken drift, load-rewind, `look <arg>` misroute. *interaction:* ships `tests/` (5 charter-named files), `pyproject.toml` with a real ruff lint config, `requirements-dev.txt` — a visible build-harness relationship, and here an **advantage**: the tests are genuine, not counterfeit, and they pin design intent. Charged as shipped. *framework-coupled:* project layout only (excluded).

---

# ARTIFACT B — "The Ashen Keep"

**PREMISE LINE.** A burned-out keep where a grieving lord has calcified into a tyrant of ash, and two ghosts who died in the fire still remember the one blade that can touch him. You climb nine rooms from courtyard to throne, and the game's real subject is that killing him is a mercy he seems to want.
> *"A vast hall, its banners burned to lace. Cold hearths line the walls, ash still heaped in their mouths."*

**COMPLETABILITY: WON.** Route: Courtyard → Great Hall → Shrine (Maren, Draught) → Library (Breastplate, Rell) → Storeroom (Cur, Vest, Draught) → Armory (Sentinel, Longsword) → Crypt (Cave Widow, Sunfire Brand) → Throne. Won on the second attempt, by two turns.
> `The Ashen King's crown shatters. For one moment his ashen face looks almost human again -- almost grateful -- before he crumbles to cold cinder and silence.` / `VICTORY` / `You defeated 4 foes in 14 combat rounds.`

**CONFORMANCE: 47/47 · unmet: none · NEAR-FULL.** No trigger fires. All 15 required verbs present (plus `inventory` and `examine me`); `help` matches reality; `ashen_king` has a half-HP phase change and `boss_weakness=True` on `sunfire_brand`; win, defeat and restart present.

**ROOM GRAPH & PLACEMENT.** 9 authored rooms, **9/9 reachable** from `courtyard` in one component, and **every exit is reciprocal** (checked all 20 edges). **Zero unplaced entities**: 4/4 monsters placed, 2/2 NPCs placed, 6/7 items placed in rooms — the seventh, `rusty_dagger`, is the player's starting weapon and is intentionally in inventory, not a room. **No seam bug stopped me.** Every printed name is accepted, and the game volunteers the token it wants: `Sister Maren is here. (try: talk to sister)`.

**STATE INTEGRITY — clean.** Save captures everything A does *plus* per-room `monster_hp`, `guard_active`, `monster_turns_taken`, boss `monster_phase`, `poisoned_turns`, `visited` flags and `previous_location`. Wounded the Sentinel to 27/32, fled, saved, reloaded in a fresh process:
> `(Hollow Sentinel: 27/32 HP) -- attack, flee, use <item>, or equip <item>`

Nothing was rewritten. Restart after defeat fully resets (journal back in the hall, inventory reset, 30/30). Load works both in-game and from the title menu.

**ROBUSTNESS TABLE**

| probe | result | impact |
|---|---|---|
| unknown command `xyzzy`, `!!!` | `I don't know how to 'xyzzy'. Type 'help'…` | clean refusal |
| empty / whitespace input | silent re-prompt | clean |
| bare `go` | `Go where? Try north, south, east, or west.` | clean refusal |
| invalid direction | `You can't go north from here.` | clean refusal |
| case / bare directions (`GO NORTH`, `north`, `n`) | all work | tolerant |
| bare `take` | `Take what?` | clean refusal |
| unknown noun | `There's no 'unicorn' here to take.` / `You aren't carrying a 'unicorn'.` | clean refusal |
| **bare `examine`** | prints the room description | **silent misinterpretation** |
| `attack`/`flee` with no monster | `There's nothing here to fight.` | clean refusal |
| move mid-combat | `You can't just walk away -- the Hollow Sentinel won't let you. (Try 'flee'.)` | clean refusal |
| save mid-combat | `You can't save in the middle of a fight!` | clean refusal |
| missing / corrupt save | `No valid save file found to load.` then back to menu | clean refusal |
| bad menu key | `I didn't understand that. Try N, L, or Q.` | clean refusal |
| EOF at main prompt / mid-combat / defeat prompt | `Farewell, wanderer.`, rc=0 | clean |
| **EOF at the TITLE prompt** | **`EOFError: EOF when reading a line`, rc=1** | **traceback** |
| **EOF at the quit-confirm prompt** | **`EOFError` in `cmd_quit`, game.py:416** | **traceback** |
| non-`y` answer at restart prompt | silently exits (no re-prompt) | silent misinterpretation |

Worst impact observed: **uncaught traceback**, at two prompts. Note the title-screen crash is the *first thing* a piped or Ctrl-D'd invocation hits:
> `File ".../adventure/game.py", line 86, in show_title_screen` / `EOFError: EOF when reading a line`

**MODIFICATION PROBE — clean pass.** Added a 10th room (*Cracked Belfry*, off the High Library) and changed Iron Longsword damage 6→10. **Touched two files: `adventure/world.py` (one `add()` call + one exit key) and `adventure/items.py` (one integer).** Nothing broke; new room walkable and its item takeable; save/load absorbed the new room without any serializer change (`belfry` appeared in the save's `rooms` block and restored correctly).

**OTHER FINDINGS.** Combat is the strongest thing here: enemy HP is shown every round with the legal actions re-listed (`(Cave Widow: 24/24 HP) -- attack, flee, use <item>, or equip <item>`); the three monsters run genuinely different scripts — a turn-indexed shield brace with a *mechanical* consequence (`Your Rusty Dagger connects, but the blow feels blunted -- 2 damage.`), an HP-threshold frenzy (`Bloodied and frenzied, the cur lunges for 4 damage! / It snaps again before you can recover -- 5 more damage!`), and a poison DoT that persists in the save (`A venomous chill spreads through your veins. You are poisoned!`). The weakness is expressed through the equipment system rather than as a key: ordinary steel is resisted, the Brand's resistance flips to a multiplier (`Your Sunfire Brand flares bright -- a searing strike for 18 damage!`). `drop` of an equipped weapon narrates the unequip (`You lower your weapon and let it fall -- you're unarmed now.`) and `status` falls back to `weapon: None (bare fists)`. Blemishes: **double article** on the boss's two most important lines — `A The Ashen King blocks your way!` and `The The Ashen King was too much for you.`; `status` leaks internal ids — `Defeated:  ravenous_cur, hollow_sentinel, cave_widow`; the phase-2 line claims `His wounds knit shut with ember and smoke` but HP does not increase (24→8 after a 16 hit); the crypt is called "below" and reached by climbing **north** up the stair. No tests, no packaging config.

**ATTRIBUTION.** *model-innate:* world, prose, double-article bug, both EOF tracebacks, the README map error, id leak, the below/north geography slip. *interaction:* **none observed** — no shipped save files, no stub tooling, no counterfeit lint; but equally no test harness of any kind, which is a handicap on B9. *framework-coupled:* project layout only (excluded).

---

## FLIGHT VERDICT — TEN FORCED CHOICES

### PANEL A — DELIVERY

| axis | winner | justification |
|---|---|---|
| **A1 working surface** | **B** | Both run their whole advertised vocabulary, but B's surfaces do the *complete* thing — `status` adds `Fighting: Hollow Sentinel (27/32 HP)`, `drop` unequips, `inventory` stacks `Healing Draught x3`, `equip` prints the resulting stat — while A has three misbehaving surfaces (`look <arg>` misroutes to examine, bare `talk to` auto-targets, `equip` of the worn item says `You don't have that`) plus a `load` that quietly heals wounded monsters. |
| **A2 state integrity** | **B** | B round-trips per-room monster HP, guard state, boss phase and poison turns and restored the Sentinel at exactly `27/32`; A's save has no monster-HP field at all, so reloading restored a 13-HP wolf to a full 18 — loading rewrites the world, which is precisely what this axis asks. |
| **A3 robustness** | **A** | A produced **zero tracebacks across the entire battery**, including EOF at every prompt and a deliberately corrupted save; B crashes with an uncaught `EOFError` at the title prompt (rc=1) and again at the quit confirmation — traceback beats clean refusal only in the wrong direction. |
| **A4 delivered scope** | **A** | Absolutely more landed: 11 rooms vs 9, 8 items vs 7, 3 NPCs vs 2, plus a genuine 6-test suite that drives the public command path and a working `pyproject`/ruff/requirements setup — B ships a deeper engine but no tests and no packaging at all. Closest call on this panel. |

### PANEL B — CHARACTER

| axis | winner | justification |
|---|---|---|
| **B5 ambition** | **B** | Judged on the design attempted: B reached for a status-effect layer (save-persistent poison), turn-indexed and HP-threshold monster scripts, and a boss whose *resistance* flips into the weakness so the special item is a weapon you must still fight with — where A's sigil, however cleverly given three phase-dependent code paths, is designed to end the encounter rather than shape it. |
| **B6 imagination** | **B** | Both commit hard to one element (A's water, B's ash) and both write well, but B's characters have interiority A's do not — Maren's *"He burned this shrine, and everyone in it. Myself included."* against Maren-the-Elder's near-walkthrough *"Find the sun sigil in the mausoleum"* — and B's central idea, a tyrant whose death reads as a mercy (*"almost human again -- almost grateful"*), is the non-obvious one. |
| **B7 experience (felt play)** | **B** | B's combat is tense and legible — HP every round, poison ticking, a boss I genuinely lost at 8/70 and won by two turns — while A's central encounter, once you hold the sigil, resolves in a single command, and A never shows enemy HP unless you think to type `look` mid-fight. Neither artifact forced me to consult the tree to progress: both accept every name they print, and B actively volunteers its token (`try: talk to sister`), so no source-knowledge penalty applies to either. |
| **B8 craft (surface)** | **B** | B has more polish and more blemishes; A has less of both. B's sectioned help, boxed status, title banner, marked inventory and specific in-voice errors beat A's plain equivalents, and B's article bug fires twice a playthrough against A's, which is baked into the monster name and fires on *every single combat line* (`You hit A Cave Wolf for 9 damage`). |
| **B9 workability** | **A** | Decided by the probe plus what surrounds it: A's edit was one data file with zero code touched, its engine returns `List[str]` instead of printing — which is *why* it is testable — and it ships 6 real passing tests pinning design invariants; B's modification was equally clean but its print-directly design makes it untestable without stdout capture, and it ships no safety net at all. |
| **B10 documentation** | **B** | A's README is accurate but thin, never mentions the test suite it ships, and never declares pytest; B's documents the monster behaviours, the boss design, the exact save contract and every module's purpose, all of which I verified true in play. Its ASCII map is genuinely wrong — it hangs the Courtyard off the Armory and reverses the east/west branches — and that is the finding against it, but a stranger could run *and extend* B's game from its README and only run A's. |

---

## TALLIES

**Delivery: 2–2 (A: A3, A4 · B: A1, A2)**
**Character: B 5–1 (A: B9)**

## OVERALL: **B**

**NO PANEL SPLIT.** The panels do not disagree on direction — Panel A has no direction at all, splitting 2–2 down a clean fault line: A is the more *defensive* build (nothing crashes, ever; more rooms; a real test suite) and B is the more *complete* one (every surface does the fuller thing; the save captures the live world). Panel B then goes to B 5–1. A's single Character win, B9, is the one place its infrastructure investment converts into character rather than scope.

**Not flagged CLOSE.** A 2–2 delivery tie against a 5–1 character result is a decision, not noise. Worth recording for the operator, though: A4 and A1 were the two closest calls in the flight, and A's clean-sweep on the EOF battery is the strongest single fact in its record — B's two tracebacks are a five-line `try/except` from being absent, and if they were fixed, Panel A would read B 3–1 and the overall would widen rather than narrow.

**Neither artifact was stopped by a seam bug**, and neither has a disconnected component or an unplaced entity — the first flight in this campaign where both sides pass the placement audit outright. A: 11/11 rooms reachable, 8/8 items, 4/4 monsters, 3/3 NPCs placed. B: 9/9 rooms reachable with every exit reciprocal, 6/7 items placed plus the starting dagger by design, 4/4 monsters, 2/2 NPCs placed.
