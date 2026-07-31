# TIER_RUBRIC v1.2 — solo artifact scoring for model tiering

**Status: IN USE.** Every record carries its version string. Any change to a
dimension, a band, a probe battery, or the judge prompt bumps the version.

## Changelog

**v1.2 (2026-07-29)** — two amendments, both from the first three-vote rejudge.

1. **§3.1 composes multiplicatively; the floor is gone.** Each severity is now
   its v1.1 cost as a share of the dimension (`1 − cost/20`) and the row
   fractions multiply, so **one defect costs exactly what it did in v1.1** and
   the schemes diverge only as defects accumulate. Fixed additive costs punished
   the NUMBER of root causes and then ran out: two of three judges floored the
   same *winnable* artifact at 0/20 (sums of −20 and −24) while the arm it was
   compared against — which printed a **false `=== VICTORY ===` six commands from
   a cold start** — scored 7/20, because it had four causes rather than nine. The
   instrument was ranking count over severity and the floor was hiding it.
   **0/20 now means nothing the judge invoked worked.**
2. **§4.1 — a pass-2 recharacterisation is DISCLOSED, not revised.** The lock
   rightly stops pass 2 raising a score; it cannot stop pass 2 revealing that an
   already-observed defect is worse than it looked. Keep the locked number, record
   the disclosure beside it, tell rejudges what to expect. A judge did exactly
   this unprompted and its record was the most useful of the three.

**THIS IS THE FIRST AMENDMENT THAT MOVES SCORES**, unlike v1.1's. Recomputed on
the three real ledgers: §3.1 `0/0/6 → 7/5/9`, totals `51/48/59 → 58/53/62`, median
**51 → 58**. Star band ★★★ unchanged and tier 1 unchanged (in fact unanimous
rather than 2-of-3), so no conclusion moves — but the numbers do.

**Comparability with the v1.0 and v1.1 scores.** Both prior records **stand under
the version that produced them** and are not recomputed (operator decision,
2026-07-29): `Arm D-swarm` 54/100 ★★★ tier 1 under v1.0, and the 2026-07-29
three-vote arm 51/100 ★★★ tier 1 under v1.1. Both are ★★★ tier 1 under either
scheme, so the campaign's conclusions are unaffected. A v1.2 re-score is not owed
unless a star is disputed.

*Why they could not simply be recomputed, even had we wanted to:* §7 asked for the
`entry_point_ledger` in the record, but the stored records kept only dimension
TOTALS, not the rows. A §3.1 amendment is therefore unappliable backwards. §7 now
requires the rows to be stored so the next amendment is not stuck the same way.

**v1.1 (2026-07-29)** — two amendments, both from the first live judgement.

1. **§3.1 groups by cause and prices by effect.** *Root causes control frequency;
   symptoms control severity.* One defect is one row however many entry points it
   breaks, and a new **INVALIDATING (−6)** severity covers symptoms that destroy a
   terminal state or the data behind it. v1.0 scored per-symptom and billed one
   conflated boolean twice (−8); a naive per-cause fix with a −4 ceiling would
   have made that bug cheaper than two typos.
2. **§3.2 bands by IMPACT, not count.** v1.0's 1–2 band said "multiple fatal
   edges" while its descriptor said "hostile to anything off the happy path" —
   the first judge hit an artifact that was both (two fatal edges, eight clean
   probes) and had to reason around the contradiction. Bands now key on the worst
   observed impact: HOST-DAMAGING / BOOT-BLOCKING / SESSION-ENDING / SURVIVABLE.

Comparability for v1.1 was accepted by operator decision; neither of its
amendments was retroactive and neither would have moved a band. See the v1.2 entry
above for the standing position on all prior scores.

**v1.0 (2026-07-29)** — initial.

---

## 1. What this is, and what it deliberately is not

**A cheap, predictable relative gauge — not a capability measurement.** It does
not force completion. A 2h run is a slice, and every artifact judged under this
rubric will be unfinished; that is expected and is not a defect to score against.

Because the instrument is coarse by design, **the record is stars, not points.**
The 100-point rubric exists to make the judge work through specific criteria
rather than form a gestalt. The number is scaffolding. What is retained is a star
and a paragraph.

