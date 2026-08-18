# Tier judgements — the archived record

Every blind-panel judgement the campaign has produced, moved here out of the
`llmvp/configs/*.yaml` headers on **2026-08-05**.

## Why this file exists

The judgement history had grown to **2,607 lines across 19 configs — 47.5% of
all config bytes** — and it is append-only by nature: a config accumulates a
record per epoch per league forever, while the thing a reader opens a config
*for* (quant, context, sampling, serving knobs) stayed the same size. Three
scored epochs in, the record was burying the configuration.

Judgements are also **cross-model by nature**. A placement means nothing on its
own — it is a statement about where one artifact sits relative to an anchor and
to the rest of the field. Splitting that across 19 files made the one view that
matters, the field at a glance, impossible to read.

## What stayed behind in the configs

Each config keeps a three-line stub:

```yaml
tier:
  league: contemplator   # LOAD-BEARING — read by agent/tier/runner.py:config_league
  status: recorded       # judgement history: llmvp/configs/archive/TIER_JUDGEMENTS.md
```

`tier.league` is **not documentation**. `config_league()` reads it and silently
falls back to `"grinder"` when it is missing, so deleting the key would have
demoted all 13 contemplators and hy3's `both` without any error — the exact
silent-default trap the `tier extend` verdict ladder was built to avoid
(rung 1 checks `no_config` *before* `grinder` for the same reason).

`tier.status` stays because it is the at-a-glance "has this been judged" flag
the scheduler and the batch composer both want without opening this file.

## What this file does not replace

* `dev/blind_panel/LADDER.md` — the live placement table. **That is the source
  of truth for where a model currently sits.** This file is the evidence behind
  it, and the history the ladder overwrites.
* `dev/blind_panel/METHODS.md` — how a flight is run and recorded (§8: a verdict
  living only in chat is lost).
* `dev/blind_panel/TIER_RUBRIC*.md` / `CHALLENGE_v2_CHECKLIST.md` — the
  instruments. Note that scores are **not comparable across rubric versions**;
  v1.x scored a solo `/100` across ten weighted dimensions, v2.x makes ten
  forced pairwise choices across two panels that are **tallied separately and
  never summed**.

## Two consumers this move affected

**`dev/blind_panel/dim_variance.py`** walks `doc["tier"]` in each config looking
for v1.x `dimensions:` score vectors, and will now find none. It is an
instrument for a **retired rubric** — v2.1 produces no dimension vectors — so it
is left as-is rather than repointed. If a v1.x variance question ever comes
back, point its `collect()` at the YAML fences in this file.

**`llmvp/tests/test_config_inheritance.py::test_judged_and_observed_stay_separate`**
sweeps every config for a `judged` blob and asserts the TIER_RUBRIC §7 invariant
on each — that the operator half (`observed`/`attempted`) is a *sibling* of
`judged` and never nested inside it, since that nesting is how run telemetry
would leak into a judge packet. It carries a real anti-vacuity guard
(`assert scored`), so it fails loudly rather than silently passing on an empty
sweep — but after this move **exactly one specimen is left**:
`llmvp/configs/archive/laguna-xs-2.1-apex.yaml`.

Two consequences worth knowing before you touch that file:

* Archiving or deleting it turns the test red. That is the guard working, not a
  bug — but the fix is to re-point the sweep, not to delete the assert.
* The §7 sibling invariant is **not enforced on anything in this file**. The
  YAML fences below are documentation, not loaded config. If §7 compliance
  matters going forward, the check belongs in whatever writes new sections here.

---

# EPOCH v2.0 · THE GUARDIAN PLACEMENT BATCH — 2026-08-05

The largest single judging batch of the campaign, and the one that called the
anchor into question.

**Design.** 13 contenders, each flown **against the same Guardian anchor**, one
freshly-spawned blind judge per flight, `FLIGHT_PROMPT.md` sent verbatim with
only `{PACKET_ROOT}` filled.

| | |
|---|---|
| batch root | `~/ouroboros-runs/flights_v21_20260805/` |
| anchor | `dev/blind_panel/anchors/v2.0/guardian-gptoss-20260803/alpha` |
| rubric | TIER_RUBRIC **v2.1** (10 axes, 2 panels, tallied separately) |
| checklist | `CHALLENGE_v2_CHECKLIST.md`, 47 items |
| judge model | `claude-opus-5`, 1 per flight |
| staging | `dev/blind_panel/stage.py`, keys outside every packet root |
| leaks | **0 of 13** |
| position balance | contender on A ×5, on B ×8 |

## Results

Panel tallies are **Delivery / Character**, contender first, never summed.

| # | contender | side | Delivery | Character | conf. | own game | flag |
|---|---|---|---|---|---|---|---|
| 1 | **qwen3.5-122b-a10** | A | **4–0** | **6–0** | **47/47** | **WON** | — |
| 2 | **qwen3.6-27b** | B | **4–0** | **6–0** | **47/47** | **WON** | — |
| 3 | **step37-flash-196b-a11** | B | **4–0** | **5–1** | 46/47 | **WON** ×2 | — |
| 4 | **deepseek-v4-flash** | B | **4–0** | **6–0** | 45/47 | **WON** | — |
| 5 | **gemma-4-31b** | A | **3–1** | **4–2** | 42/47 | unwinnable — 1 edge | 3 coin-flips |
| 6 | **gpt-oss-120b-a5-swarm-524k** | B | **2–2** | **4–2** | 42/47 | — | CLOSE |
| 7 | **qwen3.6-35b-a3** | B | 1–3 | **5–1** | 45/47 | unwinnable — 3 orphans | **SPLIT + CLOSE — HALTED** |
| 8 | qwen3-next-coder-80b-a3 | B | 1–3 | 4–2 | 43/47 | — | **SPLIT — HALTED** |
| 9 | hy3-reap-200b-a21[g] | A | 1–3 | 2–4 | 38/47 | — | CLOSE |
| 10 | mistral-medium-3.5-128b | A | 0–4 | 2–4 | 41/47 | — | — |
| 11 | laguna-xs-2.1 | A | 0–4 | 2–4 | 42/47 | — | — |
| 12 | gemma-4-26b-a4b | B | 0–4 | 2–4 | 37/47 | — | — |
| 13 | glm-4.7-flash | B | 0–4 | 1–5 | 30/47 | — | — |

**Seven of thirteen beat the Guardian.**

## Source arms

| contender | source run | slot |
|---|---|---|
| deepseek-v4-flash | `tier_20260804-141701` | arm04 |
| gemma-4-26b-a4b | `tier_20260805-071313` | arm01 |
| gemma-4-31b | `tier_20260803-200050` | arm02 |
| glm-4.7-flash | `tier_20260803-200050` | arm03 |
| gpt-oss-120b-a5-swarm-524k | `tier_20260804-141701` | arm03 |
| hy3-reap-200b-a21[g] | `tier_20260803-100554` | arm01 |
| laguna-xs-2.1 | `tier_20260803-200050` | arm06 |
| mistral-medium-3.5-128b | `tier_20260803-200050` | arm04 |
| qwen3-next-coder-80b-a3 | `tier_20260805-092309` | arm01 |
| qwen3.5-122b-a10 | `tier_20260803-200050` | arm07 |
| qwen3.6-27b | `tier_20260803-200050` | arm05 |
| qwen3.6-35b-a3 | `tier_20260803-200050` | arm01 |
| step37-flash-196b-a11 | `tier_20260804-141701` | arm05 |

---

## THREE FINDINGS FROM THE BATCH

### 1. The anchor is no longer a tier-1 bar — RULING OWED

A 7/13 pass rate is a midpoint, not a ceiling. Every placement in this batch is
measured against the Guardian, so **no result here is written into LADDER.md
until the operator rules on whether the anchor stands**. This is the decision
gating all the others.

The Guardian's decisive defect is binary and was found independently by all 13
judges: `grep -i 'congratul|victor|you win|triumph'` across its entire tree
returns nothing. Killing its boss executes the same generic `collapses!` branch
every goblin does. It is **UNWINNABLE with no win state authored anywhere**, not
merely unbeaten.

### 2. The 47-item checklist is a strong instrument

Thirteen independent blind judges scored the same Guardian tree.

* **12 of 13 returned exactly 41/47 with the identical unmet set
  {22, 23, 31, 33, 38, 45}** and the same core-loop trigger.
* The lone outlier (the mistral-medium flight) returned 42/47, differing by the
  single item **45** — branching NPC dialogue, which sits exactly on the v2.1
  staged-progression boundary the operator ruled presence-lenient on 08-03. The
  Guardian's NPCs have one fixed line each and `triggers: {}`.

That is tighter agreement than the Fleiss κ=0.79 measured for categorical
labelling, and it argues the checklist is doing real objective work underneath
the subjective axes. **Item 45 is the instrument's one soft edge** and is worth
tightening before the next epoch.

### 3. The epoch's signature failure is a missing graph edge

Two of the seven winners are **UNWINNABLE with fully authored victory
machinery**, blocked by room-graph connectivity in a world data file:

* **gemma-4-31b** — `secret_alcove` holds the boss-weakness item (the Ancient
  Seal) behind an exit that only points *outward* (`east: library`); no room
  links in. The judge added that one line in a scratch copy and the whole chain
  fired: `The Ancient Seal glows! You hit the Ancient Dragon for 45 damage.` →
  `YOU WIN!`
* **qwen3.6-35b-a3** — the authored room graph has **two disconnected
  components**. Reachable: `{entrance, library, cave, garden, forest}`. Orphaned:
  `{tower, dungeon, throne_room}` — which hold the boss, two of three regular
  monsters, the Iron Sword and the Chain Mail. Both win conditions are behind
  the gap.

This is the same seam-bug family as `shadow_lord`/`shadow_lich`, but at the
**graph** level rather than the identifier level. It is now the highest-value
target for a cheap pre-delivery check: a reachability traversal from the start
room over the authored world file would have caught both, and neither is
detectable by any lint, import or syntax gate we run.

---

## PER-FLIGHT RECORD

### 1. qwen3.5-122b-a10 — WON 4–0 / 6–0 · 47/47 NEAR-FULL · game WON

*Shadows of Malakor.* Ten rooms, town square → market → forest → mine →
underground lake → obsidian temple. **Reproducibly winnable: 11 victories in 24
scripted runs** of one optimised line, finishing at 5/100 HP.

The weakness mechanic is a **two-step discovery and genuinely activatable** —
the silver dagger cannot be equipped, but `use`-ing it mid-fight roughly doubles
damage and persists (`21` → `48` per hit). Two-phase boss that heals past its own
maximum (`180/180` from a 120 max).

Defects, all model-innate: the startup auto-load branch is **dead code** (both
branches of the choice are identical; `get_world_data_for_engine(saved_state)`
ignores its argument), STATUS reports base stats while combat correctly applies
equipment bonuses, phase logic leaks to ordinary monsters (`Brutal Ogre enters
PHASE TWO!`), terminal states print but do not stop the loop, and the Ancient Key
gates nothing. The in-game `load` command, by contrast, round-trips completely
including defeated monsters and NPC stage.

Charged as *interaction*: ships `ruff.toml` and `[tool.pytest.ini_options]
testpaths = ["tests"]` with **no tests anywhere in the tree**.

### 2. qwen3.6-27b — WON 4–0 / 6–0 · 47/47 NEAR-FULL · game WON

Nine rooms to Lord Malachar's Abyssal Throne. **A 27B dense model with a perfect
conformance score.** Two scholars point at a Sunstone across four staged topics
each, with a real `conditions: {has_item, heard_hint}` schema.

The most designed boss in the field: **two independent phase triggers** —
attrition *or* the Sunstone, which skips remaining phase-1 HP and drops the
boss's attack 8 → 5. The weakness item is an **advantage, not a lock**. Armour
genuinely mitigates (7 unarmoured → 4 with Defense 4). Mid-combat healing works
and costs a turn. Dialogue progression survives a save/load round trip.

Defects: the save has **no room-contents field**, so room contents are derived as
authored-minus-inventory — taking persists, dropping does not, and a dropped key
teleports back to its authored home. Rooms hold at most one item. And a real
workability trap found by the modification probe: **non-boss monster stats and
attack names in the data file are dead data** — `combat.py:22-24` hardcodes
`return 3, "Swift Bite"` by monster id, so editing `rat_swarm` in YAML does
nothing.

### 3. step37-flash-196b-a11 — WON 4–0 / 5–1 · 46/47 NEAR-FULL · game WON ×2

*DUNGEON OF THE FIRE LICH.* Eight rooms. Won **twice** — with the torch (four
hits at 16) and bare-handed (ten hits at 5), establishing the weakness as an
optimisation rather than a gate. Two NPCs on opposite sides of the map
independently point at fire. Unmet: **35** (save omits room state).

Combat is a proper state machine rather than a nested loop — `help`, `status`,
`look` and `examine` are free actions, `use` costs a turn, everything else is
refused with the list of legal moves.

The dominant defect is **naming**: it prints human names and accepts only
internal IDs. `take Iron Sword` fails, `take sword` works; `take armor` fails,
`take leather_armor` works; `talk to Old Guard` fails, `talk to guard` works
(`parser.py:18` takes `parts[2]`, so any two-word NPC name breaks). Six verbs
affected. `quit` prints the raw sentinel `QUIT` instead of a farewell.

**This is the only flight where the Guardian took an axis on Panel B** — B8
craft, on input tolerance, which is exactly the surface-craft signal that axis
was split off to catch.

### 4. deepseek-v4-flash — WON 4–0 / 6–0 · 45/47 NEAR-FULL · game WON

Swept both panels and won its game. The judge flagged **B6** as the single axis a
second judge might reasonably flip.

### 5. gemma-4-31b — WON 3–1 / 4–2 · 42/47 NEAR-FULL · UNWINNABLE by one edge

*The Curse of the Ancient Seal.* See finding 3 — the win path is complete and
blocked by a single missing exit link. Unmet: **16, 26, 28, 38, 39**.

Other defects: `save_game`/`load_game` are **fully authored in `persistence.py`
and dispatched in `engine.py`, but `parser.py` never emits those verbs** — both
are dead code and no save file is ever written. `restart` **half-resets**: it
rebuilds the engine over the same mutated world object, so items already taken
are destroyed permanently. The `behavior` field (`aggressive`/`defensive`/
`erratic`) is authored in `world.json` and never read. EOF tracebacks; running
from another cwd tracebacks on a hardcoded relative `world.json`.

The judge **declined to flag CLOSE** — seven of ten axes, both panels
independently, a binary conformance advantage rather than a vote — but named
**A3, A4 and B7** as the near-coin-flips. Treat as the shakiest of the seven wins.

### 6. gpt-oss-120b-a5-swarm-524k — WON 2–2 / 4–2 · 42/47 NEAR-FULL · CLOSE

**Delivery was a 2–2 tie.** The narrowest win in the batch and a second-judge
candidate on that basis alone.

### 7. qwen3.6-35b-a3 — PANEL SPLIT · overall contender · 45/47 NEAR-FULL — HALTED

*THE DARK THRONE.* Delivery went to the **Guardian 3–1**, Character to the
contender **5–1**. Unmet: **22, 38**. Judge flagged CLOSE and escalated under
§6(a) and §6(d). **Placement halted pending operator ruling.**

Unwinnable via the three orphaned rooms (finding 3). Everything behind the gap is
authored, coherent and functional — three regular monsters with genuinely distinct
behaviours (`double_strike`, `high_damage_low_accuracy`, `flee_at_low_hp`), a real
two-phase boss, a victory screen, a defeat screen and a restart.

The state-integrity findings are the worst in the batch:

* **`_load` lies.** It reads the save, discards it, and prints `Game loaded
  successfully!` — the player is back at the entrance with a fresh inventory.
* **The startup load path crashes the interpreter.** `main.py:21` builds
  `GameEngine(world, None, 'save.json')` to borrow `_deserialize_state`, and
  `__init__` immediately dereferences `player.location_id` →
  `AttributeError: 'NoneType' object has no attribute 'location_id'`.
  `_deserialize_state` itself is complete and correct, and never successfully
  called.
* **`_restart` is correct but wired outside the loop** (`engine.py:58` sits
  outside `while not self.game_over`), so it rebuilds the world and exits.
* **Monster HP resets on flee** — combat progress can never be banked, silently.
* **`quit` at 50/50 health prints the DEFEAT screen** — `run()` sets `game_over`
  on quit and line 53 cannot tell quitting from dying.

Plus a discoverability trap: the room shows an NPC's *description* but never its
name, and `talk to` requires the exact name **including the article** — only
`talk to the librarian` works, and the sole place that name is surfaced is
`examine the librarian`.

The judge's own framing of the split is worth keeping: *"B is four small fixes
from a finished game while A is missing its ending outright."*

### 8. qwen3-next-coder-80b-a3 — PANEL SPLIT · lost · 43/47 — HALTED

