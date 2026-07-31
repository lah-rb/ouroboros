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