A tier is a property of **(model + our integration of it)**, not of the model.
gpt-oss has had a year of tuning here; glm-4.7-flash has had a day. Tier 2 means
"we cannot currently get better than this out of it" — operationally useful, and
not the same claim as "the model is weak."

**This is a different instrument from `METHODS.md`.** That one ranks a flight of
artifacts against each other and is reliable at exactly that. This one judges
**one artifact, alone, against the rubric**, which is what makes it pipelineable:
arm N is judged while arm N+1 runs, across sessions, so judging never blocks the
queue.

**The cost of that: absolute cross-session consistency is not held.** No anchors,
no calibration flight. The stars are coarse enough (20 points each) to absorb the
drift that buys, and §6 describes the one cheap thing that partly replaces them.

---

## 2. Tiers and stars

| stars | score | tier |
|---|---|---|
| ★★★★★ | 81–100 | 1 |
| ★★★★ | 61–80 | 1 |
| ★★★ | 41–60 | tier by score (≥50 → 1); **rejudge to place** (§5) |
| ★★ | 21–40 | 2 |
| ★ | 0–20 | 2 |
| — | no runnable artifact | 3 |
| — | will not boot | UNSUPPORTED (not a tier) |

**CORRECTED 2026-07-31.** The table above previously read 80–100 / 60–79 /
40–59 / 20–39 / 1–19 — every boundary one off, and the bands unequal at the
ends. The stars are **five equal 20-point bands**: 0–20, 21–40, 41–60, 61–80,
81–100 (operator, 2026-07-31). The error was live: a judge scoring a 60 read
the old table and reported **4★** when 60 is the TOP of the 3★ band —
tier-correct, star-wrong. Scores are unaffected; only the label was.

**Tier 1 is ≥ 50.** The original spec said "higher than 50" and "less than 50",
leaving 50 undefined; integer dimensions will produce it regularly, and "at least
half the available credit" is the natural reading of the bar.

**The rejudge band and the 3★ band are the same band.** Two rules collapsed into
one: 41–60 is both "the star that straddles the tier boundary" and "the score
close enough to 50 that a single judgment cannot place it." 4★ and 5★ are tier 1
on sight; 1★ and 2★ are tier 2 on sight; **only 3★ costs two more judgments.**

**A 3★ score still HAS a tier** — ≥50 is tier 1, below is tier 2. The rejudge
does not decide whether a tier exists, it decides whether a single judgment can
be trusted to place one. An operator with deep prior knowledge of the model may
CORROBORATE instead of spending two judges; record that as a corroboration, not
as a three-vote result, so a later reader can see which it was.
This is why solo judging stays cheap — the expensive path is reserved for the
only case that needs it.

**Tier 3** — no runnable artifact: no entry point exists, or the entry point does
not start and accept one command. **Single run is sufficient**, recorded as
single-run. Re-run only when the failure looks like a framework or config fault
rather than the model. The tier-3 gate is the judge's **first** action and it
**terminates the judgment** — do not score the remaining 90 points of a program
that will not start.

**UNSUPPORTED** is not a tier. Model cannot boot under current llmvp (laguna-S
needing b10087 on a b9860 lib). Recorded with the blocking reason.

---

## 3. The rubric — 100 points

Each dimension answers a **different question.**

### PASS 1 — PLAY ONLY · 40 points · locked

#### 3.1 no broken functions — 0–20

> *Of everything the artifact offers, how much actually works when invoked?*

The heaviest dimension and the one a judge is worst at eyeballing, so it is
**scored from a ledger, not an impression.** The judge enumerates every command,
feature and entry point it invoked, marks each, then groups them.

**ROOT CAUSES CONTROL FREQUENCY. SYMPTOMS CONTROL SEVERITY.**

One defect = **one ledger row**, however many entry points it breaks. What that
row *costs* is set by its worst symptom, not by how many surfaces the symptom
reached. Rows are therefore grouped by cause and priced by effect:

