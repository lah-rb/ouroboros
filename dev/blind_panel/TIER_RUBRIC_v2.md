# TIER_RUBRIC v2 — comparative anchored-ladder protocol

**An epoch-scoped comparative instrument. The record is a tier, a ladder
position, a set of facts, and a paragraph — there is no total score.**

Adopted 2026-08-01 (operator + assistant design sessions, in-chat drafts rev
1–3). Supersedes `TIER_RUBRIC_v1.md` for all future campaigns; the v1.2
records of `tier_20260731-050209` stand as written and are pre-epoch
reference, not ladder members.

## Why v1 was retired — the two-sentence version

Measured on its own campaign, v1.2's usable range was 44–64 of 100: ~80 points
were near-constants of the task, the scaffold, or the judge (pedestal,
multiplicative floor, scaffold-dictated dimensions, one hard cap firing on
everyone), and 9 of 12 artifacts landed in the ★★★ rejudge band, costing 30
judge-sessions for 12 artifacts. Pairwise forced choice is the
higher-reliability primitive (external literature and our own blind-pairwise
data agree), and the expensive parts of v1 — full blind judging of obvious
tier 3s, point-resolution in the middle of the field — bought nothing a
decision ever used.

---

## 0. Units

A judgment attaches to an **artifact**. A tier attaches to a **model + our
integration**, and requires the artifact evidence in §9. The full record of
any placement is: the smoke record, the facts sheet, the flight verdicts with
axis scorecards, and the judge's paragraph.

## 1. Epochs

An **epoch** is a (brief version, framework version) pair. Any change to
either **archives the ladder, regenerates all anchors, and opens a new
epoch**. Cross-epoch comparison is not supported — this was always true of the
instrument and is now explicit. Epoch id appears in every record.

**Epoch v2.0 opens when the pending batch lands** (OPEN_TASKS §17–§20, the
generic state-ownership design decision, the YAML-data opinion removed from
the brief, the league run protocol below). Until then, no v2 placements are
made. The v2.0 ladder starts EMPTY except for anchors: the v1.2 field cannot
seed it (different brief, different framework); it informs expectations only.

**Epoch v2.0 brief = `missions/game_challenge_tier.yaml` version: 2**
(2026-08-03): YAML-data-files sentence removed, "eight CONNECTED rooms";
checklist = `CHALLENGE_v2_CHECKLIST.md` (47 items, binary verdict). The
framework-commit half of the epoch id is stamped in LADDER.md at open.

## 2. Leagues and the run protocol

Models divide by **measured strategy**, not declaration: cycles per wall-hour
from their runs. The v1.2 field was cleanly bimodal — contemplators at
7.5–19.7 cyc/h, grinders at 49.5–84.4, exactly one straddler:

| league | members (measured, v1.2 field) |
|---|---|
| contemplators (<20/h) | step37 7.5 · mistral 8.4 · qwen3.6-27b 8.9 · qwen3.6-35b 14.3 · olmo-instruct 14.8 · laguna-xs 15.5 · glm 18.0 · qwen3.5 18.2 · laguna-s 19.7 · (olmo-think ~2/h, degenerate) |
| straddler | **hy3 32.0/h — runs in BOTH leagues**; the divergence between its two placements is the first empirical measure of what league assignment is worth |
| grinders (>45/h) | qwen-next 49.5 · gemma-31b 54.5 · gpt-oss 58.5 · gpt-oss-swarm 70.0 · devstral 84.4 |

Budgets:

- **Contemplators: 30 work cycles, hard cap.** Chosen at the field's central
  tendency (contemplator-only mean 28.3, median 31, mode 31) — the cap judges
  performance near the center rather than at each model's stretch. No grace
  cycles; the smoke (§3) handles boundary catches. Deliberate consequence:
  laguna-s (40), qwen3.5 (37) and glm (36) get less runway than the old 2h
  wall gave them.
- **Grinders: 2h wall**, retained because the contemplator distribution was
  measured under it, **parked at the next resumable point** (post-recert
  boundary), never mid-action. **Wall clock is a scorable level for
  grinders** — it is the axis their strategy spends.
- Both leagues report cycles, wall time, and tokens as covariates in the DoE.
- **Comparisons are league-fair.** A contemplator's standing is stated among
  contemplators, a grinder's among grinders. Cross-league claims flow only
  through anchors (§5) and the completion duel (§10).

## 3. Stage 0 — the smoke (unblind mini-judge)

