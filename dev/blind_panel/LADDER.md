# The Ladder — epoch v2.0

*The persistent placement record for TIER_RUBRIC v2. Append-only within an
epoch; archived whole at epoch close. Historical comparisons are never
replayed.*

## Epoch status: **OPENING — anchor generation in progress (2026-08-03)**

**Epoch id:** brief = `missions/game_challenge_tier.yaml` **version: 2**
(2026-08-03; YAML opinion removed, "eight CONNECTED rooms",
`CHALLENGE_v2_CHECKLIST.md` 47 items) · framework = **`d756175`** (the
epoch-batch closing commit: §17–§21 fixes, scope-don't-truncate, league
protocol + resumable-point park, state-ownership design, v2 brief).

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

*(empty — awaiting epoch open)*

## Ladder — grinder league

*(empty — awaiting epoch open)*

## Frontier scorecards

*(none yet)*

## Pre-epoch reference: the v1.2 field (NOT ladder members)

For expectation-setting only; different brief, different framework, different
instrument. `tier_20260731-050209`, TIER_RUBRIC v1.2:

qwen3.6-35b 64 · step37 62 · gpt-oss 60 · swarm 53 · qwen3.6-27b 53 ·
qwen3.5 51 (tier 1) — hy3 49 · mistral 49 · qwen-next 49 · laguna-xs 48 ·
laguna-s 46 · devstral 44 (tier 2) — olmo-instruct, olmo-think, glm (tier 3) —
gemma-31b void (rerun owed), gemma-26b pair never ran.
