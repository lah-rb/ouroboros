# TIER_RUBRIC v2.1 — comparative anchored-ladder protocol

**An epoch-scoped comparative instrument. The record is a tier, a ladder
position, a set of facts, and a paragraph — there is no total score.**

Adopted 2026-08-01 (operator + assistant design sessions, in-chat drafts rev
1–3). Supersedes `TIER_RUBRIC_v1.md` for all future campaigns; the v1.2
records of `tier_20260731-050209` stand as written and are pre-epoch
reference, not ladder members.

## Changelog

- **v2.1 §rulings (2026-08-03) — two ambiguities caught by judge divergence,
  ruled by the operator.**
  (1) **CHARGE WHAT SHIPS**: the attribution clause had defined exclusion
  only for framework-coupled and left `interaction` undefined — the Guardian
  title match's B9 flipped on exactly that (j1 charged a shipped counterfeit
  `ruff`; j2 excluded it as harness-relationship). Ruled: everything in the
  tree is in comparison; interaction observations are among the most
  important (harness-fit is a scored property — the campaign selects models
  FOR this framework); layout stays excluded (scaffold-dictated, zero
  discrimination); confirmed framework faults are handled by fix-and-discard
  at the operator level, never judge-side exclusion. Attribution clause in
  §4 rewritten; FLIGHT_PROMPT updated to match.
  (2) **Item 45 is presence-lenient; strength is comparative.** hy3's staged
  topic progression split two judges (47/47 vs 46/47). Ruled: staged
  progression MEETS the checklist item (weaker showing in play); the
  gradation — fixed single line < staged progression < state/choice-
  conditional dialogue — is judged comparatively on the axes (B6/B7), where
  it already lives. hy3's 47/47 stands. Checklist item annotated.

- **v2.1 (2026-08-03) — THE PANEL SPLIT.** v2.0 inverted its own stated
  purpose. Measured on its own first eight flights, **six were 5–0 sweeps**:
  four of five axes (working surface, state integrity, delivered scope, and a
  function-gated ambition) resolve one question, so the instrument measured
  delivery five times and reported it with five labels. Three specific
  crossings, all fixed here:

  1. **Ambition was gated on delivery.** §7 said the goal was ambition
     "understood on its own merits" and then defined the axis as "weighted by
     what survived" — a contradiction one line apart. Judges read it as
     written ("almost none of A's survived, so the axis goes to B"), which is
     the anti-ambition selection bias v2 was built to REMOVE, now codified.
     The gate is gone.
  2. **UX and UI were conflated, and UI won.** One "experience" axis absorbed
     v1.2's `ux` *and* `creativity`; judges spent it on help accuracy, status
     blocks, naming and case handling. Flight 6 is the proof: *"B writes
     better prose and has the real title banner, but it has no help
     whatsoever"* → the axis went against the better-written artifact. Split
     into felt play (UX) and surface craft (UI), with imagination its own
     axis.
  3. **Categories with no home.** `reusability` was declared "real again"
     with nothing built to catch it; `org` was dropped or demoted to "the
     judge's paragraph"; `documentation` was excluded as framework-coupled
     "until OPEN_TASKS §20 lands" — §20 landed 2026-08-02 (`a0ec769`) and
     nothing propagated, so eight flights excluded it on an expired
     condition.

  The deeper inheritance: v1.2's function coupling was already over-dominant
  (`creativity` required the choice to have "PAID OFF"; `ambition` was merged
  with `completeness` by design), and v2.0 amplified it by removing nearly
  every decoupled dimension. v1.2 §3.4 had written the correct principle for
  conformance alone — *"Conformance measures whether the model did what was
  asked; the other dimensions measure whether it delivered. Keeping them
  separate is what makes the decomposition informative."* **v2.1 generalises
  that split to every model-strength category**, via two panels that are
  reported separately and never summed.

  Cost is unchanged. v2.0 conflated *fewer sessions* with *fewer dimensions*;
  only the first was ever the driver (30 sessions for 12 artifacts came from
  ★★★ rejudges and full-judging obvious tier 3s). Ten forced choices in one
  session cost marginal tokens, not a second session.

- **v2.0 (2026-08-01)** — initial comparative instrument; superseded v1.2.

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
  **MISSING means ABSENT FROM THE TREE, not unreachable in play** — a win
  screen behind a broken door is present and does not trigger; a win that
  nothing in the code can ever set does. See the checklist's rule 1 for the
  full statement, including the operator's ruling that a conformant D&D
  character sheet would score 100% and still lose every axis. Conformance is
  never a quality score.
- **State integrity note** — does persistence/reset round-trip without
  rewriting the world?