Runs BEFORE any blind judging is spawned, on every arm.

**Judge:** any Sonnet / Opus / Fable model in the operator's chat session —
off-box, so it neither competes for local serving resources nor convolutes the
local picture. **Unblinded**, with run-log and workdir access. This is the one
sanctioned exception to the never-judge-unblind rule, and it may never fly a
comparison flight.

**Five questions, all answered in the record with quoted evidence:**

1. Does the program start and accept one command?
2. If not — **what** is broken, precisely?
3. **Severity** — entry-point dead / subsystem dead / cosmetic?
4. **Since when** — broken from which cycle, per the log?
5. **Did the model know** — diagnosis on record, repair dispatched, fix
   content in flight?

**Outcomes:**

- **RUNNABLE** → facts pass, then placement.
- **BROKEN, known and unaddressed** (or never built) → **TIER 3**, recorded
  with the smoke's full documentation. No blind judging is spent. Single run
  suffices. (v1.2 spent a full judge session learning olmo-instruct's loop was
  `while playing: pass`; the smoke calls that in minutes.)
- **BROKEN with courtesy-fix eligibility** → minimal mechanical fix,
  disclosed, then judged normally. Eligibility is two-sided:
  - **(a) fix in flight** — the log shows the model had identified THIS defect
    and was repairing it when the budget hit (the devstral case: re-cert
    caught the leaked envelope, repair dispatched 19 log lines before the
    wall);
  - **(b) boundary mistake without opportunity** — the breaking change landed
    at or so near the stop that no verification cycle ran after it; the model
    *could not* have known. Distinguished from (a) by log position, and from
    tier-3 negligence by recency: a defect the model had cycles to observe and
    did not is neither.
  - Scope: the smallest edit implementing the model's own evident intent
    (strip a leaked envelope, apply the fix its diagnosis names). **One fix
    maximum.** If it does not produce a runnable artifact, tier 3 stands. The
    shipped, unrepaired state remains the record's truth, disclosed wherever
    the artifact is cited.

## 4. The facts pass (recorded, never ranked)

Gathered during the artifact's first blind play, as facts with quoted
evidence:

- **Completability class** — the headline:
  `WON / WINNABLE-NOT-WON / UNWINNABLE / NO-TERMINAL-STATES`.
  Playing toward the win is mandatory judge effort.
- **Conformance, binary**: `NEAR-FULL / SIGNIFICANTLY-DEVIATED`.
  Deviated = any missing element of the brief's core loop (terminal states,
  the central mechanic chain), **or** >20% of the checklist. The presence rule
  is retained inside the binary: authored-but-unreachable counts as present
  here and is charged on the axes.
- **State integrity note** — does persistence/reset round-trip without
  rewriting the world?
- **Robustness battery** — the ten probes, reported as a fact table with the
  worst impact named; not banded, not scored.
- **Attribution labels** on every observation:
  `model-innate / interaction / framework-coupled`. Framework-coupled
  observations (project layout; documentation until OPEN_TASKS §20 lands) are
  recorded and **excluded from comparison**. The cautionary example is
  §20 itself: v1.2 judges docked every artifact's docs for a truncation OUR
  extraction caused.

## 5. Anchors

Frozen artifacts with staged trees kept forever. All anchors regenerate at
epoch open. **Two in-ladder, one out-of-band:**

| role | model | job |
|---|---|---|
| **GUARDIAN** | gpt-oss | *the tier boundary.* The St. Peter of Ouroboros — best-understood model in the fleet; hard to beat, absolutely beatable. **Beat the Guardian = tier 1. Lose = tier 2.** Every placement flies the Guardian first. |
| **FLOOR** | devstral | *the exemplar of minimally viable.* Separates low tier 2 from near-boundary tier 2. |
| **FRONTIER** | Claude Sonnet, single shot | *the thing to shoot for.* Same objective text, one pass, no agentic loop, no testing, no iteration. **Out-of-band**: Frontier flights produce an AXIS SCORECARD and never move the ladder or gate a tier. |

Frontier caveats, stamped on every Frontier verdict:

1. **Family bias.** Flight judges are Opus; same-family judges systematically
   favour family outputs, and self-preference operates through style
   recognition, so blinding does not cure it. The tailwind's direction is
   known and recorded.
2. **Cross-pipeline.** A one-shot never runs the decomposed write path, which
   is where the local field's dominant failure class (seams) is born. Frontier
   measures distance-to-frontier, not our integration.
