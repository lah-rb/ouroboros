# Blind panel — 2h quality tier sweep, 2026-07-31

Rubric **TIER_RUBRIC v1.2**. Artifacts from `~/ouroboros-runs/tier_20260731-050209`:
mission `game_challenge_tier`, `top_phase=quality`, **2h backstop**, 18 arms,
budget 40h. Judges are blind subagents that see only a packet
(`make_judge_packet.py`) — artifact + rubric + checklist — and are walled off
from the run tree, the repo, and every config and trace.

Judge model, all records: `claude-opus-5[1m]`.

## ⚠️ SAMPLING REGIME SPLIT — read before comparing arms across this file

A loader defect found on 2026-07-31, **after arms 01–03 had run**, discarded
every configured zero: `_build_generate_kwargs` used `gen.min_p or 0.05` and
`gen.top_k or 40`, and Python treats `0`/`0.0` as falsy. Configs that declared
the vendor-canonical "disabled" value were silently overridden.

| arm | declared | actually served |
|---|---|---|
| 01 laguna-s-2.1-apex | `min_p 0.0` | `min_p 0.05` |
| 02 laguna-xs-2.1 | `min_p 0.0` | `min_p 0.05` |
| 03 gpt-oss-120b-a5 | `min_p 0.0`, `top_k 0` | `min_p 0.05`, **`top_k 40`** |

Fixed the same day; the sweep was **not** restarted and the affected arms were
**not** re-run (operator decision: annotate). So:

* **arms 01–03** were scored under the pre-fix loader,
* **arms 04–18** run the declared, vendor-canonical values.

Compare within a group freely; across the boundary, note it. Arm 03 is the one
to treat most carefully — `top_k 40` against a declared no-limit is a
materially different sampler, not a tail trim.

None of the decisive defects recorded below are sampling-reachable (a victory
flag never assigned; a loader ignoring a key its own data file writes), so the
*findings* are unaffected. The **scores** carry the caveat.

**Escalation rule in force:** a first verdict landing in the ★★★ 40–59 band
triggers §5 rejudge-to-place; the recorded score is the **median of three**
independent judgments. Verdicts outside that band stand on one.

---

## Arm 01 — laguna-s-2.1-apex

**RECORDED: 46 / 100 · ★★★ · median of three** (46 / 46 / 46).
Run: 120 min (parked at the backstop), 13 files, 7 py_ok, 4 degenerations,
13/39 goals.

| § | dimension | J1 | J2 | J3 |
|---|---|---|---|---|
| 3.1 | no broken functions | 3 | 4 | 4 |
| 3.2 | robustness | 2 | 2 | 2 |
| 3.3 | UI \| UX \| utility | 6 | 6 | 6 |
| 3.4 | conformance | 9 | 9 | 9 |
| 3.5 | ambition \| completeness | 3 | 3 | 3 |
| 3.6 | creativity | 5 | 5 | 5 |
| 3.7 | org: project | 4 | 4 | 4 |
| 3.8 | org: logic | 3 | 3 | 3 |
| 3.9 | reusability \| modification | 8 | 7 | 7 |
| 3.10 | intent \| documentation | 3 | 3 | 3 |
| | **total** | **46** | **46** | **46** |

### Decisive defect — the game cannot be won

`game_won` is never assigned anywhere in the tree, so the `*** CONGRATULATIONS ***`
branch at `engine.py:185` is dead code. **All three judges proved this by play
rather than inference**: each reduced the boss's HP by data edit, killed it, and
got the ordinary prompt back — no ending. The defeat path is fully wired and
fires correctly. Three further blocks stack underneath it: the boss-weakness
item (`blessed_crucifix`) is authored only as the boss's own drop, so it is
obtainable only after the fight it exists to enable; the sole armour
(`leather_armour`) declares slot `torso`, which is not one of the player's six
slots; and `equip` never applies anything regardless.