| severity | meaning | keeps |
|---|---|---|
| INVALIDATING | destroys a terminal state or the data behind it — a false win or loss, a corrupted or unrecoverable save, world state silently rewritten | **×0.70** |
| SILENT-WRONG | ran, no error, wrong or no effect | ×0.80 |
| CRASH | traceback or hang on an advertised operation | ×0.85 |
| UNREACHABLE | exists in code or help, no path to it in play | ×0.90 |
| DEGRADED | works but obviously partial | ×0.95 |

**Score = 20 × the product of every row's fraction, rounded.** The arithmetic must
be shown, and each row must name its root cause and list the symptoms it accounts
for.

**Each severity is its v1.1 point cost expressed as a share of the dimension**
(`1 − cost/20`), so **one defect costs exactly what it always cost** — a lone
INVALIDATING row still lands on 14/20. The two schemes only diverge once defects
accumulate, which is the regime where the old one broke.

Why it changed (v1.2). Fixed costs are additive against a bounded dimension, so
they punish the NUMBER of distinct root causes and then run out. On 2026-07-29 two
of three judges hit the floor on the same artifact — deductions summing to −20 and
−24 — and both flagged it unprompted: *"read this 0 as 'nine independent root
causes exhausted the budget', not 'nothing works'"* and *"a floor artefact… the
floor cannot distinguish it from a dead program."* That artifact was **winnable
start to finish by all three judges**. Worse, the arm it was being compared
against had a **false victory** (six commands from a cold start printing
`=== VICTORY ===` — the canonical INVALIDATING case) and scored 7/20 unfloored,
because it had four root causes rather than nine. The instrument was ranking
count over severity and the floor was erasing the evidence.

A product cannot reach zero from positive fractions, so the floor is structurally
gone rather than merely lowered, and the marginal penalty falls as the artifact
worsens — the ninth defect on a broken artifact matters less than the first on a
clean one, which is the correct shape. **0/20 now means what it says: nothing the
judge invoked worked.**

Why the split. Scored per-symptom, the 2026-07-29 arm lost 8 points to a single
conflated boolean — the same bug billed twice because it surfaced twice. Scored
per-cause with a −4 ceiling it would have lost 4, i.e. *less than two unrelated
typos*, which is worse. INVALIDATING exists so that one defect ending the game
with a false `=== VICTORY ===` outranks any number of cosmetic ones, while still
being counted once.

A judge who is unsure whether two symptoms share a cause **records them as one
row and says so**; the alternative silently doubles a penalty on a guess.

SILENT-WRONG costs more than CRASH on purpose: a traceback is honest and
findable, silent wrongness is what ships. The 2026-07-27 panel's decisive gpt-oss
defect was exactly this — the two-phase boss died and play simply continued,
`Threats: none`, with no win condition anywhere in the tree while the defeat path
was fully written.

**This is where unreachability is punished.** Conformance (§3.4) counts presence;
this dimension counts delivery. A seam bug that orphans every NPC and monster
leaves conformance intact and destroys this.

#### 3.2 robustness — 0–10

> *What happens at the edges?*

**Fixed battery. All of it, every arm, reported as a table.** Unknown command ·
bare Enter · EOF (Ctrl-D) · verb with no object (`take`, `attack`) · nonexistent
object · non-exit direction · load with the save absent · load with the save
corrupted · 500-character input · input with quotes and non-ASCII.

**Banded by IMPACT, not by count.** Counting fatal edges puts a program that
dies on Ctrl-D in the same band as one that fills a disk, and it punishes an
artifact whose battery was thorough enough to find two. Ask what the worst edge
actually *costs the person hitting it*:

| impact | what it costs |
|---|---|
| HOST-DAMAGING | harms the machine or the session around it — unbounded output, a hang needing a kill, a file written outside the workspace |
| BOOT-BLOCKING | the artifact will not start until a human removes or repairs something |
| SESSION-ENDING | the run dies and progress is lost; restarting works |
| SURVIVABLE | ugly — a traceback that does not kill it, or a silent no-op |
| CLEAN | an informative refusal, state still valid |