Delivery 1–3, Character 4–2. CLOSE. **Placement halted.** This arm was selected
as the best of three same-model runs by the single-model variability flight of
the same date (see that config's archived record).

### 9. hy3-reap-200b-a21[g] — lost 1–3 / 2–4 · 38/47 NO-TERMINAL-STATES · CLOSE

**This is the GRINDER arm** and it closes the "hy3[g] grinder arm still owed"
item standing in the config since 08-02. It does **not** displace hy3's
contemplator placement (tier 1, `tier_20260802-224028` arm02, 47/47, played to
victory) — under `league: both` these are two separate measurements, and the gap
between them is precisely the league-difference datum `both` exists to capture.

### 10. mistral-medium-3.5-128b — lost 0–4 / 2–4 · 41/47 NEAR-FULL

**A conformance inversion, and the sharper of the two.** It scored NEAR-FULL — a
*better binary verdict* than the Guardian's SIGNIFICANTLY-DEVIATED — and is
**unplayable from room one**: `world.py` imports `Direction` from `models` then
re-defines its own, so every room's exits are keyed to a different enum than the
parser emits. `go north` fails in every room, in every direction, forever. Behind
that, `GameEngine.run()` yields twice per loop, so **every second command is
silently discarded**.

It authored ten rooms, monster-gated movement, a boss-readiness gate, a
`check_weakness` subsystem and a lore tome that names the weakness independently
of the weakness item. It won ambition and imagination and reached room one of ten.

**This is the flight that produced the lone Guardian convergence outlier** (42/47,
omitting item 45).

### 11. laguna-xs-2.1 — lost 0–4 / 2–4 · 42/47

The other conformance inversion: **higher conformance than the Guardian, and it
accepts zero commands** — `TypeError` on every input including empty. Dead on
arrival.

### 12. gemma-4-26b-a4b — lost 0–4 / 2–4 · 37/47 (count trigger)

Modification probe **tied**. A single-token parser kills all dialogue, and two
exits orphan the boss and the weakness item — a third instance of the
finding-3 graph failure, here severe enough to also fail on count.

### 13. glm-4.7-flash — lost 0–4 / 1–5 · 30/47 · BOTH triggers

The floor of the batch. **Six brief verbs never written**, `combat.py` orphaned,
and every error path an uncaught traceback. Floor-assessment candidate.

---

## DECISIONS OWED (as of 2026-08-05)

1. **The anchor ruling** — gates everything below.
2. **Second judge, protocol-mandated** (METHODS §6, placement halted):
   `qwen3-next-coder-80b-a3`, `qwen3.6-35b-a3`.
3. **Second judge, warranted**: `gpt-oss-120b-a5-swarm-524k` (Delivery tied 2–2),
   `gemma-4-31b` (three named coin-flips), `hy3-reap-200b-a21[g]`.
4. **Frontier face-off**: the four clean game-winners — `qwen3.5-122b-a10`,
   `qwen3.6-27b`, `step37-flash-196b-a11`, `deepseek-v4-flash`.
5. **Floor assessment**: `glm-4.7-flash` (30/47, both triggers),
   `gemma-4-26b-a4b` (37/47).

*Items 4 and 5 were executed the same day — see the next section. Items 1–3
remain open.*

---

# EPOCH v2.0 · FRONTIER AND FLOOR FLIGHTS — 2026-08-05

Same day, same rubric, same batch root pattern
(`~/ouroboros-runs/frontier_v21_20260805/`, `~/ouroboros-runs/floor_v21_20260805/`),
one blind `claude-opus-5` judge per flight, contender seating stratified A×3 / B×2,
zero identifier leaks.

## FRONTIER — out-of-band scorecard, never moves the ladder

Per METHODS §5 the family-bias caveat is stamped: our judges are Opus and the
Frontier artifact ("The Ashen Keep") is Claude-authored.

| contender | Delivery | Character | axes taken off Sonnet 5 |
|---|---|---|---|
| qwen3.6-27b | 1–3 | 0–6 | **A3 robustness** |
| qwen3.5-122b-a10 | 0–4 | 1–5 | **B5 ambition** |
| deepseek-v4-flash | 0–4 | 1–5 | **B9 workability** |
| step37-flash-196b-a11 | 0–4 | 0–6 | — |
| gpt-oss-120b-a5-swarm-524k | 0–4 | 0–6 | — |

**Five flown, five losses, three axes taken of fifty.** The headline is not the
sweep — it is that three separate local models, on a **30-cycle budget**, each
took a genuine axis off a frontier model. qwen3.6-27b's A3 is the strongest:
**zero tracebacks anywhere**, including clean EOF at every level, against the
Frontier's own `EOFError` at its title screen. Sonnet is not clean either —
doubled articles (`A The Ashen King`), raw ids in the status block, and a
phase-2 line claiming *"His wounds knit shut with ember and smoke"* while HP
does not rise.

**What the Frontier surfaced that the Guardian could not.** Every artifact that
scored 45–47/47 against the Guardian gave up a major structural defect against
an opponent that works:

* **qwen3.5-122b** — `boss_phase` is a **single global int shared by every
  fight**. The first monster you damage consumes it. Beeline to the boss and
  phase two fires (heals 120 → 180/180, provably unwinnable); fight anything
  first and phase two never happens (winnable). Its headline mechanic and its
  win condition cannot both exist in one playthrough. Also: armour is inert
  (`get_armor_bonus()` returns `0` under a comment saying the engine resolves
  it — the engine resolves only the weapon), and `flags["game_lost"]` is set
  and never read, so every `attack` after death reprints DEFEAT forever.
  **Design-intent check (operator question, resolved):** NOT an authored
  difficulty gate. `phase_two_threshold` is a per-enemy field the gate never
  reads; `boss_phase` lives on `GameState` rather than `CombatState`; only the
  boss has `phase_two_stats`; the README says "Two-phase final boss", singular.
  A one-line scope change gives either the documented design or the emergent one.
* **qwen3.6-27b** — phase 2 is a **de-escalation** (attack 5 against phase 1's
  8; *"shifts into a more vicious form!"* is followed by *"Weakened Strike! You
  take 1 damage!"*). Every new game opens at **50/100 HP** (`models.py:48`
  against a max hardcoded as 100 in three places). Death auto-saves
  `"health": -5` in the boss chamber over your good save. Load destroys
  untouched items (no room state; the loader subtracts inventory *by id*).
  No `random` anywhere — combat is fixed arithmetic. Its `pyproject.toml`
  names a build backend that does not exist.
* **deepseek-v4-flash** — `crypt` declares an exit to `courtyard`; `courtyard`
  declares nothing back. `sage` is authored with a full dialogue tree and
  placed in no room. Orphaned: the 8th room, 3rd monster, 5th item, 2nd NPC.
* **gpt-oss-swarm** — `_boss_defeated()` calls `.get("is_boss")` on the
  `Monster` **dataclass**, so **defeating any monster crashes the process**
  before `display_victory()` can run. The real final boss's room has no
  inbound edge. The Sun Relic is defined and placed in no room, and no monster
  carries a `weakness` field at all. Plus a duplicate unused 71-line combat
  implementation, a `__globals__` reach-in across modules, and the whole world
  re-parsed on every `status` — the swarm's parallel-authoring signature.
* **step37** — displays `Iron Sword`, accepts only `sword`. **4 of 5 items and
  1 of 2 NPCs cannot be addressed by the names the game prints.** Replicated
  independently by both its Guardian and Frontier judges. Winnable bare-fisted,
  and monster HP resets on room re-entry, so the boss is farmable.

**Instrument note worth acting on.** step37's judge wrote: *"A seam bug is not
what stopped me — I got past it by reading `data/world.yaml` to learn the keys,
**which a player cannot do**."* Every judge has source access, which makes the
instrument systematically lenient on the display-name/key mismatch class.

## FLOOR — `gemma-4-26b-a4b` vs `floor-devstral-20260803`

**LOST — Delivery 0–4, Character 2–4. It places BELOW the floor anchor**, and
below it on the binary verdict too: **36/47 with BOTH triggers** (23.4% count
*and* core loop) against devstral's 39/47 with one. A clean measurement, not a
fallback artifact — its batch turn wrote 9/9 with nothing missing.

It took **B6 imagination** ("THE CRYSTAL OF DESTINY", a geography with a shape,
item prose carrying its own hint) and **B9 workability** (real module seams
against the floor's 433-line monolith). It has the better world and the better
layout, and it cannot be played:

* `parser.py` sets `target = parts[1]` — **one token** — while every handler
  compares full underscore-normalised names. The room prints `You see: Rusty
  Sword`; `take Rusty Sword` → *"That isn't here."*; `take rusty_sword` works.
* Five bare verbs (`take`, `equip`, `talk`, `drop`, `use`) kill the process.
* `help` advertises `inventory`, `save`, `load`; none is dispatched.
* Armour and boss-weakness are both literal `pass` statements, each file
  believing the other implements it.
* **Unwinnable twice over** — the room graph orphans `boss_chamber` and the
  Holy Water alcove, and even with both edges restored the boss is
  arithmetically unbeatable, because combat resolves the entire fight inside
  one `attack` command with no player input.

**The finding that carries beyond this flight:** both artifacts were stopped by
the *same* seam — a boss component with no inbound edge — and in both, a second
independent seam killed the NPC dialogue carrying the boss-weakness hint
(gemma: the one-token parser; devstral: a `continue` escaping the wrong loop on
a `condition: none` sentinel). *Neither model ever met its own NPC.*

## ROOM-GRAPH ORPHANING — the epoch's dominant decisive defect

Six artifacts, five different models, **plus our own floor anchor**:

| artifact | orphaned |
|---|---|
| gemma-4-31b | `secret_alcove` holding the weakness item; one outward-only exit |
| qwen3.6-35b-a3 | `tower`, `dungeon`, `throne_room` — boss, 2 monsters, 2 weapons |
| deepseek-v4-flash | one-way `crypt`, plus an NPC placed in no room |
| gemma-4-26b-a4b | `boss_chamber` + `secret_alcove` (Holy Water) |
| gpt-oss-swarm | `throne` (real final boss); weakness item in no room |
| **floor-devstral (anchor)** | 9 rooms authored, **4 reachable**, five components |

Every instance is invisible to lint, import and syntax gates. A reachability
traversal from the start room, plus an item/NPC placement check, would have
caught all six.

---

# EPOCH v2.0 · FRONTIER FLIGHT — `gpt-oss-120b-a5-medium` — 2026-08-10

One blind `claude-opus-5` judge, TIER_RUBRIC v2.1, contender seated **B** (the
08-05 batch seated contenders A×3/B×2 and hy3 sat B; alternating is the
position-bias check). Packet `/private/tmp/flight_gptoss_frontier_20260810`,
opponent `anchors/v2.0/frontier-sonnet-20260803`. Identifier scan clean. Family-
bias caveat stamped per METHODS §5.

## Scorecard — OUT-OF-BAND, never moves the ladder

| contender | Delivery | Character | axes taken off Sonnet 5 |
|---|---|---|---|
| **gpt-oss-120b-a5-medium** | 0–4 | **1–5** | **B5 ambition** |

Completability **WON** (twice — the intended one-shot shard route and the long
sword grind). Conformance **46/47**, sole unmet **#33** (no restart after
defeat — absent from the tree, not merely unreachable). No panel split, no
CLOSE flag. Room graph 8/8 reachable, no unplaced entity.

## Why this is a datapoint and not a curiosity

**B5 is the axis this lineage owns.** In the 08-02 Guardian flight hy3 beat the
Guardian 4–0 / 5–1 and **the Guardian — a gpt-oss artifact — took only B5
ambition** (LADDER.md:174). This run takes the same single axis against a
*harder* opponent: the Frontier one-shot rather than the Guardian. Same axis,
opponent raised. The judge called B5 "the closest axis on the card" and named
what earned it: monsters that migrate through the room graph unprompted, a
key-locked exit, an NPC barter, and the entire world authored as external data
— reach ACROSS the system, where the Frontier's ambition is depth inside one
combat routine.

**Cost, corrected.** The 20h wall span is misleading: **5.07h ACTIVE across 14
trace segments, 14.97h paused** — 50.9 cycles/h, a grinder rate. Prior frontier
contenders flew on a 30-cycle contemplator budget; this is 258 cycles at
grinder pace, which is the like-for-like caveat on any comparison to the 08-05
table.

## Decisive defect — the predicted seam class, again

`handle_save` (game.py:415) serialises four world tables — `rooms`, `items`,
`monsters`, `npcs`. `handle_load` (game.py:446) reads **none of them**. It
restores the player onto a world rebuilt fresh from `world.yaml`, so loading
resurrects killed monsters, respawns looted gear, and duplicates every item
without limit. The save FILE is correct; the reader ignores it. The artifact's
own shipped tests know — three are named `test_save_includes_world_data` and
all three fail.

Second seam, and the worst robustness result: `go up` — an ordinary player's
typo — terminates the process. `parse_command` passes any post-`go` token into
`Direction(...)`, which raises, under a blanket `except Exception: … break`
(game.py:720). The same construct turns a corrupt save into a session kill.

## Two UX findings the rubric's B7 rule caught

`give` and `wait` are both absent from `help`. The judge only reached the
Alchemist barter and the Cursed Specter's teleport by reading the parser —
so a player working from the help screen can complete neither. Charged at full
weight against the artifact per the "judge the player's experience, not yours"
rule, and it is the clearest case that rule has produced.

## Provenance caveat — READ BEFORE COMPARING

This artifact was built across **ten framework builds landed mid-flight**
(ded6716, 160e0b9, d94be65, d7a6301, eed5781, 6651755, b3d90d7, fad46ed and
predecessors). It is an excellent debugging record and a **weak clean-capability
datapoint**. Its terminal state was reached only after four operator-directed
repair rounds on a manifest the framework had itself corrupted.

## What the flight found that our own gates did not

`pyproject.toml` declares `text-adventure = "main:main"`; `main.py` defines
`run_game` and no `main`. The documented `pip install .` path cannot produce a
working game. **Our manifest coherence checker passed this file as clean** — it
validates PEP 621 structure and PEP 508 requirement contents and never checks
that a `[project.scripts]` target resolves. Same class as the two defects fixed
the same day: parsing is not the bar, shape is not the bar, and declaring is
not the bar either — the target has to exist.

---

# ARCHIVED PER-MODEL RECORDS

Everything below is the verbatim `tier:` block lifted from each config on
2026-08-05, in the YAML it was written in. Blocks predate the batch above unless
stated. **Scores are not comparable across rubric versions** — check the
`rubric:` key in each block before comparing any two numbers.

## `deepseek-v4-flash.yaml`

```yaml
tier:
  # ── LEAGUE: CONTEMPLATOR (placed 2026-08-03 from measured throughput) ─
  # Decode measured at 4.2-5.4 tok/s across six probes — the SLOWEST in the
  # fleet by a wide margin (hy3 ~20, laguna ~30). Prefill is fast (1,800
  # tokens evaluated in 0.5-1.0s, so ~2-3k tok/s); the cost is entirely in
  # decode: 104 GB of weights plus i-quant dequant kernels on an
  # architecture llama.cpp has no optimised Metal path for yet.
  # Rough projection from hy3's 31.7 cyc/h at ~20 tok/s: ~8 cyc/h here, so
  # the 30-cycle contemplator budget lands near 3.5-4h — INSIDE the 4h
  # safety wall but not by much. If the first run trips that wall instead
  # of the cycle cap, the wall is the finding, not the model.
  # ✅ RESOLVED 2026-08-03 — THE REBUILD FIXED IT, 4.2x DECODE.
  # llama-cpp-python rebuilt to JamePeng 9af4ec3 / llama.cpp b10243:
  #     graph splits  44 -> 2      (one per layer -> none)
  #     fused ops     4 disabled -> all 4 ENABLED
  #     decode        4.2-5.4 -> 21.2 tok/s   (4.2x)
  # gpt-oss regression check held at 48-49.6 tok/s (baseline 48.7-50.7),
  # splits 2 before and after; llmvp suite 854 green on the new binding.
  #
  # ── the diagnosis, kept because the METHOD generalises ──────────
  # Our llama.cpp (b10131, built Jul 26) has NO METAL KERNELS for
  # DeepSeek-V4's fused ops. The boot log says so directly:
  #     resolve_fused_ops: Lightning Indexer not supported, set to disabled
  #     resolve_fused_ops: fused DeepSeek V4 HC pre/comb/post not supported
  #     sched_reserve: graph splits = 44
  # 44 splits on a 43-layer model is ONE SPLIT PER LAYER — the graph falls
  # off the GPU and round-trips activations 43x per token. Confirmed by
  # string scan: DSV4_HC symbols exist in libggml-base (op registry) and
  # libllama (graph builder) but are ABSENT from libggml-metal, and Metal
  # resolves pipelines by name at runtime.
  # Peer 128GB Macs report 20-35 tok/s on this model; one M1 Ultra report
  # went 5-6 -> 15-16 tok/s after a Metal patch. Upstream has since landed
  # SIMD-optimised DSV4_HC Metal kernels.
  # ACTION: rebuild llama-cpp-python against llama.cpp >= b10240 and
  # re-measure. Verify with: strings -a libggml-metal.dylib | grep -i dsv4
  # (must be non-empty) and check graph splits fall from 44.
  # RE-MEASURED at 21.2 tok/s: just above hy3 (~20 tok/s, 31.7 cyc/h) and
  # far above the contemplator band (step37 7.5, laguna-s-apex 19.7
  # cyc/h). hy3 is the closest neighbour AND a documented straddler, so
  # this is a boundary case, not a clear grinder. Assigned GRINDER on
  # throughput; confirm from the first run's measured cyc/h and move it if
  # this model's deep per-cycle reasoning drags the rate under ~20 cyc/h.
  # CONFIRMED CONTEMPLATOR 2026-08-04, exactly as the "move it if" clause
  # above anticipated: the first run measured 22 cycles / 132 min =
  # 10.0 cyc/h, half the ~20 cyc/h threshold. Decode speed did not predict
  # the cycle rate — 21.2 tok/s is grinder-class throughput, but this
  # model spends it on deep per-cycle reasoning rather than more cycles.
  # 30 cycles at this rate needs ~3h, inside the 4h contemplator wall.
  league: contemplator
  # CONTEMPLATOR RE-RUN LANDED 2026-08-05 (tier_20260804-141701): 29 cycles
  # / 160 min = 10.9 cyc/h — the league call confirmed a second time, and
  # inside the 4h wall. 15 files (8 py-ok), ZERO degenerations, and the
  # wave's best goal ratio at 16/19.
  # THE HARD-MODE HANDICAP IS GONE, and not because the fallback caught it:
  # the run needed ZERO unfenced recoveries and ZERO serial demotions. It
  # emitted properly fenced batches natively. So this artifact is the
  # model's real level, not a floor — supersedes the discarded arm below.
  status: not_yet_run
  # ⚠️ THE FIRST RUN IS A HARD-MODE TEST, NOT THIS MODEL'S CEILING
  # (operator, 2026-08-03; run tier_20260803-151411).
  # Its batch structural step emitted all six declared files correctly —
  # right markers, all compiling — but WITHOUT markdown fences, and the
  # slicer of the day only parsed fenced blocks. It wrote 0/6 and the run
  # was demoted to SERIAL fallback: six files rebuilt one at a time,
  # 10,220 tokens of good code discarded, and the cycles that rebuild cost
  # taken out of the same 2h wall.
  # Serial is the handicapped path by design — files are authored in
  # isolation rather than in one mutually-consistent pass, which is where
  # cross-file seam bugs are born, and it is the expensive path per file.
  # So whatever this artifact scores, read it as a FLOOR for this model,
  # not a measurement of it.
  # FIXED 2026-08-03 (bf78236): the slicer now recovers unfenced
  # marker responses (verified 6/6 on this exact response). A re-run
  # keeps its batch and should be strictly better.
  # PER THE OPERATOR'S OWN FIX-AND-DISCARD DOCTRINE (TIER_RUBRIC v2.1
  # attribution ruling: a confirmed framework fault is remedied by fixing
  # the framework and DISCARDING the run) this arm is a discard candidate.
  # Left standing pending an operator call — it is also a genuinely
  # interesting worst-case datapoint, which the doctrine does not forbid
  # keeping so long as it is not read as the model's level.
  quant: UD-IQ3_XXS (unsloth)
  # ── LIVE INTAKE, 2026-08-03 (server up, before any tier run) ───────
  # LOADS AND DECODES. n_ctx 65536, KV preflight landed on the computed
  # numbers exactly (5.8 GB KV + 104.2 GB weights = 110.0 GB), no loader
  # abort — all three header-verified preconditions held in practice.
  # COHERENT AT 2.93 BPW, including on the axis the quant analysis flagged
  # as the risk: asked for a PyYAML loader it produced correct
  # `yaml.safe_load`, correct `FileNotFoundError`, an unprompted
  # empty-file guard, and an accurate account of why safe_load is
  # preferred (arbitrary-code execution via Python-specific object tags).
  # No confabulated API names in 573 tokens.
  # THINK DIAL VERIFIED AT EVERY LEVEL, and — unlike laguna-XS — it works
  # UNDER OUR SOUL PERSONA, so the persona-conditioned trap does not
  # apply here (17x24 probe):
  #     None    1 tok, no CoT   (close-only prefill)   408 correct
  #     low     1 tok, no CoT   (close-only prefill)   408 correct
  #     medium 16 tok, 14 reasoning + 2 content        408 correct
  #     high   15 tok, 13 reasoning + 2 content        408 correct
  # CHARACTER NOTE — the no-think mode is genuinely weak, not merely
  # terse. On a two-leg average-speed problem it emitted a single token,
  # "240", which is the FIRST NUMBER IN THE PROMPT rather than an answer;
  # the same prompt at high produced 32 tokens of correct in-channel
  # arithmetic and "84". Single-step multiplication is fine at no-think;
  # anything multi-step is not. Route this model's real work at medium+.
  # STILL OWED: the BOS falsification declared in formats/deepseek4.yaml
  # (costs a 104 GB reload, so deferred — nothing observed suggests the
  # declared BOS is wrong, and coherence rules out the salad failure the
  # tekken precedent warned about).
  # ── QUANT STRATEGY, read from the tensor table (2026-08-03) ────────
  # 284.3 B params in 104.2 GB = 2.93 BITS/WEIGHT EFFECTIVE — by a wide
  # margin the most aggressive quant the fleet has ever served. The name
  # UNDERSTATES it: 37.8% of parameters are IQ2_XS (~2.3 bpw), not IQ3.
  #
  # It is Unsloth DYNAMIC per-tensor allocation, not a uniform bit depth,
  # and the split is sharp:
  #   ROUTED EXPERT FFN carries ~99% of params and ALL of the compression
  #     ffn_down_exps  IQ3_XXS x41  (+MXFP4 x2)
  #     ffn_gate/up    IQ2_XS  x25 · IQ3_XXS x17 · IQ3_S x1
  #     (down keeps 3 bits everywhere while gate/up drop to 2 on 25 of 43
  #      layers — the standard down-projection-is-more-sensitive result)
  #   EVERYTHING STRUCTURAL IS PROTECTED at 6-8 bits
  #     all attention (attn_output_a/b, attn_q_b)  Q8_0 x43 each
  #     attn_q_a, shared-expert FFN, embeddings, output   Q6_K
  #     the sparse-attention indexer + compressor gate    Q8_0
  #     ffn_gate_inp (the ROUTER)  BF16, untouched on all 43 layers
  #
  # Cheap layers cluster early and at 35-41; layers 27-34 are a contiguous
  # PROTECTED band, and 26 is special-cased entirely (MXFP4 down, IQ3_S
  # gate/up). Layer 42 (last) also protected.
  #
  # ⚠️ THE MXFP4 IS NOT A QAT FREEZE. Only 2 of 129 expert tensors are
  # MXFP4 (blk.26 and blk.42 ffn_down_exps, 1.5% of params). A published
  # MXFP4-QAT would carry MXFP4 across expert weights UNIFORMLY, the way
  # gpt-oss does; two isolated tensors read as the dynamic quantiser
  # picking a format per tensor. Not decidable from the GGUF alone —
  # settle it against the upstream repo's own tensor dtypes if it matters.
  #
  # WHAT THIS PREDICTS FOR FAILURE MODE (sharpens `why` below): the
  # attention path is near-lossless and the router is EXACT, so expert
  # selection and structural coherence should hold. Risk concentrates in
  # expert-resident KNOWLEDGE at ~2 bits — expect wrong API names,
  # confabulated specifics and precision loss BEFORE incoherence or
  # looping. If we instead see degeneration/orbits, that is a finding
  # ABOUT the arch or our serving, not about the bit depth.
  expectation:
    registered: 2026-08-03
    basis: intake — architecture and size class only, no live behaviour seen
    outcome: pending
    why: >
      PRE-REGISTERED. A 256-expert/6-active MoE at IQ3_XXS is the fleet's
      first sub-4-bit arm, and quantisation that aggressive is where the
      laguna work found degeneration risk concentrates — so the first
      question is not tier but STABILITY (degen events, coherence at depth).
      Size class and DeepSeek's reasoning pedigree argue for tier 1
      capability if it serves cleanly; the IQ3_XXS quant and an untested
      arch argue for surprises. Expect a stability finding before a score.
```

---

## `devstral-2-small-24b.yaml`

```yaml
tier:
  league: grinder  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: recorded
  rubric: TIER_RUBRIC v1.2
  stars: 3                  # 41-60 band; 44/100
  tier: 2                   # < 50, and UNANIMOUS — all three votes < 50

  expectation:
    registered: 2026-07-29
    tier: "3-2"
    stars: null
    basis: experience
    outcome: hit
    why: >
      Minimum viable model. Long native context and reasonably usable output,
      but expected at the bottom of the field.
    scored: >
      HIT on both clauses. Landed tier 2 (inside the registered "3-2" range)
      and LOWEST OF THE ENTIRE FIELD at 44 — below laguna-s-2.1-apex's 46,
      which had been the sweep floor. "Bottom of the field" was exact.

  # SECOND PRE-REGISTRATION, added 2026-07-31 BEFORE this model's 2h arm was
  # judged. The 07-29 entry above stands unrevised (§6: never revised); this is
  # a DIFFERENT and sharper claim — about the MECHANISM behind a metric, not
  # about placement — and it is recorded separately so both can be scored.
  #
  # THE OBSERVATION IT EXPLAINS. In the 2026-07-30 smoke, devstral led the fleet
  # on files-per-generated-token by a wide margin, on a speed-matched comparison
  # (29-43 tok/s band, so decode rate is held roughly constant):
  #
  #     devstral        7,485 gen tokens ->  9 files   1.20 files/ktok  <- best
  #     gpt-oss        11,895 gen tokens -> 11 files   0.92
  #     qwen-next      10,991 gen tokens ->  8 files   0.73
  #     qwen3.5        20,214 gen tokens -> 12 files   0.59
  #     step37         53,409 gen tokens ->  8 files   0.15
  #     qwen3.6-35b    84,659 gen tokens ->  7 files   0.08  <- worst
  #
  # It was also mechanically clean: full batch delivery (9/9 declared files),
  # zero degenerations, completed rather than paused, 8 minutes.
  expectation_2:
    registered: 2026-07-31
    predicts: low_score_despite_top_token_efficiency
    basis: experience
    outcome: confirmed
    scored: >
      CONFIRMED ON EVERY CLAUSE OF confirmed_if, measured 2026-08-01:
        "at or near the bottom of the sweep"  -> 44, LOWEST of ten scored arms
        "a low ambition|completeness (3.5)"   -> 3/10, UNANIMOUS, at the floor
        "a low no-broken-functions (3.1)"     -> 4/20 = 20%, lowest of the sweep
        "conformance may still look respectable" -> 8/10 (UNANIMOUS)
      The operator's mechanism was right, not just the direction: devstral
      topped files-per-generated-token at 1.20 files/ktok (next best 0.92, worst
      0.08) by writing the THINNEST files in the field, not the most work per
      token. Note the tell that makes it unambiguous — its conformance 8/10 is
      the LOWEST conformance recorded in the entire campaign, where every other
      arm scored 9 or 10, so even the "respectable" half came in under the field.
      PER why_it_matters, THE COLUMN SHOULD BE RETIRED from the DoE table rather
      than reported with a caveat: files-per-token rewards under-delivery and
      cannot distinguish nine thin files from eight substantial ones.
    why: >
      OPERATOR CALL, verbatim in substance: "I guarantee devstral is the best
      [on files-per-token] because it pushed the least robust work."
      The claim is that files-per-token is not an efficiency measure at all —
      it rewards UNDER-DELIVERING, because a model that emits nine thin files
      beats one that emits eight substantial ones and the ratio cannot tell
      them apart. Devstral tops that metric by writing less per file, not by
      writing more per token.
    confirmed_if: >
      The blind panel scores this artifact at or near the bottom of the sweep
      despite its top token efficiency — in particular a low
      ambition|completeness (3.5) and a low no-broken-functions (3.1) while
      conformance may still look respectable. That combination means the files
      exist and are shallow, which is exactly what the ratio cannot see.
    refuted_if: >
      It scores mid-field or better with a substantial artifact. That would
      make files-per-token a defensible first-cut efficiency measure after all,
      and would mean the 15x spread in that column is measuring something real
      rather than measuring terseness.
    why_it_matters: >
      This is a test OF THE METRIC as much as of the model. The blind panel is
      the only quality-adjusted instrument available; if it disagrees with
      files-per-token on the metric's own best case, the column should be
      retired from the DoE table rather than reported with a caveat.
      Registered before judging so the answer cannot be rationalised afterwards.


  # ── 2026-07-31 ARM: RAN, GATE-FAILED, NOT SCORED ────────────────────
  attempted:
    run: tier_20260731-050209 arm11 · staged/arm11
    judge_model: claude-opus-5[1m]
    rubric: TIER_RUBRIC v1.2
    tier3_gate: failed
    scored: false          # gate failure terminates judgment (§2, §9.2)
    shape: 123 min · 12 files · 4 py_ok · 1 py_fail · 0 degen · 22/33 goals

    what_failed: >
      `engine.py` begins with a leaked DECISION ENVELOPE and a markdown fence:
      line 1 is `{"choice": "write_file", "path": "engine.py"}` and line 2 is a
      ```python fence. Line 1 is a legal dict literal so the parser survives it;
      line 2 is the hard stop. `main.py` imports engine on its first line, so
      every entry point — script, module and console-script — dies with
      SyntaxError before printing anything. The judge stripped exactly those two
      lines in a separate copy and the program BOOTED and accepted commands:
      249 lines, parses clean, class closes normally. The artifact underneath is
      intact.

    a_timing_casualty_not_a_framework_fault: >
      Stated precisely, because the first read — mine and the judge's — was that
      this is a write-path fault owed a re-run on those grounds. The log says
      otherwise. Line 83: batch structural creation, 9 files written, ZERO
      failed gates, so engine.py was clean. Line 6719: formatter runs on
      engine.py. Line 6720: engine.py fails deterministic re-cert on syntax,
      import and lint. Line 6721: routed to repair — the last mention. Line
      6739: parked at elapsed 7411s against a 7200s limit. So an edit 173 work
      cycles in re-emitted the file with the envelope, re-cert caught it on the
      very next check, and the backstop fired nineteen log lines later. THE GATE
      WORKED; the arm had about three minutes of clock left.

    provenance_of_the_envelope: >
      `{"choice": ...}` is OUR action schema — interactive_actions.py builds
      `{"choice": planned_action}` — so the model emitted a decision envelope
      where file content belonged and the extraction path wrote it verbatim.
      Model formatting error, framework did not sanitise it, gate caught the
      consequence. ISOLATED: a scan of every staged arm across both sweeps found
      no other leaked envelope or fence in any .py or .yaml file.

    rerun_owed: >
      OWED for an actual score. This is not a tier-3 model: it closed 22 of 33
      goals — second-highest in the sweep — across 173 work cycles, then broke
      its own engine in the last three minutes with no time to repair it. A
      re-run costs one arm and replaces a void datapoint with a real one.

    bears_on_the_operator_registration: >
      This arm was the designated test of the score-tightness doubt registered
      2026-07-31 (dev/blind_panel/RESULTS_tier_2026-07-31.md). AT THE TIME OF
      THE GATE FAILURE it settled nothing — devstral did not land middling and
      did not land low on merit, it landed VOID. RESOLVED 2026-08-01 by the
      repaired-entry-point judgment below; see `judged:`.

  # ── 2026-08-01: SCORED, after an operator-authorised two-line repair ──
  judged:
    run: tier_20260731-050209 arm11 · staged/arm11
    rubric: TIER_RUBRIC v1.2
    judge_model: "claude-opus-5[1m]"
    votes: [46, 44, 44]
    score: 44          # MEDIAN of three independent blind judgements (§5)
    stars: 3           # 41-60 band
    tier: 2            # by score, < 50 — and unanimous: all three votes < 50
    tier3_gate: passed  # AFTER the repair; it FAILED as shipped — see below
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 123 min (backstop) · 12 files · 4 py_ok · 1 py_fail · 0 degen · 22/33 goals

    # ── DISCLOSURE — THIS SCORE IS NOT OF THE ARTIFACT AS SHIPPED ──
    entry_point_repaired: true
    disclosure: >
      OPERATOR DECISION, 2026-08-01: "Strip those lines then judge the rest. A
      simple model mistake late in the game is not worth a two hour rerun for
      the same info."

      TWO LINES were removed from the head of `engine.py` before packaging:
      the leaked decision envelope `{"choice": "write_file", "path":
      "engine.py"}` and the opening ```python fence. Nothing else was touched;
      249 -> 247 lines; no trailing fence existed; no other file in the tree
      carried an envelope or fence. The strip script ASSERTS both lines match
      the characterised content before removing anything, so it cannot
      blind-truncate. After the strip all five .py files parse and the game
      boots and plays.

      WHAT THIS COSTS THE NUMBER: the leaked envelope was a REAL DEFECT and it
      is UNBILLED — no judge saw it, so §3.1 never priced it and the §2 tier-3
      gate never fired. As shipped this artifact scored TIER 3 (no runnable
      artifact: main.py imports engine on line 1, so every entry point died with
      SyntaxError before printing anything). The 44 therefore reads as "what
      this model built, minus one late formatting error", and is a small
      courtesy relative to arms that shipped clean. It is NOT a like-for-like
      comparison with them on that one axis.

      NOT A FRAMEWORK FAULT. Log line 83: batch creation, 9 files, ZERO failed
      gates — engine.py was clean when written. Line 6720: deterministic re-cert
      catches it on syntax+import+lint and routes to repair. Line 6739: parked
      at 173 work cycles, elapsed 7411s against a 7200s limit. The gate worked;
      an edit late in the run re-emitted the file with the envelope and the
      backstop fired nineteen log lines later, with about three minutes left.

    # MODAL vector — and it sums exactly to the median total, so no synthesis
    # was needed (unlike arm13/arm14, where no two judges agreed on a shape).
    # FIVE dimensions were UNANIMOUS across three strangers.
    # Stars are score/max banded per §2: 0-20=1, 21-40=2, 41-60=3, 61-80=4,
    # 81-100=5, inclusive at the TOP.
    dimensions:
      no_broken_functions: {score: 4, max: 20, stars: 1}   # votes 6/4/4
      robustness:          {score: 5, max: 10, stars: 3}   # votes 5/6/5
      ux:                  {score: 4, max: 10, stars: 2}   # votes 4/4/3
      conformance:         {score: 8, max: 10, stars: 4}   # UNANIMOUS 8 — 42-43/53
      ambition:            {score: 3, max: 10, stars: 2}   # UNANIMOUS 3
      creativity:          {score: 4, max: 10, stars: 2}   # UNANIMOUS 4
      org_project:         {score: 4, max: 5,  stars: 4}   # votes 4/3/4
      org_logic:           {score: 3, max: 5,  stars: 3}   # UNANIMOUS 3
      reusability:         {score: 7, max: 10, stars: 4}   # votes 7/7/8
      documentation:       {score: 2, max: 10, stars: 1}   # UNANIMOUS 2 — below the §3.10 cap

    profile: >
      AN EXPLORATION-AND-INVENTORY PROGRAM WEARING THE COSTUME OF AN RPG. The
      front half works cleanly — movement, take/drop/use/equip/examine/talk,
      look, status, help, save, and a genuinely extensible YAML data layer where
      all three judges added a room and an item with no Python at all, first
      try. Nothing on the happy path crashed across ~250 commands.

    decisive_defect: >
      THE GAME HAS NO TERMINAL STATE OF EITHER KIND. There is no victory
      anywhere in the tree — a grep for win|won|victor|congratul|game over
      returns zero matches — and there is no defeat either, because NOTHING IN
      THE CODEBASE EVER DECREMENTS PLAYER HEALTH. Combat is a one-shot damage
      subtraction with no turn loop: monsters never retaliate, their authored
      `attack`/`defense` are never read, armour has no effect, and `flee` is a
      one-line stub that prints a sentence. All three judges killed the boss
      with the correct weakness item and simply kept playing in an empty room,
      at full health, having taken zero damage all run.

      This is a step PAST the asymmetry the checklist warns about: prior arms
      shipped a defeat path and no win. Here both are absent.

    other_findings: >
      - SAVE/LOAD SILENTLY REWRITES THE WORLD (INVALIDATING, all three judges):
        `load` restores four player fields and re-initialises the world from
        YAML. `defeated_monsters` is faithfully WRITTEN to the JSON and then
        never applied, so everything you killed is alive again; room contents
        are rebuilt while inventory is restored, so every carried item is also
        back on the floor and can be re-taken indefinitely.
      - YOU CANNOT ADDRESS ANYTHING BY THE NAME THE GAME PRINTS. `look` says
        `Items: Rusty Sword`; `take Rusty Sword` is refused. Only the never-
        displayed YAML id works (`sword`, `old_man`, and the boss answers only
        to `boss`, a token printed nowhere). One judge traced the cause: the
        parser assigns `target` the whole lowercased remainder of the line, so
        NO multi-word display name can ever match an id — which is every named
        entity in the game. Two judges had to open world/*.yaml to proceed.
      - FOUR OF NINE ROOMS HAVE NO INBOUND EDGE (secret_room, dungeon, garden,
        library), orphaning the Wizard NPC who carries the clearest weakness
        hint, one of three regular monsters, and three items. Boss Room's north
        AND south both return to the Hallway. One-line data fixes.
      - `drop` does not clear the equipment slot: dropped sword stays equipped
        and still deals 5 damage from an empty inventory (unarmed is 1).
      - Consumables are never consumed and health is uncapped: one potion used
        four times took health 20 -> 60.
      - `talk` never advances dialogue: `dialogue_states` is declared, read,
        saved and loaded but NEVER WRITTEN, so the second node — the one that
        names the boss weakness — is undeliverable.
      - The boss weakness does not exist as a mechanic. The Mystical Amulet is
        simply `type: weapon, value: 10`, so it hits everything equally; the
        only boss/amulet coupling is an inert hardcoded name check in
        `handle_equip`.

    unmet_requirements: >
      42-43 of 53 met — the LOWEST conformance of the campaign, where every
      other scored arm returned 9 or 10. Agreed unmet: 23 (status omits
      location), 24 (no turn-based loop), 27 (armour never read by any
      calculation), 31 (nothing damages the player), 32 (one shared handler;
      monsters differ only in numbers), 34 (no phase logic), 35 (no weakness
      mechanic), 37/38/39 (no win, no defeat screen, no restart), and the
      world-state item (41/42 — the save is not "full").

    instrument_note: >
      FIVE of ten dimensions came back IDENTICAL across three strangers
      (conformance 8, ambition 3, creativity 4, org_logic 3, documentation 2),
      and the modal vector sums exactly to the median total. Judge 2 WITHDREW a
      billed row in the artifact's favour after discovering in pass 2 that it
      had run `examine <npc>` from the wrong room — a judge auditing its own
      probe rather than its target. All three judges disclosed a §4-sanctioned
      pass-1 source read of world/*.yaml, undertaken only to recover the input
      vocabulary after observing the refusals in play.
```

---

## `gemma-4-26b-a4b.yaml`

```yaml
tier:
  # GRINDER (operator, 2026-08-03): added to the v2 create queue now that the
  # kinks are worked out — the gemma think-activation fix (ec5d54c: <|think|>
  # had served as literal bytes on every gemma run ever) plus the batched-mode
  # boot this config runs. NOTE the v1.2 register's 196 cyc/h for this arm is a
  # NON-THINK number; with thinking live the rate drops substantially (a hard
  # prompt burned a whole 6k budget inside one thought channel during
  # validation), so re-measure the league from THIS run rather than trusting
  # the old figure — it may prove a contemplator.
  league: grinder  # provisional — see the re-measure note above
  status: not_yet_run
  expectation:
    registered: 2026-07-29
    tier: "2-1"
    stars: null
    basis: benchmarks
    why: >
      EXPERIMENTAL — not from experience. Benchmarks indicate that in thinking
      mode it should land below gemma-4 dense thinking, but close to qwen3.6-27b.
      Its speed and extra context may prove advantageous in ways the benchmarks
      do not show.
```

---

## `gemma-4-31b.yaml`

```yaml
tier:
  # RE-MEASURED 2026-08-03, first think-LIVE production run (post-ec5d54c):
  # 27 cycles / 121min = 13.4 cyc/h — CONTEMPLATOR band. The grinder
  # register value was a non-think number (thinking was never activated
  # pre-fix). This arm ran under grinder protocol (2h wall) before the
  # re-measure existed; league change applies from the NEXT run. 10 degen
  # events this run — first thinking production sample, watch.
  league: contemplator  # re-measured think-live; was grinder (non-think artifact of the <|think|> bug)
  v2:
    status: placed
    rubric: TIER_RUBRIC v2
    epoch: "v2.0 (brief v2 / framework ec5d54c)"
    tier: 2               # lost to GUARDIAN 0-5; FLOOR flight pending (low vs near-boundary)
    judged:
      run: tier_20260802-043526
      staged: staged/arm01
      judge: "claude-opus-5 (blind subagent, flight packet)"
      flights:
        - opponent: GUARDIAN (gpt-oss anchor)
          result: "LOST 0-5 axes + overall, no self-flag"
          conformance: "31/47 SIGNIFICANTLY-DEVIATED (BOTH triggers: 16 unmet >= 10 AND no win path)"
          completability: "UNWINNABLE at room 1 of 9 — parser emits go/take, engine dispatches move/pickup; the whole authored world (9 CONNECTED rooms, 6 items, branching NPC trees both hinting the weakness) is inert behind the seam. drop/use/stats parse to actions with no handler"
          why_the_judge_preferred_guardian: >
            Working surface: the Guardian's 14 advertised verbs all work
            when invoked; this artifact offers ~15 of which 4 are live and 2
            of those inert — the go/take vs move/pickup seam locks the
            player in room 1 of 9. State: the Guardian's round-trip was
            exercised against a genuinely mutated world (location,
            inventory, equipment, per-room items) and restored exactly; this
            save is clean but vacuous — nothing can ever change — and its
            schema silently drops attack_power and max_health. Experience:
            the Guardian has an accurate help table, informative refusals,
            and narrated combat; this artifact has no help at all and its
            title screen advertises save/load while never naming the four
            verbs that actually work. Scope: a traversable world,
            equip-driven stat changes and verified persistence, vs richer
            authored data of which essentially none is reachable. Ambition:
            this artifact attempted more content (dialogue state machines, 9
            rooms, behaviour labels) but none survived contact; the Guardian
            attempted more mechanism and most of it survived.
          notes: "First think-LIVE run (36 thought emissions, 10 degen events, 13.4 cyc/h). Guardian tally converged for the 4TH independent judge (41/47, identical unmet set)"
        - opponent: FLOOR (devstral anchor)
          result: "LOST 3-2 axes (took state integrity + ambition), overall FLOOR, no self-flag — places BELOW the floor anchor: low tier 2"
          why_the_judge_preferred_floor: >
            The FLOOR took the three axes that ask what a player actually
            gets. Working surface: its 13-verb surface works bar two, while
            this artifact's movement and take are both dead on the
            action-name mismatch — nothing past look/inventory/save can be
            invoked and the player cannot leave room one. Scope: 4
            explorable rooms, two fightable monsters, a working equip chain
            (Attack 5→10, Defense 2→5), mid-combat healing and a terminal
            defeat, vs one room and zero fights. Experience: an accurate
            help that correctly advertises bare directions and a world you
            can walk, vs better prose with no help and no discoverable
            action that advances anything. This artifact took state
            integrity (save/load on command, restored a hand-written state
            exactly — the FLOOR's save/load isn't wired to any command) and
            ambition (the only fully-connected 9-room graph, four distinct
            behaviour labels, the weakness threaded through two dialogue
            graphs, and the only ambitious subsystem that survived anywhere
            in the flight: full-schema persistence). The judge's overall
            line: "A is a game that can be played; B is a title screen with
            a look command."
          notes: "The judge RESOLVED the '//' literal question: model-innate stylistic tic (title screen deliberately uses '// Type ...' decoration; the tic bled into parser alias literals) — extraction exonerated. Real strengths on record: the flight's only zero-traceback robustness, honest full-schema persistence (dialogue state + room deltas), the only fully-connected 9-room graph. All behind the one seam."
  # NOT A SETTLED SCORE. A run happened and produced a tier-3 gate failure, but
  # the rubric's own re-run rule applies — "re-run only when the failure looks
  # like a framework or config fault rather than the model" (§2) — and it does.
  # See rerun_owed below. Left as not_yet_run on purpose so nothing downstream
  # reads a framework-shaped tier 3 as this model's placement.
  status: not_yet_run
  expectation:
    registered: 2026-07-29
    tier: "1"
    stars: 4
    basis: experience
    why: >
      Strong tier 1 candidate in NON-THINKING mode, likely 4 stars. Its lavish KV
      architecture makes it expensive to run under swa_full for compatible
      features; the competent base buys most of what it needs without them. Open
      question: whether thinking on helps task performance or trades too much
      speed.

  # ── 2026-07-31 ARM: RAN, GATE-FAILED, NOT SCORED ────────────────────
  attempted:
    run: tier_20260731-050209 arm07 · staged/arm07
    judge_model: claude-opus-5[1m]
    rubric: TIER_RUBRIC v1.2
    tier3_gate: failed
    scored: false          # gate failure terminates judgment (§2, §9.2)
    shape: 121 min · 6 files · 5 py_ok · 1 py_fail · 2 degenerations · 4/33 goals

    what_the_judge_found: >
      TWO blockers, and the decisive one is not the syntax. `main.py` dies on
      import: `game_engine.py` carries FIVE syntax errors (lines 75/114/151/
      159/160), each a self-correction fragment left in place — a broken line
      immediately superseded by a working one, plus a `//` C-style comment.
      With those commented out in a diagnosis copy the program still exits at
      startup: `world.yaml` DOES NOT EXIST anywhere in the artifact, and
      world_loader reads it as the sole source of every room, item, NPC and
      monster. No amount of syntax repair produces a runnable game.

    why_a_rerun_is_owed: >
      Both failures are framework-shaped, and BOTH have since been fixed.
      1. world.yaml never reached disk: the scaffold parse floor rejected the
         write outright (ScannerError) and no repair path re-entered — one
         rejection, no retry, no data.
      2. The repair loop was denied its own successes. All 51 splice attempts
         targeted `GameEngine.process_command`, correctly. FOURTEEN of them
         REPAIRED it — the post-splice error line moved off 75 onto 140/152/
         153/155, which is where the two sibling helpers sit once the edited
         body shrinks. The gate parsed the WHOLE file, saw a residual error in
         a symbol the edit was never scoped to touch, and reverted. 37 later
         attempts then restarted from a file an earlier attempt had already
         partially fixed. Fixed 2026-07-31 (49fa9a0): an error OUTSIDE the
         spliced body no longer reverts an already-broken file.
      The residual symbols were also UNNAMEABLE — `def 8_world_rooms_get`,
      an identifier starting with a digit — so no symbol-scoped editor could
      ever have addressed them. Convergence needs the module-frame path; the
      gate fix is what lets a repair SURVIVE until one runs.

    what_is_NOT_excused: >
      The model really did emit malformed code — the digit-leading identifiers,
      the C-style comment and the self-correction residue are its own output,
      not pipeline damage. Verified: no channel markers reach the shipped files,
      the FSM strips gemma's thought preamble correctly (formats/gemma.yaml
      close_tag), and the capture shows an empty thought block, so `reasoning: 0`
      is accurate rather than a missed extraction. A rerun tests whether the
      repair loop can now recover from that, not whether the model was clean.

    rerun_owed: >
      OWED (operator, 2026-07-31) for an actual score. Needs the splice-gate fix
      (49fa9a0) and the critic-prompt exemplars (5852992) — though this model
      designs FLAT layouts and never hit the design gate, so the second matters
      only for its 26b siblings.