### The structural signature — competent modules, wrong seams

Every headline system is present, individually clean, and wired to a key its
counterpart does not use:

| system | authored | wired to |
|---|---|---|
| `_handle_equip` | complete and correct | `parser.py` maps `equip`→`use`, so the engine's `equip` branch is unreachable |
| `DialogueEngine` | indexes `id`, `next: str` | YAML authors no `id` and `next: [list]` → `TypeError: cannot use 'list' as a dict key`, process dies |
| `_use_key_item` | reads `exit_info["key"]` | YAML writes `key_item` |
| phase system | requires `behavior == "boss"` | YAML says `phased`; phase 1 overwrites with `aggressive` |
| menu "Continue" | reads `savegame.json` | `save` writes `data/save.json` |
| `_monster_weakness` | tracked | never read by any damage calculation |
| `engine.VERBS` (46 entries) | — | dead; nothing reads it |

§3.4 conformance **9/10** beside §3.1 **3–4/20** is the finding: the model did
almost everything asked and delivered almost none of it. That decomposition is
exactly what the presence/reachability split exists to expose.

### §3.2 — 2/10 on a nine-of-ten-CLEAN battery

Impact banding keys off the worst edge, and there is a HOST-DAMAGING one: **EOF
on piped or redirected stdin produces an unbounded `> ` loop** — independently
measured at 8.06 MB/15 s, 8.98 MB/20 s and 9.43 MB/20 s; never exits. Root
cause: `_read_line` catches `EOFError` and returns `""`, and the loop does
`if not raw.strip(): continue`. Judge 2 additionally found TTY EOF **at the
title menu** dies on an uncaught `EOFError`. Judge 3 additionally found that a
**valid-JSON / bad-schema** save is applied before validation, leaving the
session at `Health: abc/20`, `You are nowhere.`, unrecoverable — SESSION-ENDING,
where truncated JSON (what the other two probed) refuses cleanly.

### Inter-judge agreement — a result about the instrument

Three blind judges returned **the same total**. Only §3.1 (3/4/4) and §3.9
(8/7/7) varied, by one point, and they offset. All three scored conformance
46/53 and named six of the same seven unmet items (J3 cited 41 where the others
cited 42).

This is v1.2's first three-vote outing since amendment 1 made §3.1
multiplicative. That amendment was written because the additive scheme had two
of three judges floor the same *winnable* artifact at 0/20 — the instrument was
ranking the NUMBER of root causes over their severity. The replacement produced
3/4/4 on an artifact with 8–10 ledger rows. It did what it was designed to do.

### Notes

- The `!! MODEL-NAME LEAK` marker on this arm in `batch.log` is **stale**. It
  fired on `apex` in the artifact's self-chosen game title
  (`apex-text-adventure`), which collided with a quant label. `apex` was
  removed from `MODEL_TOKENS` (operator decision, 2026-07-31 — see the note
  there); the judges' packets scanned clean and the artifact is unmodified.
- All three modification probes (§3.9) landed first try for every judge. The
  data layer genuinely extends without touching Python, as the brief asked —
  new rooms and items are a `world.yaml`-only change.

---

## Arm 02 — laguna-xs-2.1

**RECORDED: 48 / 100 · ★★★ · median of three** (46 / 48 / 48).
All three votes fell below 50, so **tier 2** by §5's "side taken by ≥2 of 3".
Run: 120 min (parked at the backstop), 8 files, 6 py_ok, **13 degenerations**,
8/36 goals.

| § | dimension | J1 | J2 | J3 |
|---|---|---|---|---|
| 3.1 | no broken functions | 5 | 5 | 6 |
| 3.2 | robustness | 5 | 5 | 5 |
| 3.3 | UI \| UX \| utility | 6 | 6 | 6 |
| 3.4 | conformance | 9 | 9 | 9 |
| 3.5 | ambition \| completeness | 3 | 3 | 3 |
| 3.6 | creativity | 4 | 5 | 5 |
| 3.7 | org: project | 3 | 3 | 3 |
| 3.8 | org: logic | 2 | 2 | 2 |
| 3.9 | reusability \| modification | 6 | 7 | 6 |
| 3.10 | intent \| documentation | 3 | 3 | 3 |
| | **total** | **46** | **48** | **48** |