| band | anchor |
|---|---|
| 9–10 | every probe CLEAN and informative; load survives absent *and* malformed files |
| 7–8 | all probes handled; refusals terse or unhelpful, or one SURVIVABLE edge |
| 5–6 | worst edge is SESSION-ENDING — usable, as long as you avoid it |
| 3–4 | worst edge is BOOT-BLOCKING |
| 1–2 | any HOST-DAMAGING edge, or the happy path itself is fragile |
| 0 | multiple HOST-DAMAGING edges, or unusable in practice |

Score by the **worst** impact observed, then place within the band by how the
other nine probes went. A prior arm emitted **10,921,238 lines in 15 seconds** on
EOF — that is HOST-DAMAGING (it fills a disk when redirected) and lands at 1–2
however clean the rest of its battery is.

#### 3.3 UI | UX | utility — 0–10

> *Is it legible and pleasant to USE?*

For a CLI this is the feedback loop, and it is real surface: does the opening
tell you what to do; is help discoverable and accurate; does each command confirm
what changed; is state (health, inventory, exits) visible without asking; is
input forgiving of case, articles, synonyms, abbreviations; is output formatted
to scan.

9–10 never had to guess what to type; state legible; formatting aids play · 7–8
mostly self-explanatory, one or two consults · 5–6 playable once you learn its
dialect, help accurate · 3–4 had to read source to find the vocabulary · 1–2 wall
of undifferentiated text, or near-silent · 0 unusable.

---

### PASS 2 — READ · 60 points

#### 3.4 conformance — 0–10

> *Did it follow the brief, including the specifics sprinkled through it?*

**The model receives a paragraph to interpret. The judge receives a list to
check.** That asymmetry is the measurement: conformance is instruction-following
under interpretation, and the checklist exists so the judge scores the same items
on every arm rather than remembering different ones.

Score = `round(10 × met / total)` over the challenge's requirement checklist
(§8).

**Presence is the test, not reachability.** If the brief asks for three NPCs and
three NPCs exist in the code or data, the model conformed — *even if none of them
can be reached in play.* Unreachability de-ranks §3.1 and §3.6; it does not
de-rank this. Conformance measures whether the model did what was asked; the
other dimensions measure whether it delivered. Keeping them separate is what
makes the decomposition informative: laguna-S would score **high** here — boss,
weakness, two NPCs, monsters, all present and correctly authored in YAML — and be
destroyed on §3.1, which is precisely the true finding that a single number hides.

Count-based requirements ("at least 8 rooms", "3 regular monsters") are checked
by **counting**. Named features ("a title screen") are checked by **finding
them**. This is the boring conformance a judge skips, and a checklist plus a
formula forces it.

#### 3.5 ambition | completeness — 0–10

> *Did it aim past the floor, and did what it aimed at land?*

Scored in pass 2 because scope is only visible with the tree open — but the
"landed" half is read from pass 1's **locked** ledger, not re-litigated.

| band | anchor |
|---|---|
| 9–10 | substantive beyond-brief work, **and it works** |
| 7–8 | brief-complete plus a modest working extra |
| 5–6 | brief-complete, nothing beyond |
| 3–4 | full scope attempted, significant parts unfinished — stubs, TODOs, dead branches |
| 1–2 | a fraction of the scope attempted |
| 0 | scaffolding only |

**Ambition that does not land does not score.** A grand half-built system ranks
below a modest complete one. This is the anti-bias rule — impressive scope is
what an LLM judge over-rewards, and the corpus is full of artifacts that read
ambitious and were unplayable. Note this is *not* in tension with §3.4's presence
rule: authored-but-unreachable content is conformance (it was asked for and it is
there) and is not completeness (it does not work).

#### 3.6 creativity — 0–10

> *Did it make a non-obvious choice that PAID OFF?*

9–10 a design or content choice not seen before that measurably improves the
artifact, and works · 7–8 one distinctive working idea beyond the template · 5–6
competent generic execution of the obvious design · 3–4 template plus filler
("Room 1", "Item 2") · 1–2 derivative to the point of placeholder · 0 nothing to
assess.

**Creditable only where the idea is real** — shipped in code or data. An
imaginative idea living in a comment or a TODO scores 0 here; it belongs to §3.5
as ambition-that-did-not-land.

#### 3.7 organization: project — 0–5

> *Can you find where a thing lives without searching?*

