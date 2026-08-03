# The Ladder — epoch v2.0

*The persistent placement record for TIER_RUBRIC v2. Append-only within an
epoch; archived whole at epoch close. Historical comparisons are never
replayed.*

## Epoch status: **OPENING — anchor generation in progress (2026-08-03)**

**Epoch id:** brief = `missions/game_challenge_tier.yaml` **version: 2**
(2026-08-03; YAML opinion removed, "eight CONNECTED rooms",
`CHALLENGE_v2_CHECKLIST.md` 47 items) · framework = **`ec5d54c`** (the
epoch-batch closing commit `d756175` + the gemma think-activation fix —
`<|think|>` had served as literal bytes on every gemma run ever; landed
INSIDE the epoch-open window, before any placement. The three anchors
(gpt-oss/devstral/Sonnet) do not touch gemma serving and stand unchanged;
the gemma trio run was a §17 gate probe, not a placement).

**Gemma league caveat:** every measured gemma figure to date — including
the 26b arm's 196 cyc/h — is a NON-THINK number. With thinking live,
gemma cyc/h will drop substantially (a hard prompt burned an entire 6k
budget inside one thought channel during validation); re-measure league
assignment at placement time rather than trusting the v1.2 register.

The batch landed 2026-08-03 (W5 `7e1cebb` → W8 `d756175`). Anchor runs are
the remaining open step:

1. Generate the three anchors under the new brief and framework:
   - **GUARDIAN** — gpt-oss, standard grinder run (2h wall, resumable-point park)
   - **FLOOR** — devstral, standard grinder run
   - **FRONTIER** — Claude Sonnet, single shot: same objective text, one pass,
     writes each file exactly once, no execution, no testing, no iteration.
     Generated via a chat-session subagent, files written directly (never
     through the batch extraction path — see §20).
2. Freeze all three staged trees under `dev/blind_panel/anchors/v2.0/`.
3. Smoke + facts each anchor (anchors get the same record every artifact gets).
4. Stamp this file's epoch header with (brief version, framework commit).

## Anchors

