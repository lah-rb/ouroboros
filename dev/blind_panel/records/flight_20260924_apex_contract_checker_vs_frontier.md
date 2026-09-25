# flight_20260924 — qwen3.8-flash-next APEX (strip ON, contract round-trip checker) vs FRONTIER

**Rubric:** TIER_RUBRIC v2.1 · one Opus judge · prompt extracted verbatim from
`FLIGHT_PROMPT.md` (programmatically, `{PACKET_ROOT}` the only substitution).
Packets built with `make_judge_packet.py`; identifier and anchor scans clean
on both; RUBRIC/CHECKLIST byte-identical across A and B; packet
`INSTRUCTIONS.md` kept, as on 09-23, so the two flights share one protocol.

**Key** (held outside the packet root, judge never saw it):
- **A** = `~/ouroboros-runs/tier_20260923-165314/staged/arm01/alpha` —
  `qwen3.8-flash-next-177b-a6-apex`, strip_prior_reasoning ON, session mode,
  `top_phase: functional`, contemplator league, **stopped at the 30-cycle
  cap** (430 min, 27/80 goals: 9/9 structural, 18/71 functional verified,
  53 untested).
- **B** = `dev/blind_panel/anchors/v2.0/frontier-sonnet-20260803` — FRONTIER
  anchor, Claude Sonnet single shot.

Side-swapped against flight_20260923 (frontier sat on A there).

**What changed vs the 09-23 arm:** the serialized round-trip check is
contract-scoped (`agent/actions/roundtrip_contract.py`) — held to the
design's declared save shape, data-following, an unfollowable side logged
rather than charged, findings naming the file to fix; the 4,000-char
binding-vocabulary cap is gone; the repair prompt's round-trip bullet matches
the new findings. New design (the design phase reran), so the game differs.

## Result

**Delivery: FRONTIER 3–1 · Character: FRONTIER 5–1 · OVERALL: FRONTIER.**
No PANEL SPLIT, not flagged CLOSE. METHODS §5 family caveat applies (Opus
judge, Claude-authored frontier). Axis lines re-added before recording: they
sum to the tallies the judge reported.

Both artifacts WON and NEAR-FULL (A 46/47, unmet #16 — help omits `flee` and
`examine`; B 47/47). Local took **A3 robustness** and **B9 workability**.

| axis | pick | judge's line (condensed) |
|---|---|---|
| A1 working surface | B | everything B offers works; A's load fails after the first kill |
| A2 state integrity | B | B round-trips the whole world incl. boss phase (minor phase-2 bonus gap); A cannot resume any save made after a kill, and a failed load half-rewrites live state |
| A3 robustness | **A** | A exits cleanly on EOF at every prompt; B tracebacks on EOF at title, quit-confirm, restart, and on a corrupt save |
| A4 delivered scope | B | 7 items in two gear tiers, three distinct monsters + poison, reactive NPCs, working save vs the minimum 5 items and a broken load |
| B5 ambition | B | poison, brace, frenzy, gear tiers, three hint routes, kill-reactive dialogue, resistance→weakness; A's reach went into engine scaffolding (its bell-exposes-shell boss is the cleverer single idea) |
| B6 imagination | B (narrow) | A has the more original seed (a drowned monastery whose bells still ring); B sustains its voice everywhere |
| B7 felt play | B | B's combat tense, NPCs react; A's boss one-shot by the bell, non-reversing map directions, `flee` never revealed, post-kill saves unrecoverable |
| B8 craft/UI | B | complete help, combat options line, `x`, quit confirm vs A's help omissions, ids in room headers, grammar slips |
| B9 workability | **A** | both probes passed; A's were data-only edits in one file and its loader names cross-reference placement errors precisely |
| B10 documentation | B | B's README detailed and mostly verified (map wrong); A's generic, miscounts rooms, claims save/load that play contradicts |

## Movement across the three APEX flights

| | 09-21 structural-only | 09-23 strip on, functional | 09-24 contract checker |
|---|---|---|---|
| Delivery | FRONTIER 3–1 | 2–2 | FRONTIER 3–1 |
| Character | FRONTIER 4–2 | FRONTIER 5–1 | FRONTIER 5–1 |
| local axes | A2, B5, B9 | A2, A3, B5 | A3, B9 |