5 clear module boundaries, data separate from code, entry point obvious, no file
doing two unrelated jobs · 4 sensible, one questionable placement · 3 flat or
lumpy — one oversized module, or data mixed into code · 2 names do not match
contents, or one god-file holds most of it · 1 single-file dump, or a tree with
no discernible principle · 0 scaffolding only.

#### 3.8 organization: logic — 0–5

> *Within a module, how is control flow arranged?*

5 functions do one thing, state changes localized, no copy-paste, module seams
are explicit contracts · 4 mostly clean, one long function or one leaky boundary
· 3 works, but a 100+ line branch tower or logic duplicated in 2–3 places · 2
deep nesting, cross-module reaching into internals, state mutated from many
places · 1 control flow not followable without a debugger · 0 N/A.

#### 3.9 reusability | modification ease — 0–10

> *Could a competent stranger change this without rewriting it?*

**Scored from the MODIFICATION PROBE, run identically on every arm:**

- **(a)** add a new room and a new item, connected and reachable, **editing data
  files only**
- **(b)** add one new command verb to the parser
- **(c)** change a balance constant (boss HP) and observe it take effect

Report per task: files touched, lines touched, worked first try y/n.

| band | anchor |
|---|---|
| 9–10 | (a) data-only, (b) one registration point, (c) one constant — all first try |
| 7–8 | all three land; one needs a code change that should have been data |
| 5–6 | all three land, each needing edits in 2–3 places |
| 3–4 | one requires restructuring, or a change in one place silently breaks another |
| 1–2 | any change requires understanding the whole tree; content hardcoded throughout |
| 0 | not modifiable, or does not run |

#### 3.10 intent | documentation | comments — 0–10

> *Can a reader learn WHY, not just what?*

9–10 comments explain decisions and tradeoffs; README accurate, says how to run
and what is incomplete; names carry intent · 7–8 good README, comments where
non-obvious · 5–6 comments restate the code (`# increment i`); README is a
feature list · 3–4 sparse or absent; you must read code to start it · 1–2
misleading · 0 none.

**Hard cap: any documentation claim contradicted by play caps this dimension at
3.** Earned rule — in the 2026-07-27 panel three of four READMEs asserted working
boss fights and one held up.

---

## 4. The pass split, and why pass 1 is locked

**PASS 1 plays. PASS 2 reads. Pass-1 scores may not be revised after pass 2.**

Pass 1 is the 40 points that answer *does it work* — and it holds the heaviest
dimension, so the lock protects the thing most worth protecting. Source may be
opened during pass 1 only to *explain a failure already observed*, never to
discover a feature.

If pass 2 reveals a working feature never found in play, that is **not** a §3.1
credit. It is a **§3.3 UI/UX finding**, because undiscoverable is a usability
defect. The contamination this prevents is measured, most sharply on laguna-S:
*"reads like the winner — the best prose, the best help text, the best refusals…
In play it is a walking simulator: not one monster, not one NPC, no combat, no
boss, no win, no death."* Five missing lines in `loader.py`.

Pass 2's 60 points answer *what is it* — and under §3.4's presence rule they are
substantially readable, which is what makes the split coherent rather than a
compromise.

### 4.1 A pass-2 recharacterisation is DISCLOSED, not revised

The lock stops pass 2 from *raising* a pass-1 score. It cannot stop pass 2 from
revealing that a defect already observed in play is **worse than it looked**, and
that is a different thing: the finding was in scope, only its severity was
misread. Silently keeping the generous number makes the ledger wrong; silently
correcting it breaks the lock.

**So: keep the locked score, and record a disclosure beside it** naming the row,
the severity as billed, the severity pass 2 supports, and the score the dimension
would carry. Rejudges are told to expect that number.

    §3.1 = 6 (locked)
    DISCLOSURE — R2 billed SILENT-WRONG (−4) in pass 1 as item destruction.
    Pass 2 shows DUPLICATION, i.e. "world state silently rewritten" =
    INVALIDATING (−6). Not revised, per §4. This dimension would be 4;
    a rejudge should expect 4–6.