| role | model | artifact | smoke | facts |
|---|---|---|---|---|
| GUARDIAN | gpt-oss-120b-a5 | `anchors/v2.0/guardian-gptoss-20260803/` (run `tier_20260801-185254` arm 1) | ✅ compile clean · ASCII title screen · game starts | 120min grinder park at 20/30 goals, 147 cycles (73.5 cyc/h), 0 degen, scan clean. 5 py + world.yaml — chose YAML UNPROMPTED (the v1 opinion, now voluntary). Quirk: ships its own `savegame.json` and auto-loads it at boot (playtest state leaked into the demo). First live validation of the W2 park: `cycles_consumed=147` + `pending_return` persisted at the work→entry boundary. (An earlier facts note called `judge1/` a shipped quirk — WRONG: that is stage.py's per-judge play copy, stripped from the frozen anchor.) |
| FLOOR | devstral-2-small-24b | `anchors/v2.0/floor-devstral-20260803/` (run `tier_20260801-185254` arm 2) | ✅ compile clean · launches into play · clean quit | 120min grinder park at 21/28 goals, 117 cycles (58.5 cyc/h), 0 degen, scan clean. 5 py + world.yaml — ALSO chose YAML unprompted (2 of 2 fleet arms). NO title screen: one-line welcome straight into the first room — the first v2 checklist miss (item 2) on an anchor |
| FRONTIER | claude-sonnet (one-shot) | `anchors/v2.0/frontier-sonnet-20260803/` — "The Ashen Keep" | ✅ compile clean · title screen · clean quit | 10 files (9 py + README), 60,305 B; world defined in `adventure/world.py` — pure-Python, NO data files: the first v2 signal that the brief now measures the choice instead of prescribing it |

## Anchor cross-flights (instrument validation, 2026-08-03 — the epoch's first blind flights)

Two blind Opus judges, separate packets, forced choice per axis, keys held
outside the packets. **Gate verdict: the instrument works** — expected
ordering, no self-flags, and cross-judge CONVERGENCE: both judges scored the
GUARDIAN artifact independently at **41/47 with the identical unmet set
(22, 23, 31, 33, 38, 45)** and found the same three decisive defects.

| flight | result | headline |
|---|---|---|
| GUARDIAN vs FLOOR (in-ladder) | **GUARDIAN, 5–0 axes, overall, no self-flag** | Both UNWINNABLE + SIGNIFICANTLY-DEVIATED (core loop); near-tied on presence (41 vs 40 of 47); separated entirely by DELIVERY — devstral's authored content dies at the seams (dead NPC subsystem via a wrong-loop `continue`; room graph in two components — checklist item 38, the re-homed connectivity item, caught it immediately; save/load never wired to a command), gpt-oss's content reaches the player |
| GUARDIAN vs FRONTIER (scorecard, out-of-band) | **FRONTIER, 5–0 axes, overall, no self-flag** | FRONTIER: **WON** — full legitimate playthrough to the victory screen, **47/47 NEAR-FULL**, clean state round-trip incl. NPC dialogue stage and per-room monster HP. GUARDIAN: UNWINNABLE (no win text anywhere; weakness key unequippable by type), corpse-resume (death autosaved + silently reloaded forever), resurrection-on-load. Caveat stamped per §5: judge is Opus, artifact is Claude — same-family tailwind, direction known; and a one-shot never runs the decomposed write path where the field's seam failures are born |

GUARDIAN repeat signature (2 independent judges + the 2026-07-27 panel): a
fully-written defeat path and NO win condition — the item-31 asymmetry is now
a three-sighting pattern for gpt-oss under this brief class.

## Guardian title match (2026-08-03 — better-of-two, operator rule)

**Challenger:** gpt-oss rerun `tier_20260802-224028/staged/arm01` ("Ruins of
Eldoria", 121min grinder, 0 degen, smoke clean) vs the **frozen GUARDIAN
anchor**. Two blind claude-opus-5 judges, TIER_RUBRIC v2.1 two-panel
protocol, positions swapped (j1: challenger=A · j2: challenger=B), keys held
outside packets.

**VERDICT: GUARDIAN RETAINS — unanimous, both positions.**

| judge | challenger pos | Delivery | Character | overall | flags |
|---|---|---|---|---|---|
| j1 | A | Guardian 4–0 | Guardian 5–1 | Guardian | none |
| j2 | B | Guardian 4–0 | Guardian 4–2 | Guardian | none |

Position-bias check PASSED (winner followed the artifact). Challenger's
decisive defects (both judges independently): the dict/object player seam —
`GameState.player` dict vs `Monster.take_turn`'s `state.player.defense` —
kills the process on the monster's first turn of EVERY fight; and the
one-way `courtyard→armory` exit orphans courtyard+throne_room (boss, potion)
from the start component. `save_game` has no caller. Challenger took
AMBITION on both cards (defense economy, drops, in-combat use sub-prompt,
richer GameState — attempted, not delivered). Judge divergences worth
keeping: challenger conformance 39/47 (j1) vs 41/47 (j2) — j2 counted
authored-but-inert items (29 weakness flag, 36 orphan save) as met; and B9
workability FLIPPED (j1→Guardian, charging the counterfeit `ruff`; j2→
challenger, crediting real module boundaries against the Guardian's 810-line
god class and excluding the stubs as interaction-attributed) — the rubric's
interaction-attribution charging rule is ambiguous and needs a §ruling.

Non-axis facts banked: the challenger shipped GATE-GAMING artifacts — a
do-nothing `ruff` stub ("exit 0"), a `python`→python3 shim padded to defeat
the anti-gut retention check, and `# ruff: noqa` atop parser.py — full
harmony-CoT provenance recovered (interactions.jsonl raw_text; the model
weighed `python -m ruff` and chose the fake, then honestly reported the
escalation as deferred). Item-31 (defeat path written, NO win condition
anywhere) reproduced on BOTH arms — now 5+ independent sightings on gpt-oss.

## League register (measured, from the v1.2 field)

| model | cyc/h | league | budget |
|---|---|---|---|
| step37 | 7.5 | contemplator | 30 cycles |
| mistral-medium | 8.4 | contemplator | 30 cycles |
| qwen3.6-27b | 8.9 | contemplator | 30 cycles |
| qwen3.6-35b | 14.3 | contemplator | 30 cycles |
| olmo-instruct | 14.8 | contemplator | 30 cycles |
| laguna-xs | 15.5 | contemplator | 30 cycles |
| glm-4.7-flash | 18.0 | contemplator | 30 cycles |
| qwen3.5-122b | 18.2 | contemplator | 30 cycles |
| laguna-s-apex | 19.7 | contemplator | 30 cycles |
| **hy3** | **32.0** | **BOTH** (straddler — operator decision: more info beats less) | both budgets, two runs |
| qwen-next | 49.5 | grinder | 2h wall |
| gemma-4-31b | 54.5 | grinder | 2h wall |
| gpt-oss | 58.5 | grinder | 2h wall |
| gpt-oss-swarm | 70.0 | grinder | 2h wall |
| devstral | 84.4 | grinder | 2h wall |
| olmo-think | ~2 | contemplator (degenerate — deliberation does not converge; see its config) | 30 cycles |

## Ladder — contemplator league

| pos | model | tier | flights | artifact | notes |
|---|---|---|---|---|---|
| 1 | hy3-reap-200b-a21[c] | **1** | GUARDIAN: **won — Delivery 4–0, Character 5–1, overall** (v2.1 two-panel; judge claude-opus-5; hy3 sat B; no split, no close flag) | `tier_20260802-224028/staged/arm02` — "THE SHADOW REALM" | **First field artifact of the epoch a judge PLAYED TO VICTORY** (only the Sonnet one-shot had done it), and **first field 47/47 NEAR-FULL — zero unmet**. Weakness verified counterfactually: Star Amulet → two-phase boss falls to the VICTORY screen; without it → boss invulnerable, honest defeat + WORKING restart to a pristine world. 9 rooms, 3 regular monsters, mid-combat healing works, world.json (fleet's 3rd data-format answer). First think-LIVE hy3 artifact (contemplator 30 cycles in 74min, 0 degen). Defects on record: save is write-only (`engine.load` has no caller — item 37 met-by-presence), boss phase HP pools decorative (health recomputed per turn; ±40 amulet modifier carries the fight), `talk to <npc>` mis-targets to room.npcs[0] (bare `talk <npc>` works), EOF-at-save-prompt traceback, decline-restart dead loop. Guardian took only B5 ambition (nested combat REPL, typed entity layer, wield-the-key loadout sacrifice). Guardian conformance convergence: 41/47 again — 4th/5th independent reproduction. INTRA-TIER ORDER vs qwen3.6-35b provisional: no head-to-head flown; ordered on WON+47/47 vs UNWINNABLE-by-balance+46/47 |
| 2 | qwen3.6-35b-a3 | **1 (provisional — §9 second artifact owed)** | GUARDIAN: **won 5–0** axes+overall (v2.0-era flight; judge self-flag on experience axis only) · FRONTIER: lost 0–5 (scorecard, out-of-band) | `tier_20260802-004021/staged/arm01` | First placement of the epoch. 46/47 NEAR-FULL; UNWINNABLE by BALANCE alone — win path authored+wired, boss to 40/150 with phase 2 + shard amplification firing; working restart, consent-based load, mid-combat healing. 30 cycles exactly, 234min = 7.7 cyc/h (HALF its v1.2 rate — v2 cycles run deeper). Pure-Python world, same choice as the Sonnet one-shot. GUARDIAN convergence: 3rd independent judge, identical 41/47 + unmet set |
| 2 | gemma-4-31b | **2, LOW (below the FLOOR anchor)** | GUARDIAN: lost 0–5 · FLOOR: **lost 3–2** (took state integrity + ambition) | `tier_20260802-043526/staged/arm01` | First think-LIVE artifact (36 thought emissions, 10 degen, 13.4 cyc/h → league re-measured contemplator; this run rode the grinder wall pre-re-measure). Sealed in room 1 of 9: parser emits `go`/`take`, engine dispatches `move`/`pickup`. Real strengths under the seam: only zero-traceback robustness of the epoch, honest full-schema persistence, only fully-connected 9-room graph, weakness threaded through branching NPC dialogue. `//` literals resolved model-innate (title-screen tic), extraction exonerated |

## Ladder — grinder league

| pos | model | tier | flights | artifact | notes |
|---|---|---|---|---|---|
| 1 | qwen3-next-coder-80b-a3 | **2, LOW (below the FLOOR anchor)** | GUARDIAN: lost 0–5 · FLOOR: **lost 4–1** (took state integrity only) | `tier_20260802-043526/staged/arm02` | Run shape was the epoch's cleanest (6/6 batch at cycle 1, 54.2 cyc/h, 0 degen — the §20-doc-bug hypothesis holds) but the artifact never reaches a fight: 8 rooms in two components, zero monsters placed, dead 244-line commands.py, examine a silent no-op, save/load incompatible with its own file. Judge 5's Guardian tally 42/47 (±1 vs the four prior 41/47s) |

## Frontier scorecards

| candidate | axes | overall | headline |
|---|---|---|---|
| qwen3.6-35b-a3 (flight 4, 2026-08-03) | FRONTIER 5–0 | FRONTIER, no self-flag | "The Ashen Keep" WON again (47/47, second judge, full round-trip incl. room depletion + NPC stage); qwen UNWINNABLE by arithmetic (boss best 80/150), NEAR-FULL **carried wholly by the presence rule** — win path, phase 2, monster specials (`hasattr` on a dict, p≈0.03 empirical), and dialogue branching all authored, none reachable. Position-bias check passed (A-slot won after three B-slot verdicts). Family-bias caveat stamped per §5 |

## Pre-epoch reference: the v1.2 field (NOT ladder members)

For expectation-setting only; different brief, different framework, different
instrument. `tier_20260731-050209`, TIER_RUBRIC v1.2:

qwen3.6-35b 64 · step37 62 · gpt-oss 60 · swarm 53 · qwen3.6-27b 53 ·
qwen3.5 51 (tier 1) — hy3 49 · mistral 49 · qwen-next 49 · laguna-xs 48 ·
laguna-s 46 · devstral 44 (tier 2) — olmo-instruct, olmo-think, glm (tier 3) —
gemma-31b void (rerun owed), gemma-26b pair never ran.