- **Robustness battery** — the ten probes, reported as a fact table with the
  worst impact named; not banded, not scored.
- **Premise line** (v2.1) — two sentences on what this game *is*, in the
  judge's own words, plus **one quoted line of its prose**. Recorded before
  any axis is voted, never ranked. It exists because a record made only of
  verdicts tells you how an artifact worked and nothing about what it was;
  the field's own operator could not recognise his models from their records.
- **Attribution labels** on every observation:
  `model-innate / interaction / framework-coupled`.
  **CHARGE WHAT SHIPS (operator ruling, 2026-08-03).** Everything in the
  artifact tree is IN comparison regardless of attribution — the label is
  recorded so the operator can weigh cause, never a ground for a judge to
  exclude. In particular, `interaction` observations (how the model behaved
  WITH the harness — gate-gaming stubs, shipped saves, workaround shims)
  are among the MOST important comparisons this ladder makes: the campaign
  is looking for strong models FOR this framework, and harness-fit is a
  scored property, advantage or handicap. (Laguna-S is the canonical
  example: a genuinely interesting model that is handicapped under the
  framework because it cannot escape its own agentic priors — that is
  signal, not noise.) The one exception stands on its original ground:
  **project layout is excluded** because the scaffold dictates it
  identically for every arm, so it discriminates nothing. A suspected
  FRAMEWORK FAULT is the operator's business, not the judge's: when one is
  confirmed (the 2026-08-03 env-assert trap), the remedy is fix the
  framework and DISCARD the run — never per-observation exclusion inside a
  comparison. The cautionary example is OPEN_TASKS §20: v1.2 judges docked
  every artifact's docs for a truncation OUR extraction caused. **§20
  landed 2026-08-02 (`a0ec769`), so documentation is IN comparison as of
  v2.1** (axis B10) — the expiring condition v2.0 wrote and never
  propagated.

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
engine), plays both artifacts, records a premise line for each, then makes a
**forced choice on every axis of both panels** with a one-line justification
each (per-axis ties forbidden), then an overall forced choice (overall tie
allowed, recorded as a tied cluster).

**The two panel tallies are reported separately and never summed**, e.g.
`Delivery A 4–0 · Character B 5–1 · Overall —`. The overall remains its own
judgment and still decides the tier.

**A PANEL SPLIT HALTS THE PLACEMENT.** When the panels disagree on direction,
the judge flags it and the record is written, but **no tier is recorded and
no ladder row moves** until the operator rules. The operator chooses: accept
the judge's overall as called, make the call directly, or escalate to a
3-judge panel. This is the most informative outcome the instrument can
produce — an artifact that delivers more while reaching for less, or the
reverse — and under v2.0 it was structurally impossible, because every axis
voted the same question. It is a decision for a person, not a default.

**Escalation is otherwise exception-driven.** A 3-judge panel fires only when:
(a) the judge self-flags the overall as CLOSE;
(b) the flight decides a tier boundary or a league-top position;
(c) a transitivity cycle appears — the panel takes the whole triplet;
(d) the operator escalates a panel split.

**The middle stays coarse.** Tied clusters are an accepted output; ordering a
tier's interior changes no decision and buys no information for its cost.

## 7. The axes — two panels

Ten forced choices in two panels. **Panel A asks whether it works. Panel B
asks what it is.** They are tallied separately and never summed. A judge who
finds themselves writing the same sentence on both panels has made an error
on Panel B.

### PANEL A — DELIVERY *(does it work)*

- **A1 · working surface** — how much of what it offers works when invoked.
- **A2 · state integrity** — does persistence/reset round-trip without
  rewriting the world?
- **A3 · robustness** — behaviour under the ten probes: clean refusal vs
  traceback vs silent misinterpretation. The facts table is kept as well;
  this axis is the comparative read of it.
- **A4 · delivered scope** — how much landed, absolute.

These four are expected to correlate; that is not a defect, it is the same
question asked from four sides. **A 4–0 Panel A is ordinary and carries no
special weight.**

### PANEL B — CHARACTER *(what is it)*

- **B5 · ambition** — which artifact **reached further**, judged on the design
  it set out to build, **not on how much of it works**. An artifact that
  attempted a two-phase boss with a hidden weakness and failed to wire it
  reached further than one that shipped a single-phase boss cleanly.
  Delivery is charged on Panel A; charging it again here is double-counting.
  **If you are writing "but none of it survived", you are voting on the wrong
  panel.**

- **B6 · imagination — world & voice** — premise, place, prose, the
  non-obvious idea, **ignoring whether it works**. v1.2 required the choice to
  have "PAID OFF"; that gate is removed, because whether it paid off is
  precisely what Panel A measures. **A vivid unreachable world beats a
  generic reachable one on this axis**, and that inversion is the point:
  it is the only place in the instrument where imagination can win.