Written because a judge did exactly this unprompted on 2026-07-29 and its record
was the most useful of the three: it disclosed that its locked 6 was "the most
generous defensible number", named the two further rows it had left unbilled to
respect the lock, and told the other votes where to look. The rubric asking for
what a good judge already volunteered costs nothing and makes the honest move the
expected one rather than a discretionary one.

A disclosure is **not** a licence to bill the row twice. The locked number is the
score; the disclosure is provenance.

---

## 5. Judging

- **One artifact at a time, alone, against this rubric.** No flight, no
  comparison, no anchors. Judge arm N while arm N+1 runs.
- **One judgment = one fresh instance.** Never sees run logs, goal counters,
  `OUTCOME` sidecars, the config, the model name, the pre-registered expectation,
  the operator's run notes, or another judgment.
- **Judge model pinned and recorded** as an exact model string in every record.
- **THE OPERATOR OF A BATCH CANNOT JUDGE IT.** Whoever watches arms run knows
  the model name, the config, the degeneration events and the goal counters —
  every single thing §9.5 forbids a judge from seeing. Pipelined judging means a
  judge is *spawned fresh against a staged directory path and nothing else*; it
  does not mean the operator scores an artifact between arms. Learned on
  2026-07-29, when the operator offered to "validate the rubric" by judging the
  batch's first artifact after having watched that arm for two hours.
- **3★ only (40–59) → two additional independent judgments**, fresh instances,
  not further opinions in one conversation. Tier = the side taken by ≥2 of 3.
  Recorded score = **median**. The first judgment is a full vote; it knows
  nothing the others do not.
- **Staging is per-arm and immediate.** `stage.py` runs the moment an arm
  finishes, before the next arm touches `/tmp`. Its strip and identifier scan are
  the whole of the blinding when there is only one label to assign — and pass 2
  reads source, so a leaked model name in a comment is now *certain* to be seen
  rather than merely likely.
- **THE MECHANISM: a judge is a freshly spawned SUBAGENT given a packet path and
  nothing else.** This is what makes the rule above enforceable instead of
  aspirational — an operator cannot un-know the model, the config, the
  degeneration events and the goal counters by resolving to be impartial.
  Per judge: build a packet with `make_judge_packet.py`, then spawn one
  subagent whose prompt carries the packet path, this protocol, and an explicit
  wall — read ONLY inside the packet; never `~/ouroboros-runs/`, the repo, any
  config, log or trace; no git. Each judge gets its own packet and its own
  scratch directory, so save files and mutated world state cannot collide. The
  subagent's final message IS the record; it never sees another judgment, the
  operator's notes, `observed`, or the pre-registered expectation.
  Added 2026-07-31, the day the operator started playing an artifact personally
  and had to be stopped — the same failure this section already records from
  2026-07-29, recurring because the rule named a prohibition and not a method.
- **A redaction must not read as a defect.** Whatever the packet rewrites to
  protect blinding has to look like ordinary content. The workdir redaction
  first wrote `<arm>`; all three judges of arm02 remarked on the "unsubstituted
  placeholder" and one scored it against organization. Disclose contamination
  in the record rather than silently correcting it.
- **THE RESULT IS RECORDED INTO THE MODEL'S CONFIG `tier:` BLOCK.** A judgment
  that lives only in a transcript is lost: the 2026-07-29 non-thinking
  laguna-xs run was never written down and its per-dimension scores no longer
  exist, so the A/B it was run to support cannot be computed. Record the rubric
  version, every judge's total, the median, the per-dimension score/max/stars,
  the decisive defect, the unmet requirements, and the expectation's outcome.
  **One config = one score** — a variant that changes a scoreable property gets
  its own config via `extends:`, so the experimental delta is the config diff.

---

## 6. What replaces the anchors

Dropping anchors costs absolute cross-session consistency. Two things carry what
is left, and neither costs a judging slot:

**1. Coarse output.** 20-point stars absorb roughly ±5–8 points of single-judge
noise (`JUDGE_STANDARD v1.0` measured ranking noise at ±1–2 positions;
`METHODS.md` records an arm placing 2nd then a distant 3rd on the *same*
artifact). Precision the instrument cannot support is not recorded.