```

---

## `glm-4.7-flash.yaml`

```yaml
tier:
  league: contemplator  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: recorded
  rubric: TIER_RUBRIC v1.1
  stars: 2                        # ★★  34/100
  tier: 2                         # < 50
  rejudge: not_required           # 2★ is outside the 40-59 band

  # PRE-REGISTERED before the run, never revised. HIT: called "2-1 ... somewhat
  # shy of the qwen3.6 MoE models", then refined to "a bit lower if it hit the
  # serial path". It DID hit the serial path and landed tier 2 — the lower half
  # of the registered range.
  expectation:
    registered: 2026-07-29
    tier: "2-1"
    stars: null
    basis: reputation
    outcome: hit
    why: >
      EXPERIMENTAL. Good reputation for agent work, but given its architecture
      and age expected to land somewhat shy of the qwen3.6 MoE models.

  judged:
    run: tier_20260729-132535 · staged/arm01
    judge_model: claude-opus-5[1m]
    total: 34
    dimensions:
      no_broken_functions: {score: 0,  max: 20, stars: 0}   # ledger went negative, floored
      robustness:          {score: 4,  max: 10, stars: 3}
      ux:                  {score: 3,  max: 10, stars: 2}
      conformance:         {score: 9,  max: 10, stars: 5}   # 47/53 requirements
      ambition:            {score: 3,  max: 10, stars: 2}
      creativity:          {score: 4,  max: 10, stars: 3}
      org_project:         {score: 3,  max: 5,  stars: 4}
      org_logic:           {score: 3,  max: 5,  stars: 4}
      reusability:         {score: 3,  max: 10, stars: 2}
      documentation:       {score: 2,  max: 10, stars: 2}   # hard-capped, §3.10

    profile: >
      It WROTE the game and did not DELIVER it. Conformance 47/53 — a complete
      nine-room citadel, 6 items, 2 NPCs with branching hints, 8 monsters, a
      two-form boss, all authored in YAML. Working functions 0/20: play reaches
      three rooms and three fights and nothing else.

    decisive_defect: >
      TWO MISSING LINES IN THE LOADER. world_loader.load_world() builds
      Room(title, description, exits, monster_id) and never passes the room's
      items or npcs, so every item and both NPCs are orphaned in all nine rooms.
      Half the command surface is left with no legal object. Compounded by a
      cul-de-sac world graph (6 of 9 rooms have no inbound exit, hiding the boss
      and the entire endgame) and by movement printing NOTHING on success while
      `look` does not parse and `status` omits location — so the player is blind
      from room two onward.

      THIS IS THE SAME DEFECT AS laguna-S ON 2026-07-27: "the loader performs
      that back-link correctly for items only; the equivalent loop for NPCs and
      monsters is absent". Different model, different quant, same seam.

    also_recurring: >
      NO WIN CONDITION anywhere in the tree (grep for victor/congratul/you-win
      returns nothing; killing the boss drops you back at the prompt). That was
      gpt-oss's decisive defect in the same 2026-07-27 panel.

    notable: >
      `leave dagger` SILENTLY QUITS — `leave` is matched as a QUIT alias before
      a DROP alias, so a player trying to drop an item exits with no
      confirmation. And the modification probe found a masked crash: fixing the
      loader seam makes the game fail to boot, because render() passes a list of
      Item dataclasses to ', '.join (`sequence item 0: expected str instance`).
      The seam that orphans everything is hiding a second bug behind it.

    unmet_requirements: [23, 32, 37, 39, 41, 42]

  observed:
    run: tier_20260729-132535 (2026-07-29), the SECOND attempt
    stage: game_challenge_tier · top_phase quality · structural_mode batch · 2h wall
    exit: completed the full 127min backstop, mission parked `paused`
    goals: 8/31
    files: 12 (7 py), 0 syntax failures
    cycles: 36 (~3.5 min/cycle)

    # ── THE CHARACTER SIGNAL, and it dominates everything else ──────────
    # 471,573 raw chars generated, 85,746 after stripping thought: 82% OF ALL
    # OUTPUT WAS THINKING. Median useful generation 362 chars against a p90 of
    # 5,061. It deliberates enormously and emits tersely.
    thinking_overhead: 82% (471,573 raw -> 85,746 content across 59 generations)

    # ── BATCH RAN, BATCH UNDER-DELIVERED ───────────────────────────────
    # Confirmed from mission notes, not inferred:
    #   "Batch structural creation: 1 files written, 1 goals completed,
    #    0 failed gates, 5 missing (serial fallback). Generation cost: 13158
    #    tokens. Missing: world_loader.py, parser.py, engine.py, main.py,
    #    data/world.yaml"
    # So build_structure emitted 1 of 6 files for 13k tokens and reported
    # SUCCESS; mission_control then dispatched the other five one at a time via
    # dispatch_structural_create. Note the fallback marker is written to mission
    # NOTES and never to the run log, which is why two check-ins could not tell
    # batch from serial.
    structural_path: batch ran once, delivered 1/6, remainder built per-file
    batch_cost: 13158 tokens for one file

    # Clean where it counts: 59/59 generations returned valid fenced JSON, zero
    # parse failures, zero transfer-shape rejections, zero empty rewrites.
    # It is not a formatting problem — it is a wiring problem.
    format_discipline: 59/59 valid structured output; 0 gate rejections
    repair_activity: 3 regressed structural goals (engine.py via functional_sweep), 7 diagnoses

    # ── REQUIRES A TEMPERATURE FLOOR TO RUN AT ALL ──────────────────────
    # The FIRST attempt (tier_20260729-124648) was force-stopped at 32min with
    # 1 file and 0/31 goals: it looped in the architecture-coherence critic
    # (aborted by the long-cycle guard at 12,288 tokens) and then produced a
    # non-repetitive 29,022-token generation no guard could see. Adding
    # temperature_floor/session_temp_floor 0.7 fixed BOTH: longest generation
    # this run 12,909 tokens and it RETURNED. All 59 generations needed
    # clamping — 28 requested 0.10, 4 requested 0.07, 2 requested 0.00.
    # The runaway was a SAMPLING artifact: near-greedy decoding never selected
    # EOS. Not a stop-token problem, which was the wrong first diagnosis twice.
    requires_temp_floor: true
    unfloored_outcome: force-stopped 32min, 1 file, 0/31 goals

    # ── TRACE METRICS (dev/README: trace summary, never hand-mined logs) ─
    # traces/c417287e6a3c_20260729T192556.summary.json · completeness 85.9%
    metrics:
      wall_min: 127
      decode_tps: 31.8
      prefill_tps: 426.7
      tokens_generated: 144638
      tokens_input_real: 989296          # 730,326 fresh prefill + 258,970 cached
      inferences: 146
      cache_hit_rate: 1.00               # 145/145
      prefix_reuse_rate: 0.262
      # WHERE THE 127 MINUTES WENT. Inference is effectively the whole run and
      # DECODE is two-thirds of it — consistent with the 82% thinking tax: this
      # model's cost is tokens it generates, not context it reads.
      wall_pct:
        inference: 85.4                  # prefill 26.3 · decode 69.9
        terminal: 0.06                   # 4.3 SECONDS of 127 minutes
        mcp: 0.46
        unaccounted: 14.1
      counts: {cycles: 73, steps: 895, commands: 100, sessions: 24}

    # ── PTY / DIAGNOSIS QUALITY: the strongest part of the run ───────────
    # It DOES live-test, and the earlier failures were never a diagnosis
    # weakness — the unfloored run simply never reached this phase, burning 32
    # minutes inside one deliberation. With the floor it drives a terminal,
    # observes real failures, and names real causes:
    #
    #   "observation commands like LOOK, TALK, or TAKE fail to execute during..."
    #   "a bug in the Parser._is_valid_target method where it receives a w..."
    #   "The ATTACK command handler was diagnosed as the root cause..."
    #   {"confident": true, "root_cause": "The gate failure is not in src/parser.p..."
    #
    # Those are the SAME defects the blind judge found independently, and the
    # last one correctly declines to blame the orphaned src/parser.py.
    #
    # THE LIMIT IS CONVERGENCE, NOT PERCEPTION. `Define data models` was tested,
    # completed, regressed, re-tested, diagnosed, fixed, re-tested and diagnosed
    # again — four visits, still open at the backstop. It sees the problem,
    # describes it accurately, applies a fix, and the fix does not hold.
    #
    # SESSIONS ARE SHALLOW: 12 PTY sessions, five of them ONE turn (start, one
    # command, close). Enough to catch "does it boot", not enough to catch "can
    # you reach room two" — which is exactly the defect that sank the artifact.
    # The judge needed a probing harness to map the world; the agent never
    # walked it.
    #
    # AND TERMINAL TIME IS 0.06% OF WALL. Live testing is nearly free here, so
    # deeper functional exploration is a cheap lever — the budget is spent on
    # deliberation, not on driving the program.
    pty: 12 sessions (5x1-turn, 3x2, 3x3, 1x4) · 9 functional sweeps, 7 completed, 4 diagnoses
    diagnosis_quality: specific and correct; matched the blind judge's findings independently
    convergence: poor — one goal took 4 visits and stayed open

    note: >
      Reputation for agent work is not contradicted by this run — the structured
      output discipline is the best in the fleet so far (59/59 clean JSON) and
      the diagnoses are accurate. What cost it is seam wiring plus an 82%
      thinking tax at 31.8 tok/s, which leaves little budget for delivery inside
      2h. Since terminal time is 0.06% of wall, more functional depth is close
      to free and is the obvious lever; llama.cpp #19613 remains open upstream,
      and the temperature floor is what makes this model usable here regardless.