3. **It might not be out of reach.** If a top-band artifact beats the Sonnet
   one-shot, that is not a broken anchor — it is the most interesting result
   the campaign can produce. Do not presume; the scorecard decides.

step37 is NOT an anchor: it is the strongest ladder member, free to be
overtaken, which is what a ladder member is for.

## 6. Placement

smoke → facts → **flight vs GUARDIAN (always first, every candidate)** →
- **winner** (tier 1): flight vs **FRONTIER** for the scorecard, plus at most
  one flight vs the nearest same-league ladder neighbour when league-top
  standing is at stake;
- **loser** (tier 2): flight vs **FLOOR** to separate low from near-boundary;
  optional nearest-neighbour flight only at a standing boundary.

**Flights:** one blind judge (subagent, packet, read-wall — METHODS.md is the
engine), plays both artifacts, **forced choice per axis** with a one-line
justification each (per-axis ties forbidden), then an overall forced choice
(overall tie allowed, recorded as a tied cluster).

**Escalation is exception-driven.** A 3-judge panel fires only when:
(a) the judge self-flags the overall as CLOSE;
(b) the flight decides a tier boundary or a league-top position;
(c) a transitivity cycle appears — the panel takes the whole triplet.

**The middle stays coarse.** Tied clusters are an accepted output; ordering a
tier's interior changes no decision and buys no information for its cost.

## 7. The axes

1. **working surface** — how much of what it offers works when invoked
2. **state integrity** — persistence/reset round-trips without rewriting the world
3. **experience** — UX, writing, discoverability
4. **delivered scope** — how much landed, absolute
5. **ambition** — which attempted more, weighted by what survived

Overall is its own forced choice, not a sum. Axes 4 and 5 deliberately pull
against each other: that tension is the anti-ambition bias made visible
instead of baked in, and reading them as a pair is how ambition is understood
on its own merits.

## 8. Tiers

- **Tier 3** — smoke-failed, documented by the smoke.
- **Tier 1** — beat the Guardian. **Tier 2** — lost to the Guardian.
  The boundary is a judged artifact with a year of track record, not an
  arithmetic threshold; moving it is an explicit epoch decision.

## 9. Model-level claims

- A config `tier:` record cites: epoch, league, flights (opponents, league
  relation, axis scorecards, judge count), smoke record, facts sheet.
- **Top-band and boundary placements require a SECOND artifact** before the
  config asserts the tier. Two artifacts that straddle widely are not
  averaged — **high run variance is the recorded finding.**
- Pre-registered expectations survive unchanged from v1 §6 — they attach to
  placements instead of scores, and are never revised after the fact.

## 10. The completion duel

Once the field is placed: the **top contemplator** and the **top grinder**
each run once TO COMPLETION — no cap, no wall. Deliverables: the first
completed artifacts and complete cycle/time/token counts; the answer to what
grinding actually buys given unlimited runway; the epoch's
champion-of-champions, decided on completed work rather than truncation luck.
Run-off format (best-of-N) is decided when the field is separated
(operator, plan-to-accept).

## 11. Cost model

Per new model: 1 smoke (chat-session, ~free locally) + 2–3 flights (one judge,
two artifacts each) + panels only on exceptions. Versus v1.2's measured 30
judge-sessions for 12 artifacts: roughly 3–4× cheaper, and adding a model
never replays the field. The duel is 2 uncapped runs + 1 flight per epoch.

## 12. Where the v1 dimensions went

| v1 dimension | v2 disposition |
|---|---|
| no_broken_functions (20) | **working surface** axis |
| robustness (10) | facts table (worst impact named) |
| ux (10) | **experience** axis |
| conformance (10) | **binary fact** (near-full / significantly-deviated) |
| ambition (10) | **ambition** axis (forced choice, own merits) |
| creativity (10) | **experience** axis |
| org: project (5) | dropped — scaffold-dictated at this brief size; labeled observation only |
| org: logic (5) | judge's paragraph |
| reusability (10) | real again after the YAML opinion is removed; modification probe stays as facts |
| documentation (10) | framework-coupled observation until §20 lands; then experience |
| completability (buried in 3.1 multipliers) | **the headline fact** |
| tier-3 gate (§2) | the smoke, with documentation and the courtesy fix |
| ★★★ rejudge (§5) | exception-driven escalation (CLOSE / boundary / cycle) |
