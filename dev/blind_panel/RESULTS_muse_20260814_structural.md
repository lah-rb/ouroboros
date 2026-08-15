# muse-glimmer-30b — first agentic measurement, structural only

*Run `tier_20260814-175357`, one arm, `game_challenge_tier` v2 brief,
`--top-phase structural`. Three blind judges, independent, TIER_RUBRIC v2.1.
**No tier is recorded** — §8 defines both live tiers strictly as the outcome of
a Guardian flight, and this was a solo packet.*

## Read the ceiling before anything else

This arm stopped at **structural**. The default is `quality`, and the phases we
skipped are the ones that exercise what was built. Every headline defect below
is of the form *authored and never connected* — which is precisely the class the
later phases exist to catch. **This is a measurement of muse's first pass, not
of what it would ship.** Comparing it against the archived arms without
controlling for the ceiling would be comparing a draft to a finished run.

The run itself was clean: 13 min, 11 files, 7 Python, 0 syntax failures, 0
degenerations, mission completed at the ceiling. The batch turn HELD — *"Batch
structural creation: 8 files written, 8 goals completed, 0 failed gates"* — no
serial fallback, which is the signal that separated the two worst of thirteen
arms in a previous campaign.

## What all three judges found, independently

| finding | J1 | J2 | J3 |
|---|---|---|---|
| completability | UNWINNABLE | UNWINNABLE | UNWINNABLE |
| conformance | 45/47 | 44/47 | 43/47 |
| binary verdict | NEAR-FULL | NEAR-FULL | NEAR-FULL |
| `flee` success returns the death sentinel | ✓ | ✓ | ✓ |
| combat executes any unrecognised input as an attack | ✓ | ✓ | ✓ |
| `save` advertised in `help`, no such command | ✓ | ✓ | ✓ |
| `load_game` authored, no caller anywhere | ✓ | ✓ | ✓ |
| EOF → uncaught traceback | ✓ | ✓ | ✓ |
| both modification probes pass on DATA ALONE | ✓ | ✓ | ✓ |
| README contradicted by play (4 claims) | ✓ | ✓ | ✓ |
| NPC dialogue: one fixed line, `options: []` | ✓ | ✓ | ✓ |
| two-phase boss is a `print`, not a mechanic | ✓ | ✓ | ✓ |

The conformance spread (43–45) is explained and disclosed by the judges
themselves: it turns on #36/#37 (does an authored-but-uncalled `load_game`
count as "state loads") and #16 (`help` advertising a command that does not
exist). J3 flagged its own #37 call as "the tally's one genuine judgement
call". All three land NEAR-FULL regardless, so the spread changes nothing.

## The margin, resolved across three readings

All three proved unwinnability analytically — the game has no RNG, and J3 ran
72 scripted full-route attempts for 0 wins against 20 byte-identical
instrumented runs.

They differ on the size of the gap, and it resolves cleanly:

* **J1: short by 3 HP** — drank the potion at 12 HP, where the 20-cap wastes 2.
* **J2 and J3, independently: arrive at 16 HP** — potion drunk at 6 HP, full 10
  recovered. J2 frames it as 1 HP short of the 17 needed; J3 as 30 damage
  delivered against 40.

Those are the same fact from two directions: 16 HP buys exactly 4 turns, 4 turns
deal 10/5/10/5 = 30, and one more HP buys the 5th turn worth 10 more. **The
margin is 1 HP.** J1's line was simply suboptimal, and two judges converged on
the optimal one without contact.

## The signature, which is the actual finding

Every headline defect is a component built to specification and never joined to
the one beside it:

* seven authored error strings (`Take what?`, `Go where?`, …) unreachable,
  because the parser only matches `take ` with a trailing space
* `load_game` written correctly, imported by the engine, never called
* `save` in the help text, absent from the parser
* boss "second phase" a `print` that reprints every turn and changes no stat
* a Torch that "provides light in dark places" in a world with no dark places
* `flee` returning the same `False` for escaping as for dying, so the one
  monster you *can* escape kills you: `You flee successfully!` immediately
  followed by `*** GAME OVER ***`
* a win screen that is correct and wired, priced one hit point out of reach

J3 put it best: *"a tree assembled against a checklist rather than a game
brought up and played."* J2 independently: *"assembled against a specification
and tested against the same specification, never against itself."*

## The strength, also unanimous

**Workability.** Both mandatory modification probes passed with a single
data-file edit and zero code touched — a ninth room added and navigated, a
weapon's damage changed and felt in play. Seven modules with sensible seams,
dataclasses, a real `pyproject.toml`, and a genuinely data-driven world.

J2's extra probe found the limit: renaming the boss in data alone makes the game
endless, because `engine.py` tests `monster.id == 'dragon'` as a string literal.
Content seams are clean; logic seams leak.

## Flagged for the operator before any flight is booked

J3 raised a **panel-split risk**, unprompted: Panel A has real content to vote
(wide working surface, correct reset round-trip, 6/10 clean probes, both
modification probes passing) while Panel B has almost nothing (it reached for
exactly the checklist and its world is the brief's own example spelled out).
Flown against an opponent that reached further and delivered less, the panels
will disagree — and §6 halts the placement for a ruling when they do.

## Two instrument defects this run exposed

1. **The blind scan could not see a new model's name.** The artifact's README
   opened `# Muse Glimmer 30b` and `pyproject.toml` set
   `name = "muse-glimmer-30b"`; the scan passed it "no model names — judgeable".
   `MODEL_IDENTIFIERS` is a hand-maintained denylist, so the arm it cannot cover
   is the NEW one. Fixed: `stage.py --arm-identifier`, passed by the runner.
   The model named the project after itself — nothing in the pipeline wrote that
   string.
2. **The solo packet demanded a score the rubric forbids.** All three judges
   independently opened their record by declaring the conflict and refusing to
   invent a scale. Fixed: the template now states the prohibition first and asks
   for the §4 facts pass.

Raw records: `dev/blind_panel/records/muse_20260814_j{1,2,3}.md`.