- **B7 · experience (UX) — felt play** — pacing, discoverability, feedback,
  tension, sense of place. Does the world reveal itself at a decent rate? Is
  combat tense or arithmetic? Are the NPCs worth talking to? **This is not
  the help text.**

  **JUDGE THE PLAYER'S EXPERIENCE, NOT YOURS (operator ruling, 2026-08-06).**
  You can read the source; a player cannot. Whenever you got past an obstacle
  by consulting the tree — learning that the room prints `Iron Sword` but the
  parser only accepts `sword`, that an NPC needs its bare id, that a verb the
  help omits actually exists — **that obstacle stands at full weight here, and
  counts AGAINST the artifact.** Say so explicitly in the record: name the
  wall and say you routed around it with knowledge the player has no way to
  obtain.

  This is not hypothetical leniency. A campaign judge wrote: *"A seam bug is
  not what stopped me — I got past it by reading `data/world.yaml` to learn
  the keys, which a player cannot do."* Two artifacts in that campaign printed
  names they then refused, and the defect reads as a nuisance to a judge with
  the data file open and as an unplayable game to everyone else. The advantage
  is systematic, so the correction has to be too.

- **B8 · craft (UI) — surface** — help accuracy, status legibility, naming,
  error messages, input tolerance, prompt hygiene. Separated from B7 because
  in v2.0 it silently outvoted felt experience: a checkbox layer is easy to
  audit and therefore dominates a merged axis.

  **An artifact that will not accept the names it prints fails INPUT TOLERANCE
  here** — that is the mechanism, and it is charged on this axis. B7 charges
  the consequence (you cannot play it). Both are legitimate: the surface fault
  and the felt cost are different findings, not one finding counted twice.

- **B9 · workability** — could a person work in this? Logic organisation
  (module boundaries, whether the seams are where you would put them),
  project organisation, and reusability as one question, because they
  compound into a single property rather than trading against each other.
  **Mandatory modification probe:** add a ninth room, or change a weapon's
  damage, without touching unrelated code — report what broke. Project
  *layout* stays labeled framework-coupled at this brief size; logic
  organisation does not.

- **B10 · documentation** — accuracy against play first (a claim contradicted
  by play is worse than no claim), then completeness: would this let a
  stranger run and extend the thing? In comparison as of v2.1 (§4).

### Reading the panels

Panel A and Panel B are designed to be able to disagree, and the disagreement
is the finding — see §6. Facts (completability, binary conformance, the
robustness battery, the premise lines) are never ranked and never voted.

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

**v2.1 changes nothing here, and that is the correction.** v2.0 cut the
instrument from ten dimensions to five believing dimension count was a cost;
it never was. The 30 sessions came from ★★★ rejudges and full-judging obvious
tier 3s — *session* count. A judge in a flight has already read the rubric,
built the packet, played both artifacts and probed them; ten forced choices
instead of five is marginal tokens inside one session. **Coverage is cheap;
sessions are expensive.** v2.1 buys back v1.2's full coverage at v2.0's price.

## 12. Where the v1 dimensions went

Every v1.2 dimension has a home in v2.1. Where v2.0 dropped, merged or
deferred one, the v2.1 column is the repair.

| v1.2 dimension | v2.0 disposition | **v2.1** |
|---|---|---|
| no_broken_functions (20) | working surface axis | **A1** working surface |
| robustness (10) | demoted to facts table | **A3** + facts table retained |
| ux (10) | merged with creativity | **split: B7** felt play + **B8** surface craft |
| conformance (10) | binary fact | **binary fact** (unchanged — v1.2 §3.4 got this right first) |
| ambition (10) | axis, but "weighted by what survived" | **B5**, delivery gate REMOVED |
| creativity (10) | absorbed into experience | **B6** imagination, "PAID OFF" gate REMOVED |
| org: project (5) | dropped | **B9** (layout still labeled framework-coupled) |
| org: logic (5) | "judge's paragraph" | **B9** |
| reusability (10) | declared real, nothing built | **B9**, modification probe MANDATORY |
| documentation (10) | excluded on a condition that expired | **B10**, in comparison |
| completability (in 3.1 multipliers) | the headline fact | **headline fact** (unchanged) |
| tier-3 gate (§2) | the smoke + courtesy fix | unchanged |
| ★★★ rejudge (§5) | exception-driven escalation | unchanged, plus the operator's panel-split call |
| — | state integrity (new) | **A2** — a v2.0 addition that earned its place |
| — | delivered scope (new) | **A4** — likewise |