Different designs every time, n=1 each; read direction, not size. The axis
that swung is **A2**: 09-23's design round-tripped a fight in progress; this
design's load breaks after the first kill.

## A's decisive defect — verified in the tree after the verdict

  * **Load fails after any kill: a value-vocabulary slip INSIDE one
    function, not a cross-module seam** (the judge's label is corrected
    here). `engine._apply_save` builds `state.defeated_monsters` as a set of
    id STRINGS — as the declared contract says (`set of monster_id
    strings`) — then, twenty lines later, `any(monster.behavior == "boss"
    for monster in state.defeated_monsters)` → `AttributeError: 'str'
    object has no attribute 'behavior'`, after the room, player, inventory
    and dialogue state are already applied. Model-innate, authored in the
    session walk (engine.py was the orbit-retry turn; the functional fix
    later touched `_look`/`_present_scene`/`parse_command`, not
    `_apply_save`).
  * **What could have caught it:** not the round-trip check — it compares
    KEYS, and every key agrees (9 declared keys compared, 0 findings,
    `player.*` unjudged and verified symmetric by reading). A real static
    type check would (`Set[str]` element → `.behavior`); the per-file gates
    run syntax/lint/import and the cross-module check is call-shape only.
    At runtime the save/load functional goals would have — they were among
    the 53 the cycle cap left untested.

## Framework findings from this run

  1. **The round-trip ride is gone.** The walk ran 9/9 files with ZERO
     repair turns; the checker compared the declared save shape and said
     how much it compared. Mid-run it also surfaced two scope bugs in the
     checker itself (a save declared as "Saved game JSON" — no file token —
     was out of scope; widening to format words then pulled in an input
     "World data (loaded from YAML)" shape), both fixed and corpus-validated
     before the run's first file check, and a both-absent rule that was
     design drift, removed.
  2. **Stray acceptance-check artifact (1 cycle).** A derived acceptance
     check (`… python3 main.py > new_game_acceptance.out …`) validated on
     create in the live workspace left its output file; the transient-file
     observer charged it to the project and the warning sweep spent cycle 15
     diagnosing it (no patch, a diagnosis note). Fix candidate: framework
     check commands leave no trace — snapshot the tree around a check run
     and remove what it created.
  3. **A wrong authored test vetoed a working feature (2 cycles, at the
     cap).** After a real fix (`_look` treated every target as the room),
     the authored regression test asserted `room.name in examined`
     (`'Harbor Steps'`) against output that renders `HARBOR STEPS` — a
     presentation fingerprint the author-test prompt's own rule 2 forbids.
     The behaviour passed; the test vetoed; the sweep's answer ("retest
     directly") can never clear it because the test does not change. It
     ships as the judge's "one shipped test fails". Fix candidate: when the
     behaviour passes and an authored test fails, the TEST is the suspect
     to diagnose.
  4. **Hybrid rollback on degenerate turns fired twice, both clean.** The
     engine.py thinking orbited ("Potential issue: _find_item_by_target …"
     enumeration, drifting from ~20–25k tokens in, exact period ~689 B by
     79,872 tokens / 71 min); the long-cycle guard fired, the KV rolled back
     to the last committed turn (pos 35,735) with no replay, and the retry
     landed in 34k tokens. A token run-length degeneration in a tester
     session recovered the same way. The residual cost is detection latency
     on a drifting orbit, not recovery.

## Context the scorecard does not show

  * **Cycle-capped, not complete.** 18 of 71 functional goals verified; the
    design produced 71 functional goals (09-23: 50) and the sweep reached
    items before the cap — combat, NPCs, the boss and save/load untested.
  * **The judge WON the game** — through every regular monster, the
    phase-2 shell and the mirror-bell weakness, with an honest defeat and
    restart (09-23's artifact was won too, but without fighting a single
    regular monster). Its placement is complete (9/9 rooms reachable, nothing
    unplaced) and its loader validates cross-references — the property that
    took B9.