### Decisive defect — a working game behind one unread YAML key

`world.py:load_world` builds reachability from each room's own `exits:` map and
**ignores the file's top-level `connections:` block**. Four of eight authored
rooms are orphaned, taking with them the Crystal Shard, the third monster, the
two-phase Crystal Guardian and the only win condition. Both NPCs spend their
entire dialogue directing the player to a basement with no entrance.

Judge 1 proved the content was sound by adding **three exit lines to
`world.yaml` and nothing else**: the Crystal Spider fought, the boss appeared,
phase two triggered, and `=== VICTORY! ===` printed. Judge 2 found the block
could not have worked even if read — it maps Hallway-south to `room_entrance`,
`room_throne` *and* `room_maze`.

**Co-decisive and fatal alone:** every branch of `process_command` returns
before the shared monster-turn block, so `CombatEngine.monster_attack`, the
whole `MonsterAI` class and `show_defeat()` are unreachable for any input. The
player never lost a hit point across ~20 fights unarmoured. The artifact is
**unwinnable AND unloseable — no reachable terminal state of any kind.**

**And the weakness item is inverted.** Phase two triggers on merely *holding*
the Crystal Shard and sets `current_health = 1.5 × max_health`, `attack × 1.5`.
The item designed to make the fight winnable heals and buffs the boss.

### DISCLOSURE — the packet contaminated this record