**2. Pre-registered expectation.** Before the batch, write the expected tier and
star for each arm, with reasons. *"gpt-oss: moderate tier 1 — flexible on new
features, reliable under framework faults, fast." "devstral-2: tier 3, maybe 2 —
huge context, self-promotes through gates."*

This is the cheap partial substitute for a calibration anchor. It cannot detect
drift the way a locked reference score can, but it flags the **surprises**: if
gpt-oss comes back 2★, either the run broke or the judge is severe, and you know
to look before the number enters the ledger. Written before, never shown to a
judge, never revised after.

---

## 7. The record — two halves, and the judge sees only one

**Judged half** (blind, artifact only): stars, per-dimension scores, the three
ledgers, the judge's comments.

**Observed half** (unblinded, written by the operator from run telemetry): the
qualitative properties that no stripped artifact can carry — speed, behaviour
under framework faults, context capacity, gate self-promotion, degeneration,
throughput, whether it hit the backstop. This is where "flexible in adapting to
new features" and "self-promotion through gates" live.

**A judge must never see the observed half.** It unblinds instantly.

```json
{
  "arm": "<config name>",
  "run_id": "…",
  // Copy this VERBATIM from the first line of the RUBRIC.md you were given —
  // never from this example, which is how a record ends up stamped with a
  // version that did not score it.
  "rubric": "<the version string at the top of RUBRIC.md>",
  "judge_model": "<exact model string>",
  "expectation": {"tier": 1, "stars": 3, "why": "…"},

  "tier3_gate": "passed|failed",
  "scores": {
    "no_broken_functions": 0, "robustness": 0, "ux": 0,
    "conformance": 0, "ambition": 0, "creativity": 0,
    "org_project": 0, "org_logic": 0,
    "reusability": 0, "documentation": 0
  },
  "total": 0,
  "stars": 0,
  "tier": "1|2|3|UNSUPPORTED",
  "raw_votes": [],                   // 3★ only: all three totals, in order

  // §3.1 — STORE THE ROWS, not just the dimension total. A stored total is
  // unamendable: when v1.2 changed how §3.1 composes, neither prior record could
  // be recomputed because only the totals had been kept, and the operator had to
  // choose between re-judging and accepting a version boundary. One row per root
  // cause, each with its severity, so any future amendment can be replayed.
  "entry_point_ledger": [
    // {"cause": "…", "symptoms": ["…"], "severity": "INVALIDATING", "keeps": 0.70}
  ],
  "entry_points_invoked": [],        // §3.1, what was tried and what worked
  "pass1_disclosures": [],           // §4.1: {"row","billed","pass2_supports","would_be"}
  "robustness_battery": [],          // §3.2, all ten probes
  "requirement_tally": {"met": 0, "total": 0},
  "unmet_requirements": [],          // §3.4, by number
  "modification_probe": [],          // §3.9, three tasks
  "furthest_point_reached": "…",
  "judge_comments": "…",

  "run_notes": "…",                  // OBSERVED half — never shown to a judge
  "config_resolution": "…"           // describe_resolution(): base + diff
}
```

---

## 8. Dependencies before this can run

1. **The rewritten 2h `game_challenge`**, in two artifacts: a **paragraph** for
   the model and an **enumerated checklist** for the judge. §3.4 is a formula
   over the checklist and has no denominator without it.
2. **Per-arm staging in the run script**, so judging can start at arm completion
   rather than after the batch.
3. **Pre-registered expectations** for every arm (§6).

---

## 9. Prohibitions

1. **No pass-1 revision after pass 2** (§4). The contamination is measured.
2. **No scoring past a failed tier-3 gate.**
3. **No de-ranking conformance for unreachability** (§3.4). That is §3.1's job
   and double-punishing destroys the decomposition.
4. **No mixing judge models, or rubric versions, inside a batch.**
5. **Judges never see** the observed half, the expectation, goal counters,
   `OUTCOME`, run logs, config, model names, or each other. Goal counters have
   inverted against play in four consecutive rounds.
6. **No comparing these totals to the old /50 panel scores.** Different
   instrument, absolute vs comparative, different dimensions. Not a scale factor.