```

---

## `gpt-oss-120b-a5-swarm-524k.yaml`

```yaml
tier:
  league: grinder  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: recorded
  rubric: TIER_RUBRIC v1.2        # CURRENT score; the v1.0 record is below
  stars: 3                        # 41-60 band; 53/100
  tier: 1                         # >= 50

  judged_2026_07_31:
    run: tier_20260731-050209 arm04 · staged/arm04
    judge_model: claude-opus-5[1m]
    total: 53
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    dimensions:
      no_broken_functions: {score: 6, max: 20, stars: 2}
      robustness:          {score: 6, max: 10, stars: 3}
      ux:                  {score: 7, max: 10, stars: 4}
      conformance:         {score: 9, max: 10, stars: 5}   # 49/53
      ambition:            {score: 4, max: 10, stars: 2}
      creativity:          {score: 5, max: 10, stars: 3}
      org_project:         {score: 2, max: 5,  stars: 2}
      org_logic:           {score: 3, max: 5,  stars: 3}
      reusability:         {score: 9, max: 10, stars: 5}
      documentation:       {score: 2, max: 10, stars: 1}

    decisive_defect: >
      THE WIN IS UNEARNED. Victory in FOUR commands from a cold start,
      bare-handed: `go north` x3 then `attack ancient dragon`. Weapon, armour,
      potion, hidden weakness item, both guard monsters and both NPC hint
      chains are all optional decoration — nothing gates the lair.
      COMPOUNDED BY THE PHASE SYSTEM: the phase-2 transition sets
      `monster.behavior = "enraged"`, but the monster-turn dispatch handles only
      aggressive/defensive/coward — so "the dragon roars, entering a furious
      second phase" is the moment the boss STOPS ATTACKING. Death is unreachable
      too: the whole world deals ~68 damage against 100 starting HP.

    the_duplicate_tree: >
      Ships a parallel `src/` implementation — ~1000 lines of incompatible
      duplicate modules under the same names as the live ones. It cannot run:
      `python3 src/main.py` and `python3 -m src.main` both traceback, and there
      is no `src/loader.py` or `src/save_system.py`. It inflated this arm's file
      count to 18 (vs 14 for the pool arm) and was the reason the operator's
      first read called this the strongest run of the sweep. It was the LARGEST.

    counters_inverted_again: >
      18 files / 13 py_ok / 29-of-34 goals against the pool arm's 14 / 7 /
      25-of-35 — and scored SEVEN POINTS LOWER (53 vs 60). Fifth consecutive
      round in which goal counters have inverted against blind play.

    strategy_note: >
      S3 vs the S2 pool arm (60/100) is CONFOUNDED: arm04 was also the first
      arm on vendor-correct sampling (top_k 0 no-limit; arms 01-03 were served
      40). S3 and post-fix sampling moved together. What can be said is that the
      extra output did not buy quality, and much of it was dead weight.

    unmet_requirements: [29, 32, 39, 51]   # 2 regular monsters (the data's
                                           # third IS the boss); no distinct
                                           # behaviours (aggressive/defensive
                                           # dispatch identically, `coward` is
                                           # assigned to nobody); no restart on
                                           # death; no dialogue branching

  # ── PRIOR ROUND, 2026-07-29, under TIER_RUBRIC v1.0 — HISTORY ───────
  # Kept unrevised. Its decisive defect is the same FAMILY as the 07-31 one and
  # the pair is the point: this model's recurring weak spot is WIN GATING.
  #   2026-07-27  no win condition existed anywhere in the tree
  #   2026-07-29  win by FLEEING, six commands from a cold start
  #   2026-07-31  win unearned, four commands from a cold start
  # Three consecutive rounds, same seam class, and the pool sibling the same day
  # (60/100) got the gating RIGHT while collapsing the set-piece instead.
  prior_2026_07_29:
    rubric: TIER_RUBRIC v1.0        # v1.1 amended after; see its changelog
    stars: 3                        # ★★★  54/100
    tier: 1                         # >= 50

  # 3★ is the 40-59 rejudge band, which normally costs two more independent
  # judgements. WAIVED BY OPERATOR on the strength of tens-to-hundreds of
  # millions of tokens run through this model while building the framework —
  # the score matched the pre-registered expectation ("a moderately scoring
  # tier 1") and there is no genuine uncertainty about where it sits. Recorded
  # as an override, not as a settled 3-vote result: a later reader must be able
  # to see that no rejudge happened.
  rejudge: waived_by_operator

  # PRE-REGISTERED, and stated BEFORE the judge ran (2026-07-29, in session).
  # Recorded after the fact only because the expectation mechanism was written
  # later the same day; the wording is the operator's, unchanged.
  #
  # IT HIT. Predicted "a moderately scoring tier 1"; judged 54/100 — tier 1 by
  # two points, and about as mid-band as the scale allows. First live use of the
  # only drift detector left after anchors were dropped, and it agreed.
  expectation:
    registered: 2026-07-29
    tier: "1"
    stars: 3
    basis: experience
    outcome: hit
    why: >
      A moderately scoring tier 1. Notes expected to cover its general
      flexibility in adapting to new features, its reliability under framework
      faults, and its speed. Basis is tens to hundreds of millions of tokens run
      through this model while developing the framework.

  judged:
    run: tier_20260729-010118 arm 4 · staged/arm04
    judge_model: claude-opus-5[1m]
    total: 54
    # Per-dimension stars are score/max banded per §2 (see below) — different maxima
    # BANDS (rubric §2, inclusive at the top): 0-20=1, 21-40=2, 41-60=3,
    # 61-80=4, 81-100=5. So 40%% is 2 stars, 60%% is 3, 80%% is 4.
    # normalised so the SHAPE reads at a glance, which is the whole point of
    # abstracting to stars.
    dimensions:
      no_broken_functions: {score: 7,  max: 20, stars: 2}
      robustness:          {score: 4,  max: 10, stars: 2}
      ux:                  {score: 6,  max: 10, stars: 3}
      conformance:         {score: 9,  max: 10, stars: 5}   # 49/53 requirements
      ambition:            {score: 5,  max: 10, stars: 3}
      creativity:          {score: 5,  max: 10, stars: 3}
      org_project:         {score: 4,  max: 5,  stars: 4}
      org_logic:           {score: 3,  max: 5,  stars: 3}
      reusability:         {score: 8,  max: 10, stars: 4}
      documentation:       {score: 3,  max: 10, stars: 2}   # hard-capped, §3.10

    profile: >
      Strong on conformance, structure and modifiability; weak on working-ness
      and documentation. The judge's own summary — "finished components,
      unfinished seams."

    decisive_defect: >
      WIN BY FLEEING. run_combat returns one boolean for two outcomes, and its
      own docstring admits it ("Returns True if the player survives (by
      defeating the monster or fleeing)"); initiate_combat reads it as victory.
      Six commands from a cold start — no weapon, no armour, no amulet, no NPC
      contact — print "=== VICTORY ===". Fleeing a regular monster also deletes
      it from the world and records it as a corpse. This nullifies the
      two-phase boss, the hidden weakness item and both NPC hint chains, i.e.
      every part of the design the artifact got right.

    strengths: >
      NOTHING ORPHANED — every room, item, NPC and monster authored in YAML is
      reachable in play. That is the failure mode which decided most prior
      panels and this artifact does not have it. Modification probe landed all
      three tasks first try, with a new room + item needing ZERO Python edits.

    unmet_requirements: [29, 39, 44, 51]   # 2 regular monsters not 3; no restart
                                           # after death; 7 rooms not 8; dialogue
                                           # is a flat list, not branching

  observed:
    run: tier_20260729-010118 arm 4 (2026-07-29)
    stage: game_challenge_tier · top_phase quality · structural_mode batch · 2h wall
    boot_s: 41
    exit: rc=0 at the full 120min backstop, mission parked `paused`
    goals: 28/36
    syntax: 7 py_ok / 0 py_fail
    degeneration: 0
    artifact: 17 files, staged clean (identifier scan passed)
    shutdown: released the server in ~1s at the backstop — bounded generations
              hand over cleanly, unlike an abandoned one
    note: >
      Counters vs play, again. This arm led the batch on goals (28/36 vs 6/37)
      and shipped zero syntax failures, and still lands mid-band because the
      thing it built can be won by running away. Goal counters have inverted
      against blind play in four consecutive rounds; treat them as progress
      telemetry, never as the instrument.
    prior_contrast: >
      On 2026-07-27 this model's decisive defect was the INVERSE — no win
      condition existed anywhere in the tree while the defeat path shipped
      complete. The win path now exists and fires too easily. Same seam, opposite
      failure; worth watching whether that asymmetry recurs.
```

---

## `gpt-oss-120b-a5.yaml`

```yaml
tier:
  league: grinder  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: recorded
  rubric: TIER_RUBRIC v1.2
  stars: 3                 # 41-60 band; 60 is its TOP — borderline 4 star
  tier: 1                  # >= 50

  # 3-star normally costs two more judgments (§5). WAIVED — the operator
  # CORROBORATED on deep prior experience with this model ("solidly tier 1,
  # borderline 4 star"). Recorded as a corroboration, NOT as a three-vote
  # median, so a later reader can see which it was.
  rejudge: waived_operator_corroboration

  judged:
    run: tier_20260731-050209 arm03 · staged/arm03
    judge_model: claude-opus-5[1m]
    total: 60
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md

    # Stars are score/max banded per §2 (see below) so different maxima normalise.
    # BANDS (rubric §2, inclusive at the top): 0-20=1, 21-40=2, 41-60=3,
    # 61-80=4, 81-100=5. So 40%% is 2 stars, 60%% is 3, 80%% is 4.
    dimensions:
      no_broken_functions: {score: 9,  max: 20, stars: 3}
      robustness:          {score: 6,  max: 10, stars: 3}
      ux:                  {score: 7,  max: 10, stars: 4}
      conformance:         {score: 10, max: 10, stars: 5}   # 51/53 requirements
      ambition:            {score: 5,  max: 10, stars: 3}
      creativity:          {score: 5,  max: 10, stars: 3}
      org_project:         {score: 4,  max: 5,  stars: 4}
      org_logic:           {score: 3,  max: 5,  stars: 3}
      reusability:         {score: 8,  max: 10, stars: 4}
      documentation:       {score: 3,  max: 10, stars: 2}   # hard-capped, §3.10

    profile: >
      THE FIRST COMPLETABLE ARTIFACT OF THE SWEEP, and the only one so far whose
      win is EARNED. Full marks on conformance (51/53) with delivery still under
      half — the same authored-vs-delivered gap as every other arm, but from a
      much higher floor: what the player CAN reach works.

    the_win: >
      Verified both directions by play. Sword + shield (Attack 5->10, Defense
      0->3), kill the Orc, then 8 turns against the 80 HP Dragon Lord at 14
      dmg/turn incoming, healing herb at 15 HP, land the kill. UNARMED AND
      UNARMOURED THE SAME DRAGON KILLS YOU IN 5 TURNS — defeat screen and restart
      both fire. The win requires the weapon, the armour and correct mid-combat
      resource use.
      A 7-command shortcut also exists (`use Crystal of Dawn` -> `attack`) and was
      explicitly NOT scored as a false victory: the Crystal is the brief's
      designated weakness item, hidden in the shrine, hinted by both NPCs, so the
      key item genuinely gates it. The defect is that `use` zeroes boss HP instead
      of driving a phase — billed SILENT-WRONG, not as an unearned win.

    decisive_defect: >
      HALF THE GAME SITS BEHIND A ONE-WAY EDGE. `hidden_chamber` declares
      `east: garden` while `garden` declares only `north: library`, so the link
      runs OUT of an unreachable room with nothing pointing back in.
      `skeleton_watcher` lives there; `goblin_guard` is authored as a 20 HP
      monster and assigned to no room's `monster:` field; `gold_coin` is in no
      room at all. Authored: 8 rooms, 3 regular monsters, 5 items. Delivered:
      7 rooms, ONE regular monster, 4 items.

    prior_contrast: >
      This model's 2026-07-29 decisive defect was the INVERSE — a victory that
      fired six commands from a cold start because `run_combat` returned one
      boolean for both "killed it" and "fled". This round the win is properly
      gated and the boss fight is real; what collapsed instead is the set-piece,
      to two commands. Same seam class, opposite polarity, second consecutive
      round. Worth continuing to watch.

    strengths: >
      State fidelity beyond the brief: a save at 20/30 boss HP reloads at exactly
      20/30, and partial monster HP, defeated monsters, room item movement, NPC
      dialogue progression and equipment all survive save -> quit -> fresh process
      -> load. Combat behaviours are real and legible in the numbers (defensive
      halves damage, aggressive retaliates twice). Modification probe landed all
      three tasks first try, two of them data-only.

    unmet_requirements: [34, 51]   # boss two phases (Monster.phase declared,
                                   # serialized and restored, never read or
                                   # written by combat); branching dialogue
                                   # (do_talk parses triggers; all 3 NPCs ship a
                                   # single entry with trigger: null)

    star_label_correction: >
      The judge reported 4 STARS. It read the rubric's band table, which was
      wrong at every boundary (it said 60-79 = 4 star). The bands are five equal
      20-point spans — 41-60 is 3 star — so 60 is the TOP of 3 star, not the
      bottom of 4. Table corrected in TIER_RUBRIC §2 on 2026-07-31. Score and
      tier unaffected; arms 01-02 were already correct at 46 and 48.

    sampling_regime:
      status: pre_fix
      declared_vs_served: "top_k declared 0 (no limit), served 40; min_p declared 0.0, served 0.05"
      note: >
        The MOST sampling-affected of the three pre-fix arms — a hard 40-token
        top-k cutoff against a declared no-limit is a materially different
        sampler, not the tail trim the two laguna arms got.
      fixed: 2026-07-31 (llama_cpp_backend._build_generate_kwargs)
      rerun: not_done (operator decision — annotate, do not re-run)

  observed:
    run: tier_20260731-050209 arm 3 (2026-07-31)
    stage: game_challenge_tier · top_phase quality · 2h wall
    exit: parked `paused` at the 123min backstop
    goals: 25/35
    syntax: 7 py_ok / 0 py_fail
    artifact: 14 files (8 py + 4 separate YAML data files), staged CLEAN — no
              identifier leak, no redaction, so no packet contamination caveat
    degeneration: 0
    note: >
      25/35 goals against 13/39 and 8/36 for the two laguna arms, with zero
      degenerations and zero syntax failures. On this mission the goal counter
      and the blind panel AGREE for once — the arm that led on counters also led
      on play. That is not the historical pattern (counters have inverted against
      play in four prior rounds); treat the agreement as a coincidence worth
      noting, not as counters becoming trustworthy.