The blind packet's workdir redaction substituted the literal string `<arm>`
into `explore.sh`. **All three judges remarked on it**, and judge 3 cited it as
one of three reasons for §3.7 = 3/5 ("a stray `explore.sh` containing a literal
`<arm>` placeholder ships in the deliverable"). A blinding measure was read as
the model's sloppiness and plausibly cost a point it did not earn.

All three judges saw the identical packet, so the votes are internally
comparable, and the affected dimension was unanimous at 3 regardless. The
substitution now writes `run` — an ordinary directory name — so arms 03-18 are
unaffected. Recorded rather than silently corrected: this record is worth one
point less of trust on §3.7 than the others.

### Notes

- 13 degenerations, **all long-cycle** (ratios 0.010–0.108, periods 80–3531 B),
  every one caught by the guard. Contrast arm 01, whose failure was RAMBLING at
  distinct-ratio 0.651 — invisible to the same guard. Two APEX-family quants at
  different base sizes, two different degeneration shapes.
- This arm was **clean (degen=0, completed in 17 min) under the 20-minute smoke
  backstop**. The claim that laguna-XS does not degenerate was an artifact of
  the cap and has been withdrawn from the laguna-s config.

---

## Arm 03 — gpt-oss-120b-a5

**RECORDED: 60 / 100 · ★★★ (top of band, borderline ★★★★) · TIER 1.**
Single blind judge, **operator-corroborated** rather than rejudged — see below.
Run: 123 min (parked at the backstop), 14 files, 7 py_ok, 0 py_fail, 0
degenerations, **25/35 goals**. Staged clean, no redaction.

**THE FIRST COMPLETABLE ARTIFACT OF THE SWEEP, AND THE WIN IS EARNED.**

| § | dimension | score |
|---|---|---|
| 3.1 | no broken functions | 9/20 |
| 3.2 | robustness | 6/10 |
| 3.3 | UI \| UX \| utility | 7/10 |
| 3.4 | conformance | **10/10** (51/53) |
| 3.5 | ambition \| completeness | 5/10 |
| 3.6 | creativity | 5/10 |
| 3.7 | org: project | 4/5 |
| 3.8 | org: logic | 3/5 |
| 3.9 | reusability \| modification | 8/10 |
| 3.10 | intent \| documentation | 3/10 (hard cap) |

### The win, and why it counts

Verified by play, both directions: sword + shield (Attack 5→10, Defense 0→3),
kill the Orc, then 8 turns against the 80 HP Dragon Lord at 14 dmg/turn
incoming, healing herb at 15 HP, land the kill. **Unarmed and unarmoured the
same dragon kills you in 5 turns** — defeat screen and restart both fire. The
win requires the weapon, the armour and correct mid-combat resource use.

A 7-command shortcut also exists (`use Crystal of Dawn` → `attack`), and the
judge explicitly declined to score it as a false victory: the Crystal is the
brief's designated weakness item, hidden in the shrine and hinted by both NPCs,
so the key item genuinely gates that path. The defect is that `use` sets boss HP
to 0 outright instead of driving a phase — billed SILENT-WRONG (R2), not as an
unearned win. That distinction is the one gpt-oss's PRIOR round failed: on
2026-07-29 its decisive defect was a victory that fired six commands from a cold
start because `run_combat` returned one boolean for both "killed it" and "fled".

### Decisive defect — half the game is behind a one-way edge

```yaml
hidden_chamber:  east: garden      # a link OUT of an unreachable room
garden:          north: library    # ...and nothing links back in
```

`skeleton_watcher` lives in the orphaned room. `goblin_guard` is authored as a
20 HP monster and assigned to no room's `monster:` field. `gold_coin` is in no
room at all. **Authored: 8 rooms, 3 regular monsters, 5 items. Delivered: 7
rooms, ONE regular monster, 4 items.** Conformance is untouched (presence rule);
delivery is halved.

### Why one judge, not three

60 is the top of the 3★ band, which normally triggers §5 rejudge-to-place. The
operator CORROBORATED instead, on deep prior experience with this model
(hundreds of millions of tokens through it while building the framework):
"solidly tier 1, borderline 4 star." Recorded as a corroboration, NOT as a
three-vote median, so a later reader can see which it was.

The judge itself flagged the margin unprompted and named the exact alternative:
reading R3 (equipment bonus persisting after `drop`, and surviving save/load) as
INVALIDATING rather than SILENT-WRONG gives §3.1 = 8 and a total of **59**. It
examined that reading and rejected it — the save is valid, loadable and
recoverable, and no terminal state is destroyed.

### ⚠️ Star label corrected

The judge reported **★★★★**. It read the rubric's band table, which was wrong at
every boundary (60–79 for 4★). The correct bands are five equal 20-point spans —
41–60 is 3★ — so 60 is the TOP of 3★, not the bottom of 4★. Table fixed in
TIER_RUBRIC §2 the same day. **The score and the tier are unaffected**; only the
star label was, and arms 01–02 were already correct at 46 and 48.

### Notes

- Third instance of the README-overclaiming pattern: truncates mid-code-block
  after `pip install .`, never says to run `python main.py`, claims "powered by
  **rich**" (zero references in any `.py`), claims "a multi-phase boss" (absent
  from the code), and declares an entry point `main:run` naming a function
  `main.py` does not define.
- Sampling: **pre-fix**, and the most affected arm of the three — `top_k` served
  **40** against a declared `0` (no limit), plus `min_p` 0.05 vs 0.0. See the
  regime header at the top of this file.

---

## ⚠️ OPERATOR REGISTRATION — the scores may be too tight (2026-07-31, before arm 11 judged)

Recorded BEFORE devstral (arm 11) was scored, so the answer cannot be
rationalised afterwards.

**The suspicion.** Six scored artifacts sit in a 14-point band:

| model | median | params |
|---|---|---|
| gpt-oss-120b-a5 | 60 | 120B |
| gpt-oss-swarm | 53 | 120B |
| hy3-reap-200b | 49 | 200B |
| mistral-medium | 49 | 128B |
| laguna-xs-2.1 | 48 | ~30B class |
| laguna-s-2.1-apex | 46 | ~110B class |