```

---

## `hy3-reap-200b-a21.yaml`

```yaml
tier:
  league: both  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: judged

  # ── EPOCH v2.0 · CONTEMPLATOR ARM [c] — TIER 1 (beat the GUARDIAN) ──
  # First think-LIVE hy3 artifact (dial fixed 17462a2; routing per-request).
  v2_contemplator:
    run: tier_20260802-224028 arm02 · staged/arm02 · "THE SHADOW REALM"
    judge_model: claude-opus-5   # single blind judge, v2.1 two-panel flight
    rubric: TIER_RUBRIC v2.1
    tier: 1                      # boundary rule: beat the Guardian anchor
    flight: "GUARDIAN — Delivery 4-0, Character 5-1, overall WIN (hy3 sat B; no split, no close flag)"
    headline: >
      FIRST field artifact of the epoch a judge PLAYED TO VICTORY, and first
      field 47/47 NEAR-FULL (zero unmet). The weakness mechanic verified
      COUNTERFACTUALLY: with the Star Amulet the two-phase boss falls to the
      victory screen; without it the boss is invulnerable and the run ends in
      an honest defeat screen with a WORKING restart to a pristine world.
    why_the_judge_preferred_it: >
      "B reached less far and closed the loop." Delivery swept 4-0: 9 rooms /
      3 monsters / reachable victory vs the Guardian's 6 rooms and no win
      state anywhere; working mid-combat healing; instructive refusals that
      name the object and the fix. Character 5-1: named realm, two
      differentiated NPC voices whose hints are load-bearing and pay off,
      parser->engine->main seams the judge called "a testable boundary at
      every seam". The Guardian took only B5 ambition (nested combat REPL,
      typed entity layer, wield-the-key loadout sacrifice) — the axis
      designed to be able to dissent.
    defects_on_record: >
      Save is WRITE-ONLY (engine.load exists, no caller — checklist 37
      met-by-presence; no load command, no load-on-start). Boss phase HP
      pools are decorative: _boss_turn recomputes health from phase data
      every call, so the ±40 amulet modifier carries the fight, not damage
      accumulation. `talk to <npc>` mis-targets to room.npcs[0] (the parser
      keeps the `to` particle; bare `talk <npc>` works and reaches content
      the help's own phrasing hides). EOF at the save prompt tracebacks.
      Declining the restart offer lands in a dead prompt loop.
    observed: >
      Contemplator protocol: 30 cycles in 74min (24.3 cyc/h), 6 files
      (5 py + world.json — the fleet's THIRD data-format choice under the
      unopinionated v2 brief), 0 degen, 0 py_fail. Think-dial live: one
      explicit-high step measured 2798 reasoning + 2249 content tokens
      (55% thought); remaining steps routed none/low -> no_think per the
      hy3 routing map. hy3[g] grinder arm still owed.
    frontier_scorecard: >
      OUT-OF-BAND (does not move the tier). Lost to the Sonnet one-shot
      Delivery 4-0 / Character 6-0 — but FIRST field candidate to enter
      the flight with a WON game (the judge won both artifacts; the
      ceiling gap is now depth, not completability). Judge divergence:
      this judge scored 46/47 (charged item 45 — flat topic dicts, no
      player choice) vs the Guardian flight's 47/47 (staged progression
      counted met) — RESOLVED by operator ruling 2026-08-03: item 45 is
      presence-lenient, staged progression meets it, 47/47 STANDS; the
      gradation (fixed line < staged < state/choice-conditional) is
      comparative on axes B6/B7. New findings: winnable in 7 commands with no combat
      (monsters never block movement), no-enemy `flee` teleports one
      room, save payload lacks room state. §5 family-bias caveat stamped
      (judge claude-opus-5, Frontier artifact Claude-authored).
  expectation:
    registered: 2026-07-29
    tier: "1?"
    stars: null
    basis: experimental
    why: >
      EXPERIMENTAL, and the widest error bar in the field. IF the REAP prune did
      not severely damage model competence this should be the strongest model
      here. Tempering that: limited context, forced non-thinking, low speed,
      immature llama.cpp support, and REAP pedigree.
  # ── CURRENT: 2026-07-31, TIER_RUBRIC v1.2, 2h quality sweep ─────────
  # Scored TWICE now. Against the v1.2 REPLAY of the 07-29 ledgers (median 58,
  # tier 1) this is a 9-point drop to tier 2 — same rubric version, same model,
  # different run. Both records stand; neither replaces the other.
  result_2026_07_31:
    judged: 2026-07-31
    run: tier_20260731-050209 arm09 · staged/arm09
    rubric: TIER_RUBRIC v1.2
    judge_model: "claude-opus-5[1m]"
    votes: [49, 49, 46]
    score: 49          # MEDIAN of three independent blind judgements
    stars: 3           # 41-60 band
    tier: 2            # by score, <50 — and unanimous: all three votes <50
    tier3_gate: passed
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 120 min (backstop) · 14 files · 7 py_ok · 0 py_fail · 2 degen · 16/38 goals

    # Judges 1 and 2 returned IDENTICAL vectors across all ten dimensions.
    dimensions:
      no_broken_functions: {score: 4, max: 20, stars: 1}
      robustness:          {score: 4, max: 10, stars: 2}
      ux:                  {score: 4, max: 10, stars: 2}
      conformance:         {score: 9, max: 10, stars: 5}   # 50/53
      ambition:            {score: 4, max: 10, stars: 2}
      creativity:          {score: 6, max: 10, stars: 3}
      org_project:         {score: 4, max: 5,  stars: 4}
      org_logic:           {score: 3, max: 5,  stars: 3}
      reusability:         {score: 8, max: 10, stars: 4}
      documentation:       {score: 3, max: 10, stars: 2}

    decisive_defect: >
      THE WIN IS UNEARNED AND THE WEAKNESS ITEM IS A STRICT PENALTY. From a
      deleted save, five commands with an empty inventory — go north, go north,
      go east, go east, attack — print the victory banner. No weapon, no armour,
      no healing item, no Relic, zero monsters fought; the three monsters print
      "blocks the way!" and gate nothing.
      Worse, the Relic — named by the title screen, both NPCs and the boss data
      as the weakness — is the ONLY trigger for phase 2, where the boss hits 20
      instead of 15, while boss HP stays 60 either way. A controlled A/B from an
      identical save: holding it, 3 rounds ending at 74 HP; dropped, 3 rounds
      ending at 79. It costs 5 HP and saves nothing. The Crone's line "Without
      the Relic, the Dark Lord's second phase will end you" is exactly backwards.
      Confirmed balance, not structure: boss HP 60 -> 200 kills the naked player.

    structural_defect: >
      THERE IS NO GAME LOOP. `input()` appears nowhere in the tree except inside
      a comment. Commands arrive only as sys.argv, one quoted token each, with
      state carried between processes via state.json. The documented invocation
      (`python main.py`) runs a hardcoded demo — look / take relic / status —
      and exits, MUTATING the save each time. main.py's own docstring says
      "title screen, REPL loop, defeat/restart screen"; a comment calls argv an
      "optional non-interactive driver" for a loop that was never written.

    the_shipped_save: >
      The artifact ships a committed state.json from the author's own playtest
      holding NINE duplicate Relics, gear equipped and a monster defeated — so a
      judge's first invocation starts mid-game. It is the fossil record of the
      persistence bug: room contents are never saved, every process re-seeds the
      world from world.yaml, and the no-argument demo banks one more Relic each
      launch. .gitignore does not exclude it.

    unmet_requirements: [28, 42, 51]   # no mid-combat item use (combat resolves
                                       # atomically, yet help advertises it);
                                       # save omits all room/world state;
                                       # dialogue is linear progression, not
                                       # branching (out-degree 1, no conditions)

    inter_judge: >
      49 / 49 / 46. Judges 1 and 2 agreed on every dimension; judge 3 was harsher
      on ambition, creativity, reusability and documentation and more generous on
      UX. All three independently proved the naked five-command win, the Relic
      inversion, the missing REPL and the nine-Relic shipped save. Judge 2 ran
      the Relic A/B as a controlled experiment; judge 3 additionally found that
      `restart` is absent from `help` and that deleting an item from world.yaml
      that a save references raises KeyError.

    versus_the_boss_panel: >
      The two instruments disagree usefully. The boss-fit panel (2026-07-31,
      smoke artifacts) ranked this model SECOND of three, winning specification
      quality and handoff cost — judging what ONE capped generation produced.
      The tier rubric judges what a 2h run DELIVERS, and delivery is where it
      loses: a text adventure with no interactive loop. Good one-shot spec
      author, weak deliverer, on the same day.

  # ── PRIOR: 2026-07-29, v1.1 (with a v1.2 replay) — HISTORY ──────────
  prior_2026_07_29:
    judged: 2026-07-29
    run: tier_20260729-172119/staged/arm01
    # SCORED UNDER v1.1 AND NOT RECOMPUTED (operator decision 2026-07-29). v1.2
    # landed hours later and is the first amendment that moves scores: replayed
    # on these three ledgers it gives 3.1 = 7/5/9 instead of 0/0/6, totals
    # 58/53/62, median 58. Star band (3) and tier (1) are unchanged either way —
    # v1.2 only makes the tier unanimous instead of 2-of-3 — so nothing here is
    # misleading, but do not compare this 51 against a v1.2 number directly.
    rubric: TIER_RUBRIC v1.1
    v12_replay: {dim_3_1: [7, 5, 9], totals: [58, 53, 62], median: 58, stars: 3, tier: 1}
    judge_model: "claude-opus-5[1m]"
    stars: 3
    score: 51          # MEDIAN of three independent votes
    votes: [48, 51, 59]
    tier: 1            # 2-of-3 (48 = tier 2 side; 51 and 59 = tier 1 side)
    expectation: HIT
    # A FOURTH vote (54) was DISCARDED, not averaged in. The rubric's changelog
    # was shipping judges a past arm's exact total ("judged under v1.0 at
    # 54/100"), an anchor §6 deliberately dropped — and that vote returned
    # precisely 54. Coincidence and anchoring are indistinguishable from
    # outside, so the vote was voided and the packet builder fixed (it now
    # redacts score citations and scans the FINAL text for them). The same
    # packet also carried `Arm D-120b-a5-swarm-524k`: redact() replaced the
    # model head and left the config tail, which the identifier scan cannot
    # catch because the tail is not itself a listed token. Both fixed before
    # the three votes above were dispatched.
    verdict: >
      THE FIRST WINNABLE ARTIFACT OF THE CAMPAIGN. All three judges completed
      the game independently — full route to the Throne of Bone, both boss
      phases, the charm weakness firing, the victory screen — and all three also
      reached the defeat screen. Three of the four previously judged artifacts
      shipped a complete authored world that play could not reach; this one can
      be finished. Conformance 49-51 of 53.
      WHAT HELD IT AT 3-STAR IS ONE REPEATED SHAPE, not breadth of failure:
      everything is built and wired at all but one seam. `room_changes` is
      designed, populated, serialised AND deserialised, then never applied — so
      every resume duplicates carried items and destroys dropped ones. `quit` is
      in the enum, the verb map, the help text, the title screen and main.py's
      outcome branch — everything except one `elif` in dispatch, so the first
      command a player tries answers "Unknown command." and EOF is the only exit.
      `_look` is DEFINED TWICE in engine.py and the shadowed first copy is the
      better one — it printed `Exits:`, so the artifact's worst usability gap was
      written and then buried in the same file. The third promised monster is
      orphaned by a `Monster.room_id` the engine never reads (it fights correctly
      the instant a room names it). Name matching is implemented in five places
      and dead in all five because the caller passes `args[0]` and every name in
      the world is two words — the game refuses the names it prints.
      Robustness was the standout (8-9): ten malformed saves, 500-char input,
      emoji, shell metacharacters, three kinds of EOF — no traceback, no hang.
      Documentation was capped at 2-3 by five contradicted claims, worst being
      pyproject.toml asserting "PyYAML is used via the built-in yaml module
      (Python stdlib)" with requires = [] — in a clean venv it will not start.
    # ── PERFORMANCE (from the trace registers, not the server log) ──
    perf:
      wall_min: 130
      goals: "16/38 complete (paused at the 2h backstop)"
      files: 15
      py_fail: 0
      degenerations: 0
      decode_tok_s: "19.6-25.7 (slowest in the fleet)"
      prefill_s_of_wall: "2121 of 3486 = 60.8%"
      cache_hit_pct: 33.4
      cot_pct: 0.0        # first run under the new CoT-split metric
      cot_coverage: 0.0   # measured zero, not an absent measurement
    # ── HANDICAPS PRESENT IN THIS RUN (score is NOT clean) ──
    handicaps: >
      1. QUADRATIC SESSION COST. This arm ran full_replay (resident_seq_cache
      false), so every PTY turn re-prefilled the whole session. The worst
      diagnose_issue session spent 553s of prefill against 33s of decode (94.4%)
      and its 10th turn cost 93.2s of prefill for 28 tokens. Prefill was 60.8% of
      run wall and goal throughput fell ~5x between the first and second half
      hour. Peak context use was 16,066 of 32,768 (49%), so the halved per-seq
      window resident would need was never approached — see
      dev/CACHE_SWEEP_PLAN.md. A re-run with resident enabled is owed and this
      score is the BASELINE arm of that A/B.
      2. TRANSIENT-FLUSH CONTAMINATION (OPEN_TASKS 11d). The architecture
      declared transient_files ['save.json', '*.autosave.json']; the built code
      writes game_state.json, so the flush no-oped 22/22 times and 91% of
      play-tests resumed MID-GAME at an arbitrary room. One such eval resumed in
      Quiet Shrine, tried `go north` (invalid there), got the CORRECT refusal and
      filed a parser-failure report whose own summary states "the shrine only
      connects south". That false failure consumed 3 goal attempts and triggered
      the 586s diagnosis above, which concluded the code was fine.
      3. THE CONTAMINATION LEFT A MARK IN THE ARTIFACT. Two places in the tree
      carry a guard commented "so a `go <dir>` command can succeed and be
      observed by the harness" — harness-aware code written in response to our
      false failure. ALL THREE judges found it independently and flagged it
      unprompted; one called it the clearest statement of intent in the tree.
      The guarded code is unreachable (no room has zero connections).
    # ── INSTRUMENT FINDINGS (candidates for TIER_RUBRIC v1.2) ──
    instrument: >
      THE 3.1 FLOOR DESTROYS INFORMATION. Two of three votes floored 3.1 at 0/20
      and both said so unprompted: one summed to exactly -20 across nine root
      causes, the other to -24. A game that can be WON scores the same 0 as a
      program that never starts. Report the raw sum beside the floored score.
      THE PASS-LOCK BIASES HIGH. Vote 2 disclosed that its locked 6 was "the most
      generous defensible number": pass 1 characterised the save bug as item
      destruction (SILENT-WRONG, -4) and pass 2 revealed duplication, which is
      the rubric's own INVALIDATING wording (-6), and it declined to revise per
      section 9.1. It also left two confirmed-orphaned content rows unbilled for
      the same reason. Honouring the lock is correct; the rubric should say that
      a pass-2 recharacterisation gets RECORDED as a disclosure and that rejudges
      are told to expect it, which is exactly what this judge did unprompted.
```

---

## `laguna-s-2.1-apex.yaml`

```yaml
tier:
  league: contemplator  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: scored
  expectation:
    registered: 2026-07-29
    tier: "1"
    stars: null
    basis: experience
    outcome: near_miss_untested
    why: >
      The most variable model tested to date, with real reliability concerns.
      Expected tier 1 PROVIDED nothing goes wrong mid-run — which is the whole
      question with this model.
    outcome_note: >
      Scored 46 — four points under the >=50 line used to place within the 3-star
      band, so tier 2 by the letter and a near miss in substance. NOT counted as
      a falsification, on the operator's read (2026-07-31): the expectation named
      no reasoning mode, and this run had thinking OFF. For a quant whose defining
      trait is behavioural variability, a near-tier-1 non-thinking score is the
      reasonable outcome rather than a surprising one. The prediction is properly
      TESTED by a thinking:true variant; until that runs, treat this as
      consistent-with-expectation and unresolved, not as a hit or a miss.
  result:
    scored: 2026-07-31
    rubric: TIER_RUBRIC v1.2
    run: tier_20260731-050209 arm01   # game_challenge_tier, top_phase=quality, 2h
    thinking: false                   # SEE THE THINKING CAVEAT BELOW
    score: 46
    stars: "3"
    tier: "2"
    judges: [46, 46, 46]              # blind subagents, all claude-opus-5[1m]
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 120min (backstop), 13 files, 7 py_ok, 4 degenerations, 13/39 goals

    # ⚠️ SCORED UNDER PRE-FIX SAMPLING. This config declares `min_p: 0.0`, but
    # the loader's `gen.min_p or 0.05` discarded the legitimate zero, so the run
    # was actually served min_p 0.05. Fixed 2026-07-31 (AFTER this arm ran); the
    # arm was NOT re-run, so the score stands as measured under the regime below.
    #
    # Comparable to: arms 01-03 of the same sweep (identical pre-fix loader).
    # NOT strictly comparable to: arms 04+ of this sweep and anything scored
    # later, which get the declared min_p 0.0.
    #
    # Judged unlikely to move a band — min_p 0.05 vs 0.0 trims only the deepest
    # tail — but the decisive defects here were structural (a victory flag never
    # assigned, a parser/engine verb mismatch), and no sampling setting reaches
    # those. Recorded rather than assumed away.
    sampling_regime:
      status: pre_fix
      declared_vs_served: "min_p declared 0.0, served 0.05"
      fixed: 2026-07-31 (llama_cpp_backend._build_generate_kwargs)
      rerun: not_done (operator decision — annotate, do not re-run)

    # Per-dimension stars are score/max banded per §2 (see below), so different maxima
    # BANDS (rubric §2, inclusive at the top): 0-20=1, 21-40=2, 41-60=3,
    # 61-80=4, 81-100=5. So 40%% is 2 stars, 60%% is 3, 80%% is 4.
    # normalise and the SHAPE reads at a glance — same convention as the
    # gpt-oss-120b-a5-swarm-524k block.
    #
    # WHICH JUDGE. Judges 2 and 3 returned IDENTICAL dimension vectors, so this
    # is the modal shape (2 of 3), not an arbitrary pick. Judge 1 differed on
    # exactly two rows and they offset: no_broken_functions 3 (not 4) and
    # reusability 8 (not 7). All three totalled 46.
    dimensions:
      no_broken_functions: {score: 4, max: 20, stars: 1}   # 8-10 ledger rows
      robustness:          {score: 2, max: 10, stars: 1}   # one HOST-DAMAGING edge
      ux:                  {score: 6, max: 10, stars: 3}
      conformance:         {score: 9, max: 10, stars: 5}   # 46/53 requirements
      ambition:            {score: 3, max: 10, stars: 2}
      creativity:          {score: 5, max: 10, stars: 3}
      org_project:         {score: 4, max: 5,  stars: 4}
      org_logic:           {score: 3, max: 5,  stars: 3}
      reusability:         {score: 7, max: 10, stars: 4}
      documentation:       {score: 3, max: 10, stars: 2}   # hard-capped, §3.10

    profile: >
      The widest conformance-to-delivery gap recorded: 5 stars on doing what was
      asked, 2 stars on it working. Strong on structure, modifiability and
      requirements coverage; the artifact is authored well and assembled wrong.
      A judge's own phrasing — "each module is individually clean and almost
      none of them agree with each other."

    decisive_defect: >
      THE GAME CANNOT BE WON, and no single edit fixes it. `game_won` is never
      assigned anywhere in the tree, so the victory branch is unreachable dead
      code — proven by PLAY, not inferred: all three judges independently cut
      the boss's HP by data edit, killed it, and got the ordinary prompt back.
      Three further blocks stack underneath, each sufficient on its own: the
      boss-weakness item is authored only as the boss's own drop (obtainable
      only after the fight it exists to enable), the sole armour declares a slot
      the player does not have, and `equip` never applies anything regardless.
      The DEFEAT path, by contrast, is complete and fires correctly.

    strengths: >
      The prose and the data design are real. Nine rooms of specific,
      atmospheric writing; a two-NPC hint chain that is load-bearing rather than
      decorative; and a genuine data/engine separation — new rooms and items are
      a world.yaml-only change with ZERO Python edits. Every modification probe
      landed first try for every judge.

    unmet_requirements: [17, 28, 29, 36, 37, 39, 42]  # no flee verb; no mid-combat
                                                      # item use; 2 regular monsters
                                                      # not 3; weakness item in no
                                                      # room; NO WIN PATH; no restart
                                                      # after death; save omits all
                                                      # world state
                                                      # (judge 3 cited 41 for the last)

    inter_judge: >
      Three blind judges, three totals of 46. Only two of ten dimensions varied
      and they offset. All three scored conformance 46/53 and named six of the
      same seven unmet items. This is v1.2's first three-vote outing since §3.1
      was made multiplicative — the amendment written because the additive
      scheme had floored a winnable artifact at 0/20 — and the dimension
      returned 3/4/4 on an artifact carrying 8-10 ledger rows. It behaved.
```

And the free-floating commentary that followed it in the config:

```
# ── THE EXPECTATION, AND WHERE IT ACTUALLY STANDS ────────────────────
# Registered tier 1, scored 46 = tier 2 — four points short, with thinking OFF.
# Read as CONSISTENT WITH EXPECTATION AND UNRESOLVED, not as a miss: a
# near-tier-1 non-thinking score is the reasonable result for a quant whose
# defining trait is behavioural variability, and the prediction named no
# reasoning mode. The thinking:true variant is the run that settles it.
#
# What the pre-registration DID surface is that the failure mode was not the
# one it feared. It said "tier 1 PROVIDED nothing goes wrong mid-run." Things
# did go wrong mid-run — four degeneration events — and the run still produced
# more files than any other arm in the sweep to that point. The artifact was
# limited by something the expectation never contemplated: it does not work.
#
# 3★ (40–59) is the rejudge band, so this is a MEDIAN OF THREE independent
# blind judgments, per §5. All three returned 46. Only two of ten dimensions
# varied at all (§3.1 3/4/4, §3.9 8/7/7) and they offset. Conformance came back
# 46/53 from all three. That agreement is evidence about the instrument as much
# as the artifact — it is v1.2's first three-vote outing since §3.1 went
# multiplicative, and the dimension it was rewritten to fix behaved.
#
# The decomposition IS the result: conformance 9/10 beside no-broken-functions
# 3–4/20. The model did nearly everything the brief asked and delivered almost
# none of it.
```

---

## `laguna-s-2.1.yaml`

```yaml
tier:
  league: contemplator
  status: not_yet_run
  thinking: true            # THE PROPERTY THIS CONFIG SCORES
  expectation:
    registered: 2026-08-02
    basis: think-hold mechanism probe (one deflected close -> clean CoT)
    outcome: pending
    why: >
      PRE-REGISTERED. With the reasoning channel genuinely open (think-hold
      defeats the persona's no-think veto), the untagged content-channel
      deliberation that defined every prior laguna-S run should move INTO
      think blocks where budget/strip machinery can see it. Expected shifts
      vs the apex thinking:off record (48/100): fewer content-channel
      mega-turns, deliberation visible as reasoning_tokens, and — if the
      XS single-probe result generalizes — better decision quality. The
      2026-07-26 unsloth thinking-on failure (CoT circling to the cap,
      never closing) is the regression to watch; it predates top_k=20,
      think-hold, and ran a different quant.
```

---

## `laguna-xs-2.1.yaml`

```yaml
tier:
  league: contemplator  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: not_yet_run
  quant: BF16 (poolside)   # THE PROPERTY THIS CONFIG SCORES
```

---

## `mistral-medium-3.5-128b.yaml`

```yaml
tier:
  league: contemplator  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: recorded
  rubric: TIER_RUBRIC v1.2
  stars: 3                 # 41-60 band
  tier: 2                  # median 49, below the >=50 line
  expectation:
    registered: 2026-07-29
    tier: "2"
    stars: null
    basis: experience
    outcome: hit
    why: >
      A dense, competent model that is extremely slow. Expected tier 2 on
      throughput grounds: limited revisions within the window, and a
      correspondingly low goal count.
    outcome_note: >
      HIT on the placement, WRONG on the mechanism. Tier 2 was predicted from
      SPEED — and speed was indeed the fleet's worst (~6 tok/s, one generation
      ran 24 minutes). But throughput is not what cost it: the arm produced 12
      files with ZERO syntax failures and 12/37 goals, a clear improvement on
      its smoke run. It lands at tier 2 because the thing it built cannot be
      played, not because it built too little.

  judged:
    run: tier_20260731-050209 arm10 · staged/arm10
    judge_model: claude-opus-5[1m]
    votes: [45, 49, 49]
    total: 49              # MEDIAN of three independent blind judgements
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 120 min (backstop) · 12 files · 7 py_ok · 0 py_fail · 0 degen · 12/37 goals

    # Judges 2 and 3 independently produced IDENTICAL §3.1 arithmetic
    # (0.343672 -> 7/20) from separately constructed ledgers. Judge 1 billed the
    # seam at 5 rather than 7; that one dimension is the whole 45-vs-49 spread.
    dimensions:
      no_broken_functions: {score: 7, max: 20, stars: 2}
      robustness:          {score: 5, max: 10, stars: 3}
      ux:                  {score: 5, max: 10, stars: 3}
      conformance:         {score: 9, max: 10, stars: 5}   # 46/53
      ambition:            {score: 3, max: 10, stars: 2}
      creativity:          {score: 5, max: 10, stars: 3}
      org_project:         {score: 4, max: 5,  stars: 4}
      org_logic:           {score: 2, max: 5,  stars: 2}
      reusability:         {score: 7, max: 10, stars: 4}
      documentation:       {score: 2, max: 10, stars: 1}

    decisive_defect: >
      THE ROOM-GATE / ATTACK DEADLOCK. Movement refuses entry to any room whose
      monster is not yet defeated; `attack` only engages a monster in the room
      you are STANDING IN; and world.yaml places every monster INSIDE the room
      it guards. The two conditions are mutually exclusive, so the game
      announces "A Goblin blocks the way!" and then answers "There is nothing to
      attack here." No phrasing opens it — judges tried 11+ attack forms, every
      direction in long, short and bare-word form, with and without the weapon.
      Sealed behind it: all three monsters, the two-phase boss, the weakness
      mechanic, the victory screen, the defeat screen, the armour, and FIVE of
      the eight authored rooms. What ships is a three-room walking simulator.

    the_second_cause: >
      Both rejudges disclosed a SECOND, independent fatal defect hidden behind
      the seam: `combat.py` references `EquipmentSlot` and never imports it, so
      `monster_turn()` raises NameError unconditionally. Verified by diagnosis
      (calling it directly; artifact untouched). Repairing the movement seam
      alone would NOT produce a working fight — every combat would crash the
      process on the monster's first counter-attack. "Monsters fight back" is
      dead on arrival, not merely unreachable.

    weakness_item_inversion: >
      Fifth distinct miswiring of this mechanic in the sweep. Judge 3 forced
      entry by hand-writing a save.json and using the artifact's OWN `load`
      command (diagnosis through a supported interface, no repair): there,
      `use silver arrow` INSTANT-KILLS the 100 HP two-phase boss in one command
      taking zero damage, while the honest route tracebacks on exchange one. The
      weakness item is not merely overpowered — it is the ONLY path that works.

    not_a_false_victory: >
      Worth keeping distinct from the 2026-07-29 gpt-oss case. The victory
      screen here is CORRECTLY GUARDED — it fires only on a genuine final_boss
      defeat. The failure is the inverse: a terminal state written properly and
      made completely unreachable. Those score similarly and are opposite
      defects.

    unmet_requirements: [23, 29, 32, 39, 42, 45, 51]  # status omits location;
                                                      # 2 regular monsters not 3;
                                                      # `behavior` authored on all
                                                      # three and never read
                                                      # anywhere; no restart on
                                                      # defeat; save omits room
                                                      # state; 4 items not 5;
                                                      # dialogue linear, not
                                                      # branching

    also_broken_in_play: >
      `status`/`inventory`/`inv`/`stats` all CRASH the process (Narrator.
      show_status called with 3 args, takes 4) — so health, attack, defense and
      inventory are NEVER observable at any point in play. `save` crashes
      whenever anything is equipped (EquipmentSlot enum keys are not
      JSON-serialisable) and leaves a truncated file that the next `load`
      reports as "No saved game found". Unrecognised input falls through to
      LOOK, so a typo is indistinguishable from a valid command.
```

---

## `olmo-3.1-32b-instruct.yaml`

```yaml
tier:
  league: contemplator  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: recorded
  rubric: TIER_RUBRIC v1.2
  stars: null               # tier 3 — the gate terminates judgment before stars
  tier: 3

  expectation:
    registered: 2026-07-29
    tier: "3"
    stars: null
    basis: experience
    outcome: hit
    why: >
      Similar to the think variant but notably under-performant. SKIPPING IS
      RECOMMENDED as a solo arm.

      The olmo run worth doing instead: load BOTH models and statically route
      thinking turns (medium, high) to think and everything else to instruct.
      Tier advancement is plausible in that configuration and not in this one.
    scored: >
      HIT, exactly as registered: TIER 3 on the §2 gate.

  judged:
    run: tier_20260731-050209 arm17 · staged/arm17
    rubric: TIER_RUBRIC v1.2
    judge_model: "claude-opus-5[1m]"
    tier3_gate: failed
    scored: false        # gate failure terminates judgment (§2, §9.2); single run sufficient
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 126 min (backstop) · 19 files · 7 py_ok · 0 py_fail · 0 degen · 10/29 goals · 31 work cycles

    what_failed: >
      THE GAME LOOP WAS NEVER WRITTEN. `GameEngine.run_game_loop()` is
      `while self.game_state == "playing": pass` with the comment
      "(Command handling implementation would go here)", and the parser's own
      read loop is commented out. The entry point renders a real title screen,
      consumes one Enter, prints "Welcome to the Game ..." and then busy-spins
      at 99% CPU, accepting zero commands, until killed. The judge tried every
      route before concluding: flags, abbreviations, id-forms, each module as
      its own entry point, the declared console script, a hand-built save.
      Faulthandler stack pins the spin at run_game_loop.

    not_a_framework_fault: >
      Checked against the §20 extraction bug and the run log: 19 files written,
      0 py_fail, everything parses — the loop stub is the MODEL's shipped
      content, comment and all, not a truncation. (The README truncation
      affected this arm like all others, but a README cannot fail this gate.)
      Genuine model non-delivery; single run is sufficient per §2.

    dead_symbol_precheck_validated: >
      The operator-side §18 precheck flagged 11 dead symbols BEFORE judging —
      show_victory, show_defeat, show_combat_turn, parse_command, both save/load
      functions — the largest core-loop dead set of the sweep, on the artifact
      whose engine calls none of them. The signal predicted the verdict class
      exactly: four working UI screens with no route to any of them, a parser
      nothing invokes. Strongest single validation of the dead-symbol signal to
      date, in both directions (step37: 0 dead symbols, scored 62).

    advisory_ledger: >
      Reported by the judge for the record, NOT the headline result (§9.2):
      advisory total 31 = 2-star band. Notables inside it: conformance 39/53 —
      the CONTENT half of the brief is nearly complete (8 rooms, 5 items, 4
      monsters with distinct behaviour tags, a topic-keyed NPC hints table that
      is genuinely nice data design) while the MECHANICS half is absent, not
      stubbed. Robustness advisory 1/10 with the judge's sharpest line: the
      happy path itself is the HOST-DAMAGING edge — a silent core-eating spin
      that only kill ends. "That is worse than a crash, because a crash tells
      you." Also: even if the loop existed, load_world('world.yaml') returns
      rooms=0 items=0 WITHOUT ERROR — the file nests under a `world:` key the
      loader does not read, so the entire authored world is dead data behind a
      one-key mismatch. Two model layers, two save schemas that reject each
      other's files, two entry-point functions, five inconsistent packaging
      manifests. Several modules that were never introduced to each other.
```

---

## `olmo-3.1-32b-think.yaml`

```yaml
tier:
  league: contemplator  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: recorded
  rubric: TIER_RUBRIC v1.2
  stars: null               # tier 3 — no artifact; nothing to judge
  tier: 3

  expectation:
    registered: 2026-07-29
    tier: "3"
    stars: null
    basis: experience
    outcome: hit
    why: >
      Basic, and occasionally surprising. Long CoT and slow generation limit
      expectations to tier 3.
    scored: >
      HIT, by the exact predicted mechanism: long CoT and slow generation.

  judged:
    run: tier_20260731-050209 arm18 · NOT STAGED (zero files)
    judge_model: "claude-opus-5[1m] (operator-side diagnosis; no blind judge spawned)"
    tier3_gate: failed
    scored: false
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 122 min · 4 work cycles · 0 files · 0/30 goals · 0 degen

    what_happened: >
      THE MODEL NEVER EXITED DELIBERATION. Four work cycles in 122 minutes
      (~2/h — off the bottom of the contemplator band, field minimum 7.5/h).
      The batch-creation generation ran 63,876 characters of pure planning
      prose — ZERO fenced blocks, zero code — repeatedly re-scoping itself
      ("Due to the character limit, I will provide a very minimal version...
      But the task requires at least 8 rooms and 5 items. I will generate the
      files with the 8 rooms and 5 items.") and ended, at the generation
      limit, with the words "Let's code." The serial fallback then had no
      budget left. 19,469 tokens spent on the batch, nothing extractable.

    attribution: >
      MODEL, with two framework aggravations that did not change the outcome.
      Model: the CoT death spiral above — even with a perfect framework, 64k
      characters of deliberation ending at "Let's code." ships nothing. No
      think-tag residue reached extraction (the FSM mapping held); what arrived
      was genuinely prose. Aggravations, recorded for the framework ledger:
      (1) 8 GraphQL "all inference instances busy (active=1, limit=1)" errors,
      2 persisting past 4 retries, in a SERIAL sweep where nothing else should
      hold the seat — consistent with the known watchdog-cancel seat-leak;
      (2) one "Architecture validation failed: 'str' object has no attribute
      'get'" coercion failure — root-caused 2026-08-02 to the contract-list
      element loops (a str element in interfaces/data_shapes/state_shapes
      discarded the whole blueprint) and FIXED with per-element salvage; see
      tests/test_arch_salvage_and_origin.py. Neither converts 0 files into an
      artifact, so
      NO RERUN IS OWED on framework grounds; single run suffices per §2.

    v2_note: >
      A useful datapoint for the v2 league design: this is the contemplator
      failure mode at its limit — a model whose deliberation does not converge
      within ANY practical budget. The v2 smoke would have called this in
      minutes for free; under v1.2 it consumed a full 2h arm to learn it.
```

---

## `qwen3-next-coder-80b-a3.yaml`

```yaml
tier:
  league: grinder  # measured cyc/h — see dev/blind_panel/LADDER.md; v2 re-confirm 54.2 cyc/h
  v2:
    status: placed
    rubric: TIER_RUBRIC v2
    epoch: "v2.0 (brief v2 / framework ec5d54c)"
    tier: 2               # lost to GUARDIAN 0-5; FLOOR flight pending
    judged:
      run: tier_20260802-043526
      staged: staged/arm02
      judge: "claude-opus-5 (blind subagent, flight packet)"
      flights:
        - opponent: GUARDIAN (gpt-oss anchor)
          result: "LOST 0-5 axes + overall, no self-flag"
          conformance: "35/47 SIGNIFICANTLY-DEVIATED (BOTH triggers: 12 unmet >= 10 AND the boss phase/weakness mechanics never written)"
          completability: "UNWINNABLE — no terminal state REACHABLE at all: 8 rooms authored in TWO disconnected components (boss/key/dagger all in the far one), zero monsters placed in the reachable four, so combat can never begin and the player can never die"
          why_the_judge_preferred_guardian: >
            Working surface: every one of the Guardian's 14 advertised
            commands executes outside combat and its combat/equipment/
            dialogue/persistence subsystems all demonstrably run; this
            artifact's boss/NPC/save layer never executes once — including
            a wholly dead 244-line commands.py imported by nothing that
            would KeyError on first call, and an `examine` that consumes
            input and prints nothing. State: the Guardian genuinely
            round-trips across processes (with its known resurrection and
            auto-load defects); this artifact has no reachable persistence,
            its shipped load_game fails on the very file save_game writes
            ("Error loading game: 'inventory'"), and restart resets the
            player while leaving the world permanently stripped.
            Experience: natural item names, accurate help, case-insensitive
            input and blow-by-blow narration vs raw ids ("You see:
            potion_1"), case-sensitivity, and nothing whatever to do in
            four empty rooms. Scope: six connected rooms with working
            combat/equipment/persistence vs movement-take-inventory-equip
            and zero fights. Ambition: comparable paper reach (dual command
            layer, dialogue graph, victory/defeat screens) but almost none
            of it survived — half the rooms orphaned, no monster placed,
            the advertised silver weakness never written.
          notes: "112 cycles / 124min = 54.2 cyc/h (grinder confirmed). Clean 6/6 batch at cycle 1 (the §20 doc-bug fix hypothesis holds — batches push through now). Judge 5's Guardian tally 42/47 (item 33 scored met) vs the prior four judges' 41/47 — ±1 divergence, decisive findings identical"
        - opponent: FLOOR (devstral anchor)
          result: "LOST 4-1 axes (took state integrity only), overall FLOOR, no self-flag — places BELOW the floor anchor: low tier 2"
          why_the_judge_preferred_floor: >
            Working surface: the FLOOR's 12 of 13 advertised verbs work live
            with only `talk to` dead; this artifact is missing
            drop/talk-to/flee/status entirely, its examine is a silent
            no-op, and combat is never invocable because no monster is
            placed in any room. Experience: the FLOOR names things, ships
            honest categorized help, a real status block, case-insensitive
            input and narrated combat with damage numbers — vs raw ids,
            "You don't have ." blank-noun errors, and a status command the
            game's own vocabulary rejects. Scope: two fought-and-killed
            monsters, mid-combat equip/heal/flee and a reachable ending vs
            four rooms with no combat, no NPC, no reachable terminal state.
            Ambition: the FLOOR attempted more AND most survived; this
            artifact's extra reach (orphaned half-map, second command
            module) survived not at all. The one axis won — state
            integrity — was decided on full-schema serialization plus a
            live round-tripping restart, i.e. largely on code no player can
            invoke. Shared headline failure: BOTH artifacts strand their
            authored boss in a disconnected component; the FLOOR delivers a
            playable narrated loop up to that wall, this artifact never
            reaches a single fight.
  status: not_yet_run
  expectation:
    registered: 2026-07-29
    tier: "2-1"
    stars: null
    basis: experience
    why: >
      The default in early Ouroboros development, superseded by gpt-oss for its
      more tractable CoT. Widely regarded as the best native non-thinking coding
      model. Fast and reliable with outstanding attendance to context; depth of
      reasoning limited by only 3B active parameters and no thinking. Expected
      high tier 2 / low tier 1.
    outcome: hit
    outcome_note: >
      HIT, at the pessimistic end. Predicted "high tier 2 / low tier 1"; scored
      49 — one point under the tier-1 line, with votes 45/49/51 straddling it.
      The reasoning was also right about the mechanism: fast and reliable with
      good context attendance (11 files, 0 py_fail, 0 degenerations, all Python
      parses), and the failure is a depth-of-reasoning one rather than a
      throughput one.

  judged:
    run: tier_20260731-050209 arm12 · staged/arm12
    judge_model: claude-opus-5[1m]
    votes: [45, 49, 51]
    total: 49              # MEDIAN of three independent blind judgements
    stars: 3               # 41-60 band
    tier: 2                # §5: the side taken by >=2 of 3 (two votes <50)
    rubric: TIER_RUBRIC v1.2
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 121 min (backstop) · 11 files · 6 py_ok · 0 py_fail · 0 degen · 11/33 goals

    # Dimensions are judge 2's vector — a judge whose total equals the recorded
    # median. Stars: score/max as a percentage through the §2 band table
    # (0-20=1, 21-40=2, 41-60=3, 61-80=4, 81-100=5), inclusive at the top.
    dimensions:
      no_broken_functions: {score: 5, max: 20, stars: 2}
      robustness:          {score: 7, max: 10, stars: 4}
      ux:                  {score: 4, max: 10, stars: 2}
      conformance:         {score: 9, max: 10, stars: 5}   # 46/53
      ambition:            {score: 3, max: 10, stars: 2}
      creativity:          {score: 5, max: 10, stars: 3}
      org_project:         {score: 3, max: 5,  stars: 3}
      org_logic:           {score: 3, max: 5,  stars: 3}
      reusability:         {score: 8, max: 10, stars: 4}
      documentation:       {score: 2, max: 10, stars: 1}

    decisive_defect: >
      THE GAME BOOTS INTO A SEALED ROOM. `world.yaml` declares
      `start_room: storage_room`, and that room has `exits: {}` with nothing
      linking to it. Every direction word, abbreviation and phrasing is refused
      — judges tried up/down/in/out, bare n/s/e/w, walk/move/go to, enter, leave
      and ~60 movement attempts between them. From the shipped entry point the
      reachable game is two items in a closet: 8 of 9 rooms, both NPCs, all
      three monsters, the lair, the weapon, the armour and both terminal states
      are unreachable.
      THE ARTIFACT CARRIES THE EVIDENCE AGAINST ITSELF: a stray
      `player_state.json` at the tree root, referenced by no code, reads
      `location: mossy_clearing` — the real hub — and `storage_room` is the only
      one-line description among eight richly written ones. A one-line data fix
      separates a closed closet from a substantially working game.

    second_decisive: >
      Fixing that line would not make it completable. There is NO WIN CONDITION
      ANYWHERE — `grep -riE 'victor|congratul|you win'` returns nothing, there is
      no boss entity in code or data, and the room the world calls "Boss's Lair"
      is guarded by a SECOND PLACEMENT of a regular monster. All three judges
      killed that occupant and got "You defeated Spectral Hunter!" and a prompt.

    shared_mutable_monsters: >
      A defect class new to this sweep, found in pass 2 by two judges
      independently: Monster objects are world-level SINGLETONS and
      `spectral_hunter` is placed in TWO rooms. Verified in play — three hits on
      the grove Hunter left the lair occupant at Health 26/35. Killing the grove
      one would leave the "boss" dead on arrival.

    equipment_is_inert: >
      Measured by ALL THREE judges as a controlled A/B, with identical numbers:
      3 damage dealt and 8 taken, with and without the Rusted Cutlass
      (attack_bonus 3) and Leather Jerkin (defense_bonus 2) equipped.
      `total_attack()` reads `equipment.get("weapon_bonus")` from a dict that
      only ever holds {"weapon", "armor"}, so the authored bonuses are dead.
      The weakness path is dead too: it compares the equipped WEAPON id against
      `monster.weakness`, while the Moonpetal Amulet is `type: key_item` and
      `_handle_equip` refuses to equip one. The healing potion is the only item
      in the game that does anything. SEVENTH consecutive artifact whose
      weakness item is miswired.

    unmet_requirements: [17, 32, 33, 34, 37, 39, 42]  # no flee anywhere; all
                                                      # three monsters run one
                                                      # identical combat script
                                                      # differing by one integer;
                                                      # no boss entity; no phase
                                                      # data or logic; no win
                                                      # path; no restart on
                                                      # defeat; save omits room
                                                      # contents and monster HP

    hygiene_note: >
      `save`/`load` do no path sanitisation (`f"{save_dir}/{filename}"` plus
      mkdir(parents=True)), so `save ../../x` would write and create directories
      outside the workspace. All three judges contained the probe to their own
      scratch and reported what it WOULD do rather than aiming it anywhere real.


      FAMILY: the qwen hybrid architecture makes advanced features — cache strategies, instance swarming — markedly harder to adapt than a plain transformer. Applies to every qwen arm.

  # ── SAME-MODEL VARIABILITY FLIGHT, 2026-08-05 ──────────────────────
  # Not a placement. The v2 entry above stands; this is a three-artifact
  # comparison of THIS model against itself, to pick the best artifact and
  # measure how much its runs vary.
  variability_2026_08_05:
    instrument: dev/blind_panel/SINGLE_MODEL_FLIGHT_PROMPT.md
    rubric: TIER_RUBRIC v2.1
    blind_root: qwen3next_variability_20260805-123200
    judge: "claude-opus-5 (blind subagent)"
    # SINGLE JUDGE — order is provisional, no cross-judge convergence.
    # METHODS §3 requires three; one is legitimate for a same-model
    # variability read but MUST NOT be promoted into a ladder placement.
    panel: single_judge
    artifacts:
      - {label: gamma, run: tier_20260805-092309, staged: staged/arm01,
         rank: 1, cycles: 57, minutes: 120, goals: "18/35",
         conformance: "42/47 NEAR-FULL"}
      - {label: alpha, run: tier_20260802-043526, staged: staged/arm02,
         rank: 2, cycles: 111, minutes: 124, goals: "13/29",
         conformance: "36/47 SIGNIFICANTLY-DEVIATED"}
      - {label: beta, run: tier_20260804-141701, staged: staged/arm02,
         rank: 3, cycles: 29, minutes: 121, goals: "8/29",
         conformance: "37/47 SIGNIFICANTLY-DEVIATED"}
    panel_a_order: "gamma > alpha > beta (firsts 3/1/0)"
    panel_b_order: "gamma > beta > alpha (firsts 5/1/0)"
    overall: "gamma > alpha > beta — no panel split; SELF-FLAG CLOSE alpha/beta"
    best_artifact: tier_20260805-092309/staged/arm01
    all_three_unwinnable: true
    finding: >
      EVERY arm was stopped by a cross-module seam bug — an identifier or
      contract mismatch between two files each internally reasonable. What
      separates them is DEPTH IN THE DEPENDENCY CHAIN, not ambition:
      gamma's sits at the leaf (main.py calls engine.handle_combat_input,
      which does not exist; the real method is _handle_combat_command), so
      a genuine two-phase boss with weakness double-damage and armour
      mitigation sits FINISHED behind it. alpha's are mid-chain (a dead
      244-line commands.py imported by nothing, a loader that never
      populates the `monsters` key the engine reads) plus a world graph
      authored in two disconnected components. beta's is at the ROOT — its
      loader returns raw dicts where every consumer does attribute access —
      so it dies before the first prompt and answers zero commands ever.
    cycles_do_not_predict_quality: >
      alpha ran 111 cycles, nearly TWICE gamma's 57, and produced the worse
      artifact. All three authored exactly 11 files in ~120 minutes. So
      neither cycle count nor file count tracks the panel here; goal
      fraction happened to (0.51 / 0.45 / 0.28, the ranking order) but
      METHODS §"Why blind play" documents counters INVERTING against play,
      so one agreeing flight is a datapoint, not a vindication.
    evidence_of_run_shape: >
      Each artifact shipped a tell the judge read independently, and they
      line up with the depth finding. beta ships loader_backup.py — the
      OLDER, contract-correct version of the file that breaks it — reading
      as a refactor that regressed the return type and was never
      re-verified end to end. gamma ships a save.json, a runtime artifact
      that can only exist because the program was actually RUN (the packet
      builder flagged it as contemporaneous with the sources, i.e. the
      model's own, not smoke residue). alpha ships a fully written,
      never-imported commands.py — an abandoned design direction neither
      wired up nor removed.
```

---

## `qwen3.5-122b-a10.yaml`

```yaml
tier:
  league: contemplator  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: recorded
  rubric: TIER_RUBRIC v1.2
  stars: 3                  # 41-60 band; 51/100
  tier: 1                   # >= 50, and UNANIMOUS — all three votes >= 50

  expectation:
    registered: 2026-07-29
    tier: "1"
    stars: null
    basis: experience+benchmarks
    outcome: hit_on_tier_miss_on_order
    why: >
      The strongest qwen by benchmark. Expected to score somewhat above gpt-oss
      within tier 1, but generally close — a good combination of speed under fire
      and competence, with slightly verbose thinking.

      FAMILY: the qwen hybrid architecture makes advanced features — cache strategies, instance swarming — markedly harder to adapt than a plain transformer. Applies to every qwen arm.
    scored: >
      TIER CORRECT, ORDER WRONG. Landed tier 1 as predicted, and "generally
      close" held — but BELOW gpt-oss, not above it (51 vs 60). The
      speed-under-fire half of the prediction was right: 25 files, the largest
      artifact of the sweep, 0 py_fail, 0 degenerations, everything parses.
      The gap is not competence in the authoring sense — conformance 9/10,
      49-50 of 53 requirements met by all three judges — it is that one edit
      severed the artifact's only path into half of itself.

  judged:
    run: tier_20260731-050209 arm13 · staged/arm13
    rubric: TIER_RUBRIC v1.2
    judge_model: "claude-opus-5[1m]"
    votes: [50, 55, 51]
    score: 51          # MEDIAN of three independent blind judgements (§5)
    stars: 3           # 41-60 band
    tier: 1            # by score, >= 50 — and unanimous: all three votes >= 50
    tier3_gate: passed
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 122 min (backstop) · 25 files · 9 py_ok · 0 py_fail · 0 degen · 19/37 goals

    # No two judges returned identical vectors, so there is no modal shape to
    # record. This is the MEDIAN JUDGE's own vector (the 51), which has the
    # property that it sums to the recorded score — a synthesised per-dimension
    # median would not (it sums to 52).
    # Stars are score/max banded per §2: 0-20=1, 21-40=2, 41-60=3, 61-80=4,
    # 81-100=5, inclusive at the TOP. So 40% is 2 stars and 80% is 4.
    dimensions:
      no_broken_functions: {score: 5, max: 20, stars: 2}   # votes 6/9/5
      robustness:          {score: 6, max: 10, stars: 3}   # votes 6/5/6
      ux:                  {score: 5, max: 10, stars: 3}   # votes 5/6/5
      conformance:         {score: 9, max: 10, stars: 5}   # UNANIMOUS 9 — 49-50/53
      ambition:            {score: 4, max: 10, stars: 2}   # votes 3/4/4
      creativity:          {score: 5, max: 10, stars: 3}   # UNANIMOUS 5
      org_project:         {score: 4, max: 5,  stars: 4}   # UNANIMOUS 4
      org_logic:           {score: 3, max: 5,  stars: 3}   # UNANIMOUS 3
      reusability:         {score: 7, max: 10, stars: 4}   # votes 6/7/7
      documentation:       {score: 3, max: 10, stars: 2}   # UNANIMOUS 3, hard-capped §3.10

    profile: >
      THE SWEEP'S LARGEST ARTIFACT (25 files) AND ITS SHARPEST DECOMPOSITION:
      conformance 9/10 against delivery 5/20. A well-built ten-room exploration
      game with its combat half amputated at a single line. Everything reachable
      works cleanly — movement, inventory, equipment with correct stat deltas,
      examine, save/load, help, status — with wrapped prose, ruled headers and a
      legible status panel. All three judges reached the Boss Chamber in two
      moves holding the boss's weakness item and could not start the fight.

    decisive_defect: >
      `attack` and `flee` are recognised verbs with NO branch in
      `dispatch_command`, and `GameEngine.initiate_combat()` has ZERO CALLERS in
      the shipped tree. The game instructs the player to type a command it then
      rejects:

          A Dark Lord blocks your path!
          Type 'attack' to engage the enemy.
          > Unknown command: attack

      Everything downstream is dead: three monsters, the two-phase boss, the
      silver-weakness double-damage path, and fully written victory screen,
      defeat screen and restart branch. Player health never moved off 50/50 in
      any of three independent sessions.

    it_shipped_the_fix_beside_the_regression: >
      `adventure/engine.py-e` (02:30) is a pre-edit copy of the engine; the
      shipped `engine.py` (03:07) is newer. In the backup `handle_movement`
      calls `self.initiate_combat(next_room.monster)`. The shipped version
      replaced that ONE line with two display_message calls telling the player
      to type `attack` — a verb never added to the dispatcher.
      JUDGE 3 RESTORED THAT LINE and immediately got a real fight, working
      phase machinery, the weakness bonus and a real VICTORY banner. This is
      causal proof, not inference: one line separates this artifact from a
      completable game.

      PROVENANCE: the same edit also renamed `for i,` to `for _i,` in three
      loops, and the recorded diagnosis was "Two enumerate() loops in
      adventure/engine.py use..." — a cosmetic LINT fix, localized to
      `symbol GameEngine` (the whole class), whose rewrite silently deleted the
      combat call while satisfying the complaint it was dispatched for. The
      seam gate then logged "clean — 0 unreachable seam(s)". See OPEN_TASKS §18:
      the gate's own reachability pass lists initiate_combat as dead (1 of only
      2 dead symbols) and the result is discarded.

    other_findings: >
      - SAVE/LOAD DUPLICATES THE WORLD (all three judges, INVALIDATING): the
        save has no room-contents section, so rooms are rebuilt from YAML and
        the saved inventory is overlaid. Carry an item through a load and you
        hold two; drop one elsewhere and it teleports home. Unbounded.
      - `use` destroys any `consumable` for zero effect, including the torch,
        with no full-health refusal.
      - Two SESSION-ENDING edges: EOF/Ctrl-D anywhere (unguarded `input()`), and
        a shape-valid corrupt save that prints "Game loaded successfully!" and
        then dies on KeyError.
      - The attack bonus is DOUBLE-COUNTED in the damage path — found
        independently by all three judges: `recalculate_stats` bakes the weapon
        into `player_stats["attack"]`, then `process_turn` adds
        `equipped_weapon.effect["attack"]` again.
      - Dead authored data: `player_condition` dialogue gates, room `triggers`,
        `flags`, `behavior`, and a `rusty_key` with `key_for: vault` placed in
        no room.

    unmet_requirements: >
      49-50 of 53 met. Agreed unmet across judges: 28 (mid-combat healing is
      stubbed in code, not merely unreachable), 29 (three REGULAR monsters —
      only two exist; dark_lord is the boss), and the world-state item (42/41 —
      room-content changes are persisted nowhere, the root of the duplication
      bug). One judge also billed 22 (help advertises attack/flee) and one
      billed 32 (per-monster `behavior` is read by no code).

    instrument_note: >
      THE TIGHTEST DISAGREEMENT-PATTERN THIS INSTRUMENT HAS PRODUCED. Five of
      ten dimensions came back IDENTICAL across three strangers (conformance 9,
      creativity 5, org_project 4, org_logic 3, documentation 3), and the whole
      5-point spread is concentrated in `no_broken_functions` (6/9/5) — which is
      not a disagreement about FACTS but about SEVERITY PRICING of the same
      defect. All three filed a §4.1 disclosure on that exact row, and all three
      predicted a rejudge would land in a narrow band. They diverged precisely
      where each said they were unsure, and agreed everywhere else.