That is a very narrow spread for models separated by an order of magnitude in
size, and the operator's stated position is that **gpt-oss leading is itself
surprising** — a strong, fast, reliable model, but "never a frontrunner in my
mind." A leaderboard whose top entry is a surprise and whose spread is 14
points invites the question of whether the instrument is discriminating at all,
or whether every artifact is being scored on the same handful of universal
failures (seam bugs, unearned wins, inverted weakness items) that swamp real
differences between models.

**The test, and why devstral is the right vehicle.** devstral-2-small-24b is
the smallest model in the fleet by a wide margin and has underperformed
consistently across prior rounds. Its pre-registered expectations (see
llmvp/configs/devstral-2-small-24b.yaml) already predict a low score.

    IF devstral lands MIDDLING — anywhere near the 46-53 cluster —
    THAT IS THE TELL that something is not discriminating properly.

A 24B model scoring level with a 200B one is not a plausible capability
result; it is evidence that the rubric, the mission, or the flow is measuring
something other than model quality. The most likely candidate is that the
brief's universal failure modes dominate every artifact's §3.1, compressing
the range regardless of who wrote it.

    IF devstral lands CLEARLY LOW — below the cluster —
    the instrument is separating models and the tight band is a real result:
    these models genuinely are close on this task.

**Do not treat this as settled either way until arm 11's median is in.** It is
a one-arm test of a campaign-level doubt, so a middling devstral is a REASON TO
INVESTIGATE, not proof of a broken instrument; and a low devstral removes the
most obvious alternative explanation without proving the band is meaningful.

### PARTIAL ANSWER — the decomposition, not the designated vehicle (2026-08-01)

**The designated vehicle did not report.** Arm 11 (devstral) gate-failed on a
leaked decision envelope with ~3 minutes of clock left and was scored VOID, so
the one-arm test above is still owed a re-run. What follows is an INDEPENDENT
line of evidence on the same doubt, computed from the per-dimension vectors this
campaign started recording — not a substitute for the devstral re-run.

**Method.** Every recorded dimension vector from this sweep (n=7 scored arms),
per dimension: the range across arms as a fraction of that dimension's max.
Reproduce with `dev/blind_panel/dim_variance.py`.

**Finding 1 — 30 of the 100 points are near-constant.** Three dimensions do not
move across seven models spanning 24B to 200B:

| dimension | max | values across all 7 arms | spread |
|---|---|---|---|
| conformance | 10 | 9,9,9,9,9,9,10 | 1 |
| creativity | 10 | 5,5,5,5,5,5,6 | 1 |
| documentation | 10 | 2,2,2,3,3,3,3 | 1 |

Two of the three are pinned **by rubric construction, not by coincidence**:

- §3.10 sets a **hard cap of 3** when any documentation claim is contradicted by
  play. Every artifact overclaimed; the cap fired on all seven. The dimension is
  currently a 10-point yes/no question that everyone answers the same way.
- §3.4 scores `round(10 × met/total)` over the 53-item checklist. The observed
  population sits at 46–51 met, i.e. 87–96%, so a 10-point scale is being used
  over 2 of its points. Note this is DELIBERATE — §3.4 explicitly measures
  presence, not reachability, and the rubric names laguna-S as the case it wants
  to score high here and destroy on §3.1. The decomposition is informative; it
  is the SUM that the near-constant absorbs.

**Finding 2 — every artifact banks a 16–18 point pedestal.** Those three
dimensions contribute a mean of 16.9 points regardless of quality. That is the
floor nothing has fallen below, and it is the direct mechanical reason no scored
artifact is under 46. Stripping it, the live range is 29–42 out of 70.