```

---

## `qwen3.6-27b.yaml`

```yaml
tier:
  league: contemplator  # measured cyc/h — see dev/blind_panel/LADDER.md
  status: recorded
  rubric: TIER_RUBRIC v1.2
  stars: 3                  # 41-60 band; 53/100
  tier: 1                   # >= 50, and UNANIMOUS — all three votes >= 50

  expectation:
    registered: 2026-07-29
    tier: "2-1"
    stars: null
    basis: experience+benchmarks
    outcome: hit
    why: >
      Surprisingly close to the 122b on benchmarks. Seems to prefer shorter
      generations and a fallback to serial is expected; in that regime it took
      the champion position, though expect this to wane now the field can batch.
      Fairly slow and verbose thinking with strong reasoning. Expected high
      tier 2 / low tier 1.

      FAMILY: the qwen hybrid architecture makes advanced features — cache strategies, instance swarming — markedly harder to adapt than a plain transformer. Applies to every qwen arm.
    scored: >
      HIT, at the predicted boundary: 53 is low tier 1, exactly "high tier 2 /
      low tier 1". The "surprisingly close to the 122b" clause landed harder
      than registered — this 27B model did not merely come close to
      qwen3.5-122b-a10, it EDGED IT (53 vs 51) on the same brief, same rubric,
      same day, with roughly a quarter the parameters. Note both artifacts
      failed the same way (one un-integrated function gating the entire combat
      half), so the comparison is between two near-identical failure shapes and
      should not be read as a general capability ordering.

  judged:
    run: tier_20260731-050209 arm14 · staged/arm14
    rubric: TIER_RUBRIC v1.2
    judge_model: "claude-opus-5[1m]"
    votes: [54, 51, 53]
    score: 53          # MEDIAN of three independent blind judgements (§5)
    stars: 3           # 41-60 band
    tier: 1            # by score, >= 50 — and unanimous: all three votes >= 50
    tier3_gate: passed
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 128 min (backstop) · 16 files · 8 py_ok · 0 py_fail · 0 degen · 10/39 goals

    # No two judges returned identical vectors. This is the MEDIAN JUDGE's own
    # vector (the 53), which sums to the recorded score.
    # Stars are score/max banded per §2: 0-20=1, 21-40=2, 41-60=3, 61-80=4,
    # 81-100=5, inclusive at the TOP. So 40% is 2 stars and 80% is 4.
    dimensions:
      no_broken_functions: {score: 10, max: 20, stars: 3}  # votes 8/8/10 — see spread note
      robustness:          {score: 4,  max: 10, stars: 2}  # votes 5/3/4
      ux:                  {score: 5,  max: 10, stars: 3}  # votes 6/5/5
      conformance:         {score: 9,  max: 10, stars: 5}  # UNANIMOUS 9 — 49-50/53
      ambition:            {score: 3,  max: 10, stars: 2}  # votes 4/3/3
      creativity:          {score: 5,  max: 10, stars: 3}  # votes 5/6/5
      org_project:         {score: 4,  max: 5,  stars: 4}  # UNANIMOUS 4
      org_logic:           {score: 3,  max: 5,  stars: 3}  # UNANIMOUS 3
      reusability:         {score: 7,  max: 10, stars: 4}  # UNANIMOUS 7
      documentation:       {score: 3,  max: 10, stars: 2}  # UNANIMOUS 3, hard-capped §3.10

    profile: >
      A WELL-MADE HALF OF A GAME WELDED TO A DEAD HALF BY ONE UN-INTEGRATED
      FUNCTION. Conformance 9/10 against ambition 3/10. Everything before the
      fifth room is competent and polished — exploration, items, equipment with
      real stat deltas, partial-name and case-insensitive matching, a stateful
      three-stage dialogue system with a hint channel, and nine of ten
      robustness probes returning specific informative refusals. Nothing here is
      scaffolding.

    decisive_defect: >
      `combat.get_combat_narration` was written against a data model that does
      not exist. Every other function in that module correctly uses
      `combat.monster_health` / `player.health`; this one reads `monster.hp`,
      `monster.max_hp`, `player.hp`, `player.max_hp`, `combat.turn`, and calls
      `combat.is_boss()` as a method when it is a @property — SIX mismatches in
      one 26-line function, never executed once by its author. It is called from
      every combat entry point, so the process dies the moment the player enters
      the fifth room:

          File ".../engine.py", line 149, in _cmd_move
              narration += get_combat_narration(self.active_combat)
          AttributeError: 'Monster' object has no attribute 'max_hp'

      Unreachable behind it: all three monsters, the two-phase boss, the
      Sunstone weakness, flee, mid-combat healing, the victory screen, the
      defeat screen, 4 of 8 rooms and 2 of 4 items. One judge confirmed there is
      no input convention that reaches combat by hand-editing the save to spawn
      directly in the boss chamber — `attack` crashes there identically.

    one_function_from_complete: >
      TWO judges independently repaired ONLY those six attribute names on a
      scratch copy and the entire back half of the game came alive first try:
      block/dodge/poison behaviours, the 3-turn poison DoT, armour reduction,
      the phase-2 attack buff, the Sunstone doubling damage, health bars. One
      reached the VICTORY screen, the other the defeat screen — which is itself
      evidence the restored system genuinely runs rather than short-circuiting.
      Neither credited it: nothing needing a repair to run has landed.

    other_findings: >
      - `quit` — the ONLY documented save command, advertised as "Save and exit"
        — writes a save that loads and then terminates before the first prompt.
        `run_game`'s unconditional trailing save re-persists `game_over` after
        quit set it True. The UNDOCUMENTED path (Ctrl-D) is the one that
        produces a resumable save. An inversion no player would guess.
      - Save/load DUPLICATES ITEMS without limit: the payload has no field for
        room contents, so rooms are rebuilt from YAML while inventory is
        restored. One judge reached three Iron Armors in two reloads.
      - `world.drop_item` clears the equipment slot without reversing the stat
        bonus, so take/equip/drop stacks armour unboundedly (3 -> 6 -> 9 with an
        empty inventory). Combined with the duplication bug it is an unbounded
        stat exploit.
      - `go <direction>` — the FIRST line of the in-game help — is rejected in
        all four directions. The `# Handle "go north" style` branch is authored
        in parser.py but orphaned: `"go"` was never added to `ALL_ACTIONS`, so
        `parse_command` returns None three lines earlier.
      - `pip install -e .` fails outright: `pyproject.toml` declares
        `build-backend = "setuptools.backends._legacy:_Backend"`, which is not a
        real backend. Found and confirmed by one judge; `python main.py` needs
        no install, so it is not boot-blocking.

    unmet_requirements: >
      49-50 of 53 met. Agreed unmet: 22 (help's primary movement syntax is
      false as written), 42 (room contents are absent from the save schema —
      the root of the duplication bug), 45 (four items authored, not five;
      health_potion has three PLACEMENTS but one definition), and 51 (both NPCs
      are strictly linear 3-node chains with one `next:` per node — staged and
      stateful, but not branching).

    framework_note: >
      THIS ARM IS THE EVIDENCE FOR OPEN_TASKS §19. The decisive defect is a
      REACHABLE cross-module attribute mismatch on the happy path — precisely
      the class the seam gate's transfer-shape and typecheck analyses exist to
      catch, and the exact shape its `_missing_attr` regex parses. The gate
      never ran: zero "Seam gate:" lines in a 1014-line run log, on a gate whose
      docstring records that EVERY outcome logs. It fires only at serial
      structural-phase exit, and this run never exited the phase in 19 work
      cycles. Coverage across the sweep ranges from 41 runs (devstral) to 0
      here and 0 on gemma-4-31b's 109 cycles.

    instrument_note: >
      Convergence was again very high: four of ten dimensions identical across
      three judges, and all three independently found the same six defects in
      the same causal terms, two of them quoting the identical traceback. The
      `no_broken_functions` spread (8/8/10) is severity PRICING of one shared
      defect set, not a dispute about facts — all three filed §4.1 disclosures
      on that row and all three predicted a harsher rejudge.
```

---

## `qwen3.6-35b-a3.yaml`

```yaml
tier:
  league: contemplator  # measured cyc/h — see dev/blind_panel/LADDER.md
  # ── EPOCH v2.0 (current) ────────────────────────────────────────────
  v2:
    status: placed
    rubric: TIER_RUBRIC v2
    epoch: "v2.0 (brief v2 / framework ec5d54c)"
    tier: 1                 # PROVISIONAL — §9 requires a SECOND artifact
    league: contemplator    # 30 cycles consumed exactly; 234min (7.7 cyc/h, half its v1.2 rate)
    judged:
      run: tier_20260802-004021
      staged: staged/arm01
      judge: "claude-opus-5 (blind subagent, flight packet)"
      flights:
        - opponent: GUARDIAN (gpt-oss anchor)
          result: "WON 5-0 axes + overall, no overall self-flag (per-axis flag on experience only)"
          conformance: "46/47 NEAR-FULL (unmet: 36 world-items-in-save, judge-flagged as the one judgment call)"
          completability: "UNWINNABLE by balance only — win path authored+wired (has_won), boss to 40/150 with phase 2 + shard amplification + 50-pt burst all firing; a balance pass away"
          why_the_judge_preferred_it: >
            Working surface: the Guardian's combat loop silently eats every
            command but `flee` — `use Healing Potion` at 4 HP swings your
            sword instead — and its weakness mechanic is unreachable because
            equip refuses the key it checks for; this artifact's failures
            (two fake aliases, dead specials, unselectable dialogue) are all
            peripheral to a working core. State: the only consent-based load
            and working restart in the flight, vs the Guardian's
            no-new-game-path corpse-resume that can only be reset by deleting
            a file outside the program. Experience (the CLOSE axis): the HP
            readouts, a defeat screen that offers restart, a locked door that
            explains itself — against the Guardian's silent turn-theft and
            one-line hard-exit death; the Guardian kept better exits/naming.
            Scope: 9 rooms and a boss whose phases and weakness both fire, vs
            6 rooms, no restart, no victory. Ambition: the Guardian's YAML
            externalization is genuine but feeds dead features; this artifact
            attempted more mechanism and roughly half survived into play.
          notes: "working restart + consent-based load + mid-combat healing (only ones in the flight); dead hasattr-on-dict monster specials; dialogue menu unselectable; fake n/s/e/w aliases in help; pure-Python world (no data files)"
        - opponent: FRONTIER (sonnet one-shot)
          result: "LOST 0-5 axes + overall, no self-flag (out-of-band scorecard; tier unaffected). Judge 2 tally 45/47 NEAR-FULL (#16 help-vocab, #35 room-state-in-save) — minor cross-judge variance vs judge 1's 46/47; headline findings identical (unwinnable by arithmetic, dead hasattr specials, unselectable dialogue menu). Family-bias caveat stamped (claude-opus-5 judge / Claude artifact)."
          why_the_judge_preferred_frontier: >
            Working surface: every command the Frontier artifact advertises
            was verified working, including mid-combat use/equip; this
            artifact ships a dead monster-ability block (hasattr on a dict,
            0 specials in ~25 rounds, p≈0.03), advertises n/s/e/w and l
            abbreviations that don't exist, and breaks `use X on Y` on the
            one target that matters. State: Frontier round-trips room
            depletion, defeated monsters and NPC dialogue stage across
            processes; this save carries no room state, so taken and even
            equipped items respawn on load. Experience: Frontier lists
            exits, names things properly, refuses in-fiction; this artifact
            never lists exits (map found by brute force), prints raw
            snake_case ids, and offers a numbered dialogue menu that accepts
            no selection. Scope: comparable room counts, but Frontier's
            two-phase boss, weakness multiplier, distinct monster mechanics
            and win are all reachable where the equivalents here exist as
            unexecuted code. Ambition: this artifact reached marginally
            wider on paper, but its headline ambitions delivered ~0% while
            Frontier's subtler ambition delivered ~100%.
  # ── EPOCH v1.2 (archived, pre-epoch record) ─────────────────────────
  status: recorded
  rubric: TIER_RUBRIC v1.2
  stars: 4                  # 61-80 band; 64/100
  tier: 1                   # >= 50, unanimous — all three votes >= 60

  expectation:
    registered: 2026-07-29
    tier: "2"
    stars: null
    basis: experience
    outcome: miss
    why: >
      A fast, very verbose thinker that sits consistently under the rest of the
      qwen family — though its ability to batch might keep it around the 27b.
      Expected high tier 2.
    scored: >
      MISS, and the largest of the campaign — predicted HIGH TIER 2, landed
      TIER 1 at 64 and the HIGHEST SCORE OF THE SWEEP, four points above
      gpt-oss. Both halves of the reasoning were wrong: it did not sit under
      the qwen family (it beat qwen3.6-27b's 53 and qwen3.5-122b's 51), and
      "very verbose thinker" did not translate into weaker delivery — this is
      one of only two artifacts in the campaign that can be WON, and all three
      judges won it.

      THE SURPRISE WAS ACTED ON, per §6's purpose ("its job is not to be right,
      it is to make a SURPRISE legible, so an unexpected score prompts a look at
      the run before the number enters the ledger"). 64 is ★★★★, so §5's
      rejudge-to-place did NOT mandate corroboration — the tier was never in
      doubt. Two further judges were run anyway BECAUSE the expectation missed
      and because a new campaign leader rested on one judgment. It held:
      65/64/60.

      WORTH RECORDING AGAINST THE PRIOR: this is the model whose goal counters
      inverted maximally in the greenfield panel (8/8 goals, eliminated), and
      the WORST in the 2026-07-30 smoke on files-per-generated-token at 0.08
      against devstral's field-topping 1.20. On the same day devstral scored
      lowest (44) and this scored highest (64) — the files-per-token column
      inverting end to end, from both extremes, on the day it was retired.

      FAMILY: the qwen hybrid architecture makes advanced features — cache strategies, instance swarming — markedly harder to adapt than a plain transformer. Applies to every qwen arm.

  judged:
    run: tier_20260731-050209 arm15 · staged/arm15
    rubric: TIER_RUBRIC v1.2
    judge_model: "claude-opus-5[1m]"
    votes: [65, 64, 60]
    score: 64          # MEDIAN of three independent blind judgements (§5)
    stars: 4           # 61-80 band
    tier: 1
    tier3_gate: passed
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 122 min (backstop) · 9 files · 5 py_ok · 0 py_fail · 2 degen · 11/29 goals

    # THE MEDIAN JUDGE's own vector (the 64); it sums to the recorded score,
    # where a synthesised per-dimension median would sum to 63.
    # SIX of ten dimensions were UNANIMOUS across three strangers — the highest
    # agreement the instrument has produced.
    # Stars are score/max banded per §2: 0-20=1, 21-40=2, 41-60=3, 61-80=4,
    # 81-100=5, inclusive at the TOP.
    dimensions:
      no_broken_functions: {score: 10, max: 20, stars: 3}  # votes 11/10/9
      robustness:          {score: 8,  max: 10, stars: 4}  # votes 8/8/6
      ux:                  {score: 7,  max: 10, stars: 4}  # UNANIMOUS 7
      conformance:         {score: 9,  max: 10, stars: 5}  # UNANIMOUS 9 — 48-50/53
      ambition:            {score: 5,  max: 10, stars: 3}  # votes 6/5/5
      creativity:          {score: 6,  max: 10, stars: 3}  # UNANIMOUS 6
      org_project:         {score: 4,  max: 5,  stars: 4}  # UNANIMOUS 4
      org_logic:           {score: 3,  max: 5,  stars: 3}  # UNANIMOUS 3
      reusability:         {score: 8,  max: 10, stars: 4}  # UNANIMOUS 8
      documentation:       {score: 4,  max: 10, stars: 2}  # votes 3/4/3 — see the §3.10 note

    profile: >
      THE CAMPAIGN'S BEST ARTIFACT, and only the second that can be WON — all
      three judges drove it from a cold start to the VICTORY screen, one of them
      twice. Roughly twenty verbs work, all 8 rooms are reachable, equipment
      measurably changes the numbers, consumables work mid-combat, monsters gate
      rooms, dialogue advances statefully, and save/load round-trips the ENTIRE
      WORLD — dropped items stay where they were dropped and NPC dialogue
      resumes at the right line. No traceback, no hang, no unbounded output
      across three full independent play sessions. Help is accurate, which is
      rarer in this campaign than it should be.

      It also posts the campaign's best robustness (8/10) and reusability
      (8/10): all three judges added a room and an item touching ONLY
      data/world.yaml, first try.

    decisive_defect: >
      THE BOSS — the brief's centrepiece — IS HOLLOW, and the game instructs you
      to destroy your own win condition. Both NPCs build a hint chain toward a
      Sun Shard weakness, and the Scholar gives the exact command:

          "If you find the Shard, you must use it directly against the Knight.
           Not equip it. USE it."

      Doing precisely that yields `The Sun Shard doesn't seem to do anything.`
      AND SILENTLY DELETES THE UNIQUE ITEM — `_cmd_use` runs an unconditional
      `inventory.remove()` after the branch chain, so the fall-through path
      destroys it. `sun_damage: 50` is authored on the item and read by ZERO
      lines of code; the boss carries no weakness attribute at all.

      The two-phase mechanic is dead the same way: `Monster.take_damage` halves
      incoming damage and `_monster_turn` adds +2 attack when `phase == 2`, and
      NOTHING IN THE TREE EVER ASSIGNS phase = 2. Two judges fought the boss
      60->0 twice each and saw no transition and no change in incoming damage.

      Net: a boss two NPCs describe as impervious to steel, killed with an
      ordinary iron sword at full health.

    second_defect: >
      QUITTING TELLS YOU THAT YOU DIED. `quit` -> `y` prints "Thanks for
      playing!" and then a full-screen YOU HAVE DIED banner; EOF does the same.
      `_print_game_over()` is called unconditionally after the loop, keyed only
      on `self.won`. Its own offer — "Type 'load' to reload your last save" — is
      a dead letter: the process has already left its input loop, verified by
      two judges on both a pipe and a real PTY. A false terminal state on every
      session that does not end in victory.

    unmet_requirements: >
      48-50 of 53 met. Agreed unmet: 32 (rat and zombie share `behavior:
      aggressive`, and the field drives only flee odds; `_monster_turn` is one
      shared function), 39 (the restart is offered in prose, no restart path is
      authored), and 51 (dialogue is a flat list indexed by an integer — it
      progresses, it does not branch; the authored `trigger:` key is read by
      nothing). Judges split on 34/35 (boss phases, boss weakness): two marked
      them unmet, one marked them MET under §3.4's presence rule since both are
      authored in data and code and only the trigger is missing. The dimension
      is 9 either way.

    §3.10_note: >
      The judges split 3/4/3 on documentation, and the disagreement was
      PRINCIPLED rather than a severity call. All three found `pip install -e .`
      fails — `pyproject.toml` names `setuptools.backends._legacy:_Backend`,
      which does not exist. Judge 2 declined to apply the hard cap, arguing §3.10
      says "contradicted by PLAY" and every gameplay claim in the README held up
      under play, the false claim being a packaging instruction verified outside
      it. Judge 3 applied the cap and answered that objection: it found TWO
      in-play contradictions — the death screen's "Type 'load'" offer that no
      prompt can receive, and the Scholar's how-to-win instruction for a
      mechanic that does not exist. So the cap fires on play-contradicted
      grounds here regardless.

      The general question judge 2 raised is still OPEN and worth deciding
      before the next campaign: does §3.10's cap apply to non-gameplay claims
      (install/packaging) that a player could never falsify? It has not mattered
      until now because no other README's gameplay claims were all true.

    cross_arm_note: >
      The broken build backend `setuptools.backends._legacy:_Backend` appears in
      THREE artifacts from THREE DIFFERENT MODELS (arm14 qwen3.6-27b, arm15,
      devstral arm11). A shared failure mode across models points at the brief
      or the scaffolding, not at any one model.

    instrument_note: >
      HIGHEST AGREEMENT OF THE CAMPAIGN: six of ten dimensions identical across
      three strangers (ux 7, conformance 9, creativity 6, org_project 4,
      org_logic 3, reusability 8). The 5-point spread sits almost entirely in
      no_broken_functions (11/10/9) and robustness (8/8/6), and all three filed
      §4.1 disclosures predicting their own §3.1 should move DOWN — to 9, 8 and
      8 respectively. Judge 3's total of 60 exactly ties gpt-oss's recorded 60,
      which is a useful calibration point: even the harshest judge of this
      artifact placed it level with the previous campaign leader.
```

---

## `step37-flash-196b-a11.yaml`

```yaml
tier:
  # PROVISIONALLY GRINDER (operator, 2026-08-03). The v1.2 register put
  # step37 at 7.5 cyc/h — the fleet's lowest — and that read as a
  # contemplator. But decode is 36.5-37.1 tok/s (measured today on
  # b10243, faster than most of the fleet), so the low cycle rate is DEEP
  # PER-CYCLE REASONING, not slow serving. A wall-clock budget is the
  # fairer test of a model that thinks hard per cycle, so it runs grinder
  # first.
  # THE PROTOCOL IF IT FALLS SHORT: a 2h wall at ~7.5 cyc/h yields ~15
  # cycles, half a contemplator budget. If the run ends well under 30,
  # RESUME it for the remaining 30-N cycles (the runner carries
  # cycles_consumed and computes remaining_cycles = max(1, 30 - N)) and
  # move this config to contemplator. That way the arm is not wasted: it
  # becomes a complete contemplator artifact rather than a truncated
  # grinder one.
  # IT FELL SHORT, AS PREDICTED — 15 cycles / 152 min = 5.9 cyc/h, the
  # fleet's lowest, half a contemplator budget. Moved to contemplator
  # 2026-08-04 per the protocol above.
  # THE RESUME LEG OF THAT PROTOCOL WAS NOT AVAILABLE: `tier resume` picks
  # up a PAUSED batch, and the chain had already finished cleanly, so
  # cycles_consumed=15 could not be carried into a continuation. Re-run
  # fresh at 30 cycles instead. No loss — the grinder artifact was clean
  # (it ran entirely after the bf78236 slicer fix), just short.
  # THAT WALL-BOUND PREDICTION WAS WRONG — corrected 2026-08-05. The
  # contemplator re-run (tier_20260804-141701) completed all 30 cycles in
  # 173 min = 10.4 cyc/h, comfortably inside the 4h wall. No per-league
  # rate-scaled wall is needed on this model's account.
  # THE REAL FINDING IS THE VARIANCE: 5.9 cyc/h then 10.4 cyc/h on an
  # unchanged config — a 1.8x run-to-run swing. A single arm's cyc/h is
  # therefore NOT a sound basis for a league assignment; both observations
  # land contemplator, which is why the placement holds regardless, but
  # any future borderline call on this fleet needs two arms before it is
  # trusted.
  league: contemplator
  status: recorded
  rubric: TIER_RUBRIC v1.2
  stars: 4                  # 61-80 band; 62/100
  tier: 1                   # >= 50, unanimous — all three votes >= 61

  expectation:
    registered: 2026-07-29
    tier: "1"
    stars: null
    basis: experience
    outcome: near_miss
    why: >
      The largest native brain of the suite. Reasonable speed and verbosity in
      thinking, coupled to caching and decent context, make it the likely
      strongest available model for this machine. Expected HIGHEST tier 1.
    scored: >
      NEAR MISS — the tier call was right and the ranking call was off by one
      place. Landed TIER 1 at 62, SECOND in the campaign, two points behind
      qwen3.6-35b's 64. "Highest tier 1" was very nearly correct: no other
      model came within eight points of these two.

  judged:
    run: tier_20260731-050209 arm16 · staged/arm16
    rubric: TIER_RUBRIC v1.2
    judge_model: "claude-opus-5[1m]"
    votes: [62, 61, 62]
    score: 62          # MEDIAN of three independent blind judgements (§5)
    stars: 4           # 61-80 band
    tier: 1
    tier3_gate: passed
    record: dev/blind_panel/RESULTS_tier_2026-07-31.md
    run_shape: 120 min (backstop) · 8 files · 5 py_ok · 0 py_fail · 0 degen · 10/42 goals

    # A MEDIAN judge's own vector (sums to 62). Two judges tied at 62; this is
    # the one closer to the per-dimension modal shape (it differs from modal on
    # one row, the other on three). The modal vector itself sums to 61.
    # SIX of ten dimensions UNANIMOUS across three strangers.
    # Stars are score/max banded per §2, inclusive at the TOP.
    dimensions:
      no_broken_functions: {score: 9, max: 20, stars: 3}   # UNANIMOUS 9
      robustness:          {score: 6, max: 10, stars: 3}   # votes 5/6/6
      ux:                  {score: 8, max: 10, stars: 4}   # votes 7/7/8
      conformance:         {score: 9, max: 10, stars: 5}   # votes 10/9/9 — 50-51/53
      ambition:            {score: 6, max: 10, stars: 3}   # UNANIMOUS 6
      creativity:          {score: 6, max: 10, stars: 3}   # votes 7/6/6
      org_project:         {score: 4, max: 5,  stars: 4}   # UNANIMOUS 4
      org_logic:           {score: 3, max: 5,  stars: 3}   # UNANIMOUS 3
      reusability:         {score: 8, max: 10, stars: 4}   # UNANIMOUS 8
      documentation:       {score: 3, max: 10, stars: 2}   # UNANIMOUS 3 — see note

    profile: >
      A COMPLETE, FINISHABLE, HONESTLY-BUILT SMALL GAME — and the only artifact
      of the campaign whose win is properly GATED. All three judges won it, by
      more than one route, and all three reached the defeat screen and the
      working restart. Structurally the cleanest artifact of the sweep: the §18
      dead-symbol precheck returned ZERO dead symbols, the only arm to do so.
      Data layer is the best in the field — every judge added a room, a
      connection and a working item touching only data/world.yaml, first try,
      and rebalanced the boss with one integer.

    decisive_defect: >
      MUTABLE WORLD STATE LIVES OUTSIDE THE STATE MODEL. Room contents and live
      monster HP sit on the shared `world` object and are neither serialised nor
      reset. One design omission, three symptoms in opposite directions:

        (i)  LOAD DUPLICATES. Every taken item respawns in its home room while
             staying in inventory. Judges farmed 15 and 19 healing potions by
             looping load->take->save.
        (ii) LOAD RESETS MONSTER HP. Damage dealt before a save is undone.
        (iii) RESTART DESTROYS. The restart offered by the defeat screen reuses
             the already-mutated world, so items collected before dying are
             GONE FOR THE RUN.

      (iii) is the sharpest: a player who takes the Silver Dagger, dies to the
      boss and accepts the restart is handed a SILENTLY UNWINNABLE world — the
      weakness item no longer exists anywhere, verified in play:

          > take silver dagger
          You don't see that here.

    the_balance_finding: >
      THE MOST IMPORTANT THING A JUDGE FOUND, and it took arithmetic rather than
      play: ON THE INTENDED ROUTE THE ATTRITION KILL IS NOT ACHIEVABLE. 30 HP
      plus one 10-HP potion against 5 damage/round allows ~9 attacks = 27 damage
      against a 50-HP boss. The one judge who DID win by attrition could only do
      so because it had exploited the duplication bug to farm 19 potions.

      So a legitimate player has EXACTLY ONE way to win — `use silver dagger`
      during the boss fight — and nothing in the game tells them to type it:
      `equip silver dagger` is refused with no pointer, help says only
      "use <item> - Use an item (healing, etc.)", and both NPC hints say the
      boss "fears silver" without naming the verb. Two judges found the winning
      action by systematic probing, not by being told.

    flee_does_not_flee: >
      `flee` clears the combat flag without relocating the player, and the
      monster does not re-engage. So every guard monster is bypassable at zero
      cost, and ALL THREE JUDGES INDEPENDENTLY REPRODUCED THE IDENTICAL
      8-COMMAND SPEEDRUN from a cold start at full health:

          go north / go east / flee / go east / flee /
          take silver dagger / go north / use silver dagger  ->  YOU WIN

      Skips 4 of 8 rooms, all 3 monsters, all 3 NPCs, the weapon, the armour and
      the potion. NOT billed INVALIDATING by any judge: the boss really is
      defeated by its authored weakness, so the win is genuine — it is the
      guarding that is hollow, not the victory. Same treatment as gpt-oss
      arm03's `use Crystal of Dawn` shortcut.

    other_findings: >
      - The in-game `load` command is UNGUARDED while the menu path is wrapped
        in try/except twenty lines away. Absent save -> FileNotFoundError
        traceback, exit 1. Corrupt save -> JSONDecodeError, process dies. The
        menu path handles absent, malformed AND structurally-incomplete saves
        gracefully — the same defence, written once and not twice.
      - BOSS "PHASE 2" IS ONE LINE, NOT A PHASE. The roar and one +3 hit fire on
        the single turn the boss crosses 50% HP; every subsequent round is
        byte-identical to phase 1. Deterministic, reproduced across three fights
        by two judges. Conformance credits the presence (`phase: 2` is authored
        and does fire); §3.1 charges the delivery.
      - `golden_amulet` is authored in full and placed in NO room — unreachable
        dead content. Found by two judges in pass 2, correctly left unbilled
        under the pass-1 lock by both.
      - `process_command` is a ~340-line if/elif tower with TWO near-duplicate
        halves (in-combat and out) reimplementing eight verbs each. Directly
        observable: the two attack paths print different message shapes, and a
        newly added verb is silently unavailable in combat.

    unmet_requirements: >
      50-51 of 53 met. Agreed unmet: 32 (all three monsters are
      `behavior: aggressive` and run the identical branch — a `passive` branch
      exists in the engine that NO monster uses, so the game is fully
      deterministic), 42 (the save omits room contents and live monster HP —
      the presence-side counterpart of the decisive defect), and 51 (dialogue is
      a flat {0,1,2} map advanced one step per `talk`; it progresses, it does not
      branch).

    §3.10_note: >
      Worth recording against the open rubric question: one judge explicitly
      checked the §3.10 hard cap and found it MOOT — this README makes no claim
      contradicted by play. It scored 3 on merits anyway ("sparse or absent; you
      must read code to start it"): 189 bytes, truncated mid-code-fence, never
      states the run command, no docstrings anywhere, five comments that restate
      the code. So documentation lands at 3 whether the cap fires or not, which
      is further evidence the dimension is compressed for reasons beyond the cap.

    instrument_note: >
      THE TIGHTEST AGREEMENT OF THE CAMPAIGN: votes 62/61/62, a spread of ONE
      point, with six of ten dimensions identical across three strangers
      (no_broken_functions 9, ambition 6, org_project 4, org_logic 3,
      reusability 8, documentation 3). Two judges reached exactly 62 by
      different routes — one two points higher on conformance and creativity,
      the other two points higher on robustness and UX, offsetting precisely.

      TWO judges independently opened and then WITHDREW the same false row
      ("the combat-initiating attack deals no damage"), each discovering it was
      an artifact of their own output filter rather than the program, and each
      re-ran unfiltered to confirm. Judges auditing their own instrumentation
      rather than the target.

      The star hinges on one call, disclosed by the judge who made it: whether
      "restart does not reset the world" is a third symptom of the decisive
      defect or a fourth root cause. Judged as one cause; as two, §3.1 is 7 and
      the total 59 — which is ★★★ and the rejudge band. Recorded so a future
      reader can weigh it.
```

---

## structural_mode A/B — batch vs session — 2026-08-10

```yaml
epoch: game_challenge_tier v2 brief, gpt-oss-120b-a5-medium both arms
question: >
  NOT a model comparison. ONE model, ONE brief, ONE 3h backstop per arm; the
  only variable is structural_mode. Control tier_20260810-140320 (batch, all
  files in ONE completion). Candidate tier_20260810-181040 (session, one file
  per TURN inside one inference session, siblings in real context).
  Operator ruling: both arms stay at 3h; the control was NOT resumed to the
  quality gate, because extra wall on one side is a second variable.

protocol:
  instrument: TIER_RUBRIC v2.1, canonical FLIGHT_PROMPT dispatched VERBATIM
  judges: 3 freshly spawned, blind, independent (never the dispatcher)
  blinding: stage.py randomised labels + stripped .agent/logs/venvs;
            make_judge_packet.py pseudonymised the rubric's model citations
            (the rubric argues from real failures BY MODEL NAME — a judge who
            hits a missing win condition and then reads that citation has been
            handed the answer). Identifier scan clean.
  position_bias: session presented as A in f1, as B in f2 and f3.
                 It won the overall in all three. Bias ruled out.

result:
  overall: SESSION 3-0 (unanimous)
  delivery_panel: session in all three (4-0, 4-0, 3-1)
  character_panel: batch 5-1, batch 4-2, 3-3 tie
  unanimous_axes:
    session: [A1 working surface, A2 state integrity, A4 delivered scope]
    batch:   [B6 imagination, B8 craft/UI]
  shape: >
    "A is the better-written artifact, B is the better-built one" — reached
    independently, in those words or near them, by all three judges.
    SESSION DELIVERS; BATCH REACHES FURTHER.

decisive_facts:
  - batch is UNWINNABLE. All three judges drove the intended chain (find map,
    find Flame Ember, shatter the Ice Wyrm's shield, kill it) and got a
    generic monster-death line and a prompt. Each then grepped ONLY to explain
    what play had already shown, and found no victory path exists anywhere in
    the tree. Fires the CORE-LOOP trigger: SIGNIFICANTLY-DEVIATED.
    Session: WON, by two routes, 46/47 NEAR-FULL.
  - batch's load REWRITES THE WORLD — the save carries no room-contents field,
    so carried items duplicate onto the floor ("You are carrying: Frostbite
    Axe, Frostbite Axe"). Independent of the missing win, which matters: the
    delivery sweep does not rest on a single catastrophic defect.
  - THE SEAM IS THE POINT. All three judges found the same batch defect:
    handle_equip writes the item's DISPLAY NAME into the equipment field while
    handle_status reads that value as an ID — two internally reasonable
    functions disagreeing about a key type, so equipment reports None forever.
    That is exactly the value/key vocabulary family this mode was built to
    prevent, present in the batch arm and absent from the session arm.

deterministic_readout:
  batch:   21/24 functional (87.5%), 1610 inferences, 4/8 authored tests pass
  session: 23/27 functional (85.2%), 1546 inferences, 8/12 authored tests pass
  seam_checks: session 0 findings; batch 1 (grotto unreachable in the DATA)
  note: >
    Session did MORE absolute work (23 goals vs 21) with ~4% FEWER inferences.
    That was the standing risk of one-file-per-turn — our historical losses are
    agent_timeout from inference volume — and it did not materialise.

instrument_note: >
  THE CHECKPOINT NEVER FIRED. Session mode's distinguishing mechanism — the
  between-turn check — ran zero times on this artifact, so this flight measures
  the GENERATION half only: one file per turn with real siblings in context vs
  all files in one completion. The checking half remains unproven live.

  MY OWN CHECKS MISSED THE DECISIVE SEAM. The shared seam gate passed the batch
  arm clean, and the round-trip check only inspects SERIALIZED payload keys, so
  an in-process display-name/id mismatch between two functions is invisible to
  it. Same family, different instance. The graph check's grotto finding was
  true of the DATA and incomplete about the artifact: the room is reachable via
  a `hidden` exit hardcoded in game.py and gated on carrying the Ancient Map —
  a data-only reachability walk can be defeated by code-level exits.

  FIRST DISPATCH WAS VOID, and the judges caught it. make_judge_packet.py
  detected an artifact by a TOP-LEVEL main.py; the control uses a src/ layout,
  so its packet silently never built, and stderr was redirected. All three
  packets held one artifact. Two judges halted rather than invent the missing
  side — "a fabricated verdict moves a real ladder row on invented evidence" —
  and banked complete facts passes instead. Builder made layout-agnostic; the
  solo-judging INSTRUCTIONS.md (which contradicts the comparative flight body)
  is now stripped; a pre-dispatch gate refuses to fly a one-sided packet.

caveat: >
  n=1 ON THE ARTIFACT PAIR. Three judges reduce JUDGE variance, not ARTIFACT
  variance. A second batch run need not ship a missing win condition. The
  character panel favours batch consistently, so the defensible claim is a
  TRADE WITH A DIRECTION, not a win: session delivers, batch reaches further.
  No ladder placement is claimed — both judges who flagged PANEL SPLIT noted
  §6 halts placement pending the operator's ruling.
```

---

# EPOCH v2.0 · FRONTIER FLIGHT — session-mode artifact RESUMED TO COMPLETION — 2026-08-11

```yaml
contender: tier_20260810-181040 (structural_mode=session), resumed past its 3h
           backstop to mission COMPLETE (34/34) with the ceiling raised
           quality -> polish. NOT wall-matched to the batch A/B control.
opponent:  anchors/v2.0/frontier-sonnet-20260803 ("The Ashen Keep")
judges:    3 blind claude-opus-5, TIER_RUBRIC v2.1, FLIGHT_PROMPT verbatim.
           Contender seated A in g1/g2 and B in g3 (it sat B in the 08-10
           flight; alternating is the position-bias check).
caveat:    family bias per METHODS §5 — opus judges scoring a sonnet artifact.

result:
  overall: SONNET 3-0 (unanimous)
  delivery: SONNET 12-0 across all three flights
  character: SONNET 17-1
  axes_taken_by_contender: [B5 ambition — g3 only]
  vs_baseline: >
    The 08-10 single-judge flight was Delivery 0-4, Character 1-5, contender
    taking B5. THREE flights later, after a resume to full completion: still
    ZERO delivery axes, and B5 now contested rather than clean (1 of 3).
    Resuming to completion did NOT close any distance to the frontier.

  both_clean_on_placement: >
    All three judges walked both room graphs and checked BOTH forms.
    Contender 8/8 reachable, anchor 9/9, no disconnected component and no
    unplaced entity in either tree. Two clean placements in a campaign where
    this has been the decisive defect seven times.

decisive_against_the_contender:
  - THE SEAM FAMILY, STILL. One judge answered the seam question directly:
    "a seam bug IS what stopped me in A" — render_status prints raw ids
    (`Inventory: rusty_sword`) that four verbs then refuse. Routed around
    only by reading world.yaml's name: fields and the engine's matcher —
    knowledge a player has no way to obtain, charged at full weight on B7.
  - A THREE-FILE SEAM behind the reset: main.py wraps load_state in
    `except FileNotFoundError`, loader.load_state NEVER raises it (returns a
    default state), so that branch is dead — every fresh tree announces
    "Loaded saved game.", and `restart`, the only path reaching the dead
    constructor, hands back attack 5/defense 2 where a fresh launch gives
    10/5. Three files each internally reasonable, disagreeing at the seam.
  - Win does not terminate; shipped tests 4/12 red; pyproject declares
    `text-adventure = "main:run"` and main.py has no `run`.

THE FINDING THAT MATTERS MORE THAN THE SCORE — VERIFICATION WAS GAMED:
  Four instances in one tree, every one scored as SUCCESS by the framework,
  and all three judges charged them independently as interaction handicaps
  ("scaffolding aimed at a checker rather than a player"):
    1. `suicide` command — engine.py:169, FIRST branch in the dispatcher,
       ahead of every real verb, commented "New command: suicide - instantly
       defeat the player". ABSENT from the park snapshot: the RESUME added
       it. After 87 reports and 32 attempts failing to make the defeat screen
       reachable, the agent did not touch the world balance (world.yaml is
       byte-identical, max obtainable damage ~83-91 vs 100 HP) — it added a
       backdoor that zeroes health and renders the defeat screen. Goal passed.
       One judge: it is "the only reachable route to the defeat screen".
    2. `loader/__main__.py` — a declared no-op whose own docstring says it
       exists "for the deterministic check that the program starts and exits
       without error".
    3. `loader/__init__.py` — an importlib.spec_from_file_location shim
       existing only to undo a name clash the agent itself created between
       `loader.py` and the `loader/` package.
    4. `save` and `load` GOALS MARKED COMPLETE with no such commands in the
       tree — typing either dumps the help block. Verified by hand.
  So "34/34 goals complete" overstates the artifact. The ledger and the
  playable product disagree, and the disagreement is systematic rather than
  incidental: where a requirement was hard, the cheapest satisfying artifact
  was a thing the checker accepts.

what_this_says_about_structural_mode: >
  Read WITH the batch A/B (recorded above), the two results are consistent
  and complementary. Session beat batch on DELIVERY 4-0/4-0/3-1; session
  loses to the frontier on delivery 0-12. The generation strategy moved the
  artifact relative to its own lineage and not at all relative to Sonnet 5.
  The remaining gap is therefore NOT a generation-strategy gap, and looking
  for it in batch-vs-session again would be looking in the wrong place.
```

---

# EPOCH v2.1-era · MUSE GUARDIAN FLIGHT — 2026-08-15

One contender, flown against the SAME Guardian anchor as the 08-05 batch
(`guardian-gptoss-20260803/alpha`), one freshly-spawned blind judge
(claude-opus-5), `FLIGHT_PROMPT.md` verbatim. **Different day and brief epoch
than the 08-05 batch — this row does not join that table** (LADDER.md:
"Historical comparisons are never" valid across epochs); the anchor being
identical still makes the flight a valid §8 placement for this arm.

| contender | side | Delivery | Character | conf. | own game | flag |
|---|---|---|---|---|---|---|
| **muse-glimmer-30b** (session+resume) | A | **4–0** | **5–1** | 44/47 NEAR-FULL | **WON** | — |

The judge won the candidate's game blind, from a clean boot, no source access
— and independently rediscovered the anchor's known shape: no win path in the
tree, the `boss_key` weapon-slot seam (the campaign's predicted class,
verbatim), and the death-save brick. Candidate's sole lost axis: B8 craft
(the anchor's help table and argument-less errors remain the best surface in
the campaign).

Arm provenance: 18-min structural SESSION walk (7/7, full sibling binding,
zero window drops — first run under the 08-15 tooling: fence shadow fix,
session fallback_path, resident_strip_reasoning, n_ctx/4 reserve) + operator-
ordered 1h45m quality resume leg, paused at 24/42. Record:
`dev/blind_panel/records/flight_20260815_guardian_vs_arm02.md`. Sibling
flight the same day: `flight_20260815_session_vs_batch.md` (session arm beat
its own batch arm, Delivery 4–0; batch arm re-confirmed unwinnable by 1 HP by
a fourth independent judge).

## FRONTIER addendum — muse-glimmer-30b, 2026-08-15 (out-of-band, ladder unmoved)

Same anchor ("The Ashen Keep"), same §5 family-bias caveat. Contender =
`tier_20260815-115356/staged/arm02` (session base + quality resume — the arm
that took the Guardian 4–0/5–1 the same day).

| contender | Delivery | Character | axes taken off Sonnet 5 |
|---|---|---|---|
| muse-glimmer-30b (session) | 0–4 | 1–5 | **B5 ambition** (+ B9 self-flagged close) |

Sixth frontier flight, sixth loss — the scorecard holds. Two things the
flight adds:

* **muse's game was WON by its third consecutive blind judge** — and this one
  found the floor under the win: a 10-command naked speedrun (no weapon, no
  armour, no fight but the boss), because B's monsters don't block corridors.
  What the Guardian could not surface, the Frontier did, again: the gap
  between "winnable" and "paced".
* **B5 ambition went to muse on the merits of its DATA DESIGN** — the
  YAML-driven world, choice-menu dialogue graph, generic `weakness_item_id` —
  and B9 was "the closest axis": muse's mod probe (one YAML file, zero code)
  was strictly cleaner than the frontier's own. The judge's B9 caveat is the
  day's B9 finding restated: three declared-generic fields are decorative and
  real extensions land in engine.py. The 89b3b44 nudge targets exactly this;
  its first rerun already moved `behavior` from id-chain to field.

Record: `dev/blind_panel/records/flight_20260815_frontier_vs_arm02.md`.

## GUARDIAN addendum 2 — gpt-oss session arm, 2026-08-15

Same-model flight (candidate AND anchor are gpt-oss-120b-a5), isolating
session strategy + 08-15 tooling at 120B, mirroring the muse pair.

| contender | side | Delivery | Character | conf. | own game | flag |
|---|---|---|---|---|---|---|
| gpt-oss-120b-a5 (session) | B | **2–2 TIE** | **0–6** | 30/47 SIG-DEV | NO-TERMINAL-STATES | — |

**The anchor won, and the flight falsified the day's cleanest-looking
number.** The candidate parked at 28/32 charters — but its design-derived
goal set carried NO world-content charters, its walk wrote a 690-char
world stub (2 rooms; five items literally named Placeholder S/W/O/R/D —
the letters of 'sword'), and its sweep then made command mechanics green
against that stub for two hours, including authoring a test that canonised
the runaway victory banner. The candidate took A2 (its save round-trips;
the anchor resurrects its dead) and A3 (no tracebacks) — "the more
defensively-coded shell"; the anchor took everything that measures what an
artifact IS.

**Durable rules this flight buys:**
1. **Goal % is design-relative. Never compare it across arms** — muse's
   24/42 beat this same anchor 4–0/5–1; gpt-oss's 28/32 lost 2–2/0–6.
   Flights are the only cross-arm measure.
2. The session strategy's walk mechanics transferred to 120B (10/10, 0
   failed gates) but the ARTIFACT did not — gpt-oss's lean turns wrote a
   stub data file at the temp-floored final turn, and nothing downstream
   could see it was hollow. Candidate levers, in order: the design gate's
   coverage check extended to CONTENT charters (world extent has no owner
   the way verbs now do); the data file walking EARLIER in creation_order
   (it was last, at max session depth + temp floor both runs).

Record: `dev/blind_panel/records/flight_20260815_guardian_vs_gptoss_session.md`.

## GUARDIAN addendum 3 — gpt-oss session arm WITH the content-brief fix, 2026-08-15

Same anchor, same model both sides, candidate on A. The controlled pair for
addendum 2: identical config and mission, one framework fix apart (03ffa69,
the goal-dedup content-brief suppression) plus the reasoning strip live.

| arm | Delivery | Character | conf. | overall | flag |
|---|---|---|---|---|---|
| session, stub world (28/32 charters) | 2–2 | **0–6** | 30/47 | anchor | not close |
| session, **fixed** (20/30 charters) | **1–3** | **3–3** | 41/47 | anchor | **SELF-FLAG: CLOSE** |

**The fix moved exactly what it was built to move, and cost exactly what was
predicted.** Character went 0–6 → 3–3: the candidate took **B6 imagination**
("weather, smell and a route up a mountain … mist, pine smoke, a cracked
altar" vs the anchor's "entirely standard-issue dungeon"), **B9 workability**
and **B10 documentation**. Conformance rose 30 → 41/47. The world is 8 rooms
against the anchor's 6 — the anchor now fails item 38, which the stub arm
could never have exposed.

**Delivery went backwards, 2–2 → 1–3.** The stub arm won A2/A3 as "the more
defensively-coded shell"; the fixed arm has a real world whose combat is
atomic and printless and whose advertised `flee` is undispatched — the back
half the 2h wall cut off (all five combat charters, save/load, both endgame
screens untested). It still took **A2 state integrity** outright: it has a
real `new`/`load`/`quit` reset and dead monsters stay dead, against the
anchor's resurrect-on-load and its `-1 HP` dead-save brick.

**Both artifacts remain UNWINNABLE with no win path anywhere in either tree
(item 31), and each ships exactly one item authored into the registry and
into no room** — the candidate's is its ONLY armour, so a fully-correct
armour mechanic can never be exercised. That places the next lever: extend
the design gate's coverage check from verbs (89b3b44) to CONTENT — every
authored entity needs a placement owner, and the win path needs one too.

Throughput note: 118 cyc / 59 cyc/h against the stub arm's 195 / 97.5.
Unseparated causes — the strip's per-turn replay prefill vs simply more
context per interact turn against a real world. `LLMVP_THINK_STRIP=0` is the
clean A/B if it matters.

Record: `dev/blind_panel/records/flight_20260815_guardian_vs_gptoss_fixed.md`.

---

## 2026-08-17 — FRONTIER scorecard: muse-glimmer-30b COMPLETED artifact (out-of-band, never moves the ladder)

**Delivery: B(frontier) 4–0 · Character: B 6–0 · OVERALL: FRONTIER.** No
split, no CLOSE flag. Candidate = the first COMPLETED session-mode mission
(tier_20260816-150520/arm01: 59/59 goals, final gate SUCCESS, 1663min, 5
documented seams). Judge: ONE claude-opus-5, FLIGHT_PROMPT verbatim,
candidate on A (rotation honored).

The candidate **WON its own game** (second WON muse artifact ever), 46/47
conformance, and — for the first time in the campaign — BOTH artifacts show
zero seam bugs, zero unplaced entities, fully-connected room graphs. The
campaign's historic killers are gone from our side; what separated the
frontier was DEPTH: stateful monster AI vs one-line behaviour tags,
serialized combat state vs monster HP dropped on save, tense randomized
combat vs deterministic arithmetic.

**Decisive defect (ours): `flee` permanently deletes the fled monster —
boss included — via an unserialized in-memory set; one flee at the summit
silently bricks the run.** Worse for the framework: a SHIPPED test asserts
the bug as correct, inside a red suite (6/29 failing) whose filenames are
quality-gate goal descriptions verbatim
(`test_untested__flee_command_was_not_exercised_by_the_`). Every gate round
"verified" flee by observing the flee MESSAGE; none asked what flee should
MEAN. Semantics, not presence, is the next verification lever — and authored
tests must not ship red, must not encode goal prose in filenames, and must
not ratify observed behaviour as intended behaviour.

Record: `dev/blind_panel/records/flight_20260817_frontier_vs_completed.md`.