**Finding 3 — the RANKING is not an artifact of the pedestal.** Re-ranking on
the 70 live points only: of the 18 ordered pairs with distinct totals, **18
agree and 0 invert.** gpt-oss leads because it leads where the instrument
actually discriminates — `no_broken_functions` 9 against a next-best 7, and 60%
of live points against 53% for the runner-up.

**Finding 4 — the pedestal manufactured the three-way tie at 49.** hy3 reached
49 with 18 pedestal + 31 live; mistral and qwen3-next reached the same 49 with
16 pedestal + 33 live. Identical totals, different artifacts. The live score
separates them and the total cannot.

**What this settles, and what it does not.** It settles that *the tightness is
partly an instrument property* — a large constant is added to every score, and
the operator's read that the band looks too narrow is CORRECT about the band.
It does NOT settle the capability question: zero inversions means the ordering
is trustworthy, but nothing here shows the models are genuinely far apart, and
the doubt's sharpest form — *would a much weaker model land in this cluster?* —
still needs a low-capability arm to land ON MERIT. **devstral's re-run remains
owed and remains the test.**

**Actionable, if the band is to be widened:** §3.10's hard cap should either
have a lower floor than 3 or scale with how badly the docs overclaim, and §3.4
should be scored over the range models actually occupy rather than 0–100%.
Neither changes any recorded total or tier; both change what future totals can
express. Not landed — recorded for the operator's decision.

---

## Cross-arm pattern (running)

Both arms scored in the ★★★ band with conformance 9/10 and delivery in the
bottom quartile, and in both the decisive defect is a **seam** — two or three
lines at a module boundary — not a capability limit:

| arm | authored | but wired to |
|---|---|---|
| 01 | `_handle_equip`, `DialogueEngine`, phase system, victory banner | a verb the parser rewrites, a schema the data does not use, a behaviour string nothing sets, a flag nothing assigns |
| 02 | 8 rooms, boss, two phases, weakness item, `MonsterAI`, victory + defeat screens | a `connections:` block no loader reads, and a dispatch tower that returns before the monster's turn |

This is the third consecutive round in which every decisive defect was a seam
bug. If it holds across families, the lever is contract enforcement between
modules in `code_core` — nothing checks that a verb the parser emits is a verb
the engine dispatches, or that a key the writer uses is the key the reader
looks for — rather than model selection.

### The seam thesis, quantified across seven models (2026-08-01)

Field utilization — the mean fraction of each dimension EARNED across all seven
scored arms, 24B to 200B. This is a different question from spread: a dimension
can be useless for ranking (everyone scores alike) while carrying the campaign's
main finding (everyone scores alike and LOW). `dev/blind_panel/dim_variance.py`.

| dimension | earned | |
|---|---|---|
| conformance | 91.4% | authored what was asked |
| reusability | 77.1% | |
| organization: project | 68.6% | and structured it reasonably |
| UI/UX/utility | 55.7% | |
| organization: logic | 54.3% | |
| creativity | 51.4% | |
| robustness | 50.0% | |
| ambition/completeness | 35.7% | |
| **no broken functions** | **28.6%** | **but it does not work** |
| documentation | 25.7% | (hard-capped, §3.10) |

**A 63-point gap between the top row and `no_broken_functions`.** Every model in
the field writes the thing the brief asked for, lays it out in sensible modules,
and ships something that does not run correctly. The dimension carrying the
single largest point allocation — 20 of 100 — is the one the entire field fails,
earning under a third of it, with the best arm in the sweep at 9/20.

This is the seam thesis stated as a measurement rather than an anecdote, and it
holds across five model families and an 8× parameter range. It also says the
lever is NOT model selection: no model available to this fleet is going to close
a 63-point authoring-vs-working gap by being smarter, because the gap is not
where any of them are weak individually — it is where all of them are weak
together. Contract enforcement between modules is the intervention this predicts,
and the tier campaign now has a baseline to measure that intervention against.

