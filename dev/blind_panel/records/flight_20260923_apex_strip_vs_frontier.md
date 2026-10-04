# flight_20260923 — qwen3.8-flash-next APEX (strip ON, functional phase) vs FRONTIER

**Rubric:** TIER_RUBRIC v2.1 · one Opus judge · prompt extracted verbatim from
`FLIGHT_PROMPT.md` (programmatically, `{PACKET_ROOT}` the only substitution).
Packets built with `make_judge_packet.py`; identifier scan and anchor scan clean
on both, RUBRIC/CHECKLIST byte-identical across A and B.

**Key** (held outside the packet root, judge never saw it):
- **A** = `dev/blind_panel/anchors/v2.0/frontier-sonnet-20260803` — FRONTIER
  anchor, Claude Sonnet single shot.
- **B** = `~/ouroboros-runs/tier_20260922-233228/staged/arm01/alpha` —
  `qwen3.8-flash-next-177b-a6-apex`, **strip_prior_reasoning ON** (hybrid turn
  rollback), session mode, `top_phase: functional`, contemplator league,
  **stopped at the 30-cycle cap** (435 min, 24/57 goals: 7/7 structural,
  17 functional verified, 1 failed at the cap, 33 untested).

Side-swapped against flight_20260921 (frontier sat on B there).

## Result

**Delivery: 2–2 · Character: FRONTIER 5–1 · OVERALL: FRONTIER.**
No PANEL SPLIT (a level panel points nowhere), not flagged CLOSE — the judge's
note: "the nearest this flight comes to a CLOSE case". METHODS §5 family
caveat applies (Opus judge, Claude-authored frontier).

**TALLY CORRECTION.** The judge's first report read "Delivery: A 3–1" over
axis choices that sum to 2–2 (A1 A, A2 B, A3 B, A4 A). Asked to reconcile —
without re-opening either artifact — it confirmed an arithmetic slip, kept
every axis choice, withdrew "both panels point to A", and restated the overall
as resting on Character plus the weight of A1/A4 (core-game working surface
and scope) over A2/A3 (a phase-2 save edge and EOF at secondary prompts).

Local artifact took **A2 state integrity**, **A3 robustness**, **B5 ambition**.

| axis | pick | judge's line (condensed) |
|---|---|---|
| A1 working surface | A | A's surface all works; B: inert key, monsters that never guard, `talk to <short name>` fails, a failing shipped feature test |
| A2 state integrity | **B** | B round-trips everything incl. a mid-combat save; A drops the boss's phase-2 attack bonus on reload |
| A3 robustness | **B** | B never tracebacks (EOF everywhere clean); A tracebacks on EOF at 3 prompts + a semantically bad save |
| A4 delivered scope | A | 7 item kinds, poison, shield-brace, double-strike phase, conditional NPCs vs 5 items (1 inert) and skippable monsters |
| B5 ambition | **B** (narrow) | topic-menu dialogue graph, a boss that re-forms with a timed weakness window, lock-and-key + mutable rooms, mid-combat/mid-talk saves |
| B6 imagination | A | the grief-curdled king and the burned nun vs a competent, generic forest |
| B7 felt play | A | A's combat is tense and teaches the weakness; B's monsters never engage and fights are fixed arithmetic |
| B8 craft/UI | A | accurate help, legible status, confirm-on-quit; B's help omits `talk to`/`inventory`, hits show no damage |
| B9 workability | A | both probes small; B's save freezes the world, items can't stack, 330 dead duplicate lines + a failing test |
| B10 documentation | A | A's README specific (two errors: a mirrored map, a ghost who is alive); B's accurate but generic |

## Movement against the structural-only flight (2026-09-21)

| | 09-21 structural-only | 09-23 strip on, functional |
|---|---|---|
| Delivery | FRONTIER 3–1 | **2–2** |
| Character | FRONTIER 4–2 | FRONTIER 5–1 |
| local axes | A2, B5, B9 | A2, A3, B5 |

Delivery closed a point (the functional phase landed A3 robustness — no
tracebacks anywhere — where the structural-only arm shipped a `?` crash);
Character lost B9. Different designs, n=1 each; read direction, not size.

## B's decisive defects — verified in the tree after the verdict

  * **`talk to <name>` is a cross-module seam bug.** `parser.py` keeps `to` in
    the args for `talk`, and the name matcher compares by substring: `talk
    maren` and `talk to old maren` work, `talk to maren` (the brief's own
    syntax) gets "You don't see them here." Reproduced.
  * **Monsters guard nothing.** "A goblin is here. (18/18 health)" — and the
    player walks on; the judge won without fighting a single regular monster.
    The goal that would have tested it ("encounter three regular monsters
    guarding specific rooms") was among the 33 the cycle cap left untested.
  * **`equip armour` fails** ("You don't have that item.") — the goal failed
    its interact test in the last cycles; the diagnosis (slot-word resolution
    in `Game.equip`) was recorded, the cap landed before the fix. The shipped
    `test_user_can_equip_armour…` fails accordingly.
  * **An unwired lock-and-key / room-change layer** — `old_key`, room flags
    `cabin_unlocked` preset true, `altered_description` everywhere `None`.
    Model-innate: authored in the structural walk, never bound.

## Framework finding: the 330 dead lines in `main.py` — how the flows produced them

Charged against B9 by the judge. Traced through the flows and the turn
transcripts (structural session `67c4c0a1afec4180`, turns 7–9):

  1. **The checker reported its own blind spot as a violation.** At the last
     file the fileset round-trip check ran over a `save.py` that built its
     payload through `state_to_dict` → `_player_to_dict` helpers — a shape
     `_serialized_roundtrip_violations` cannot read — and emitted the
     UNVERIFIED variant ("payload keys could not be read from either side …
     Build the payload where it is serialized, or through a to_dict/from_dict
     pair"). The pair was symmetric — the model verified it in its own
     thinking. The blind spot is SHAPE, not name: `_payload_methods` follows
     producers/consumers only as METHODS inside a class (`state.to_dict()`,
     `GameState.from_dict(...)`); module-level functions are invisible under
     any name. Re-run on the final artifact the check STILL reports
     UNVERIFIED, renamed or not (2026-09-23).
  2. **`action_check_session_file` charged it to `main.py`.** The UNVERIFIED
     message names functions, not files, so `_implicated_file(...) or
     current` fell back to the file just written.
  3. **The repair instruction framed it as `main.py`'s defect to fix in
     `main.py`** — "Rewrite the SAME file so those problems are gone", every
     finding "a name, a key or an id that two files disagree about … not
     opinions", a round-trip gloss for the OTHER variant, "Change nothing
     else" (and a `save.py` block would have been dropped by the writer).
     Turn 8's thinking named the right target — "Maybe we need to output
     save.py? But prompt says … rewrite SAME file" — then reverse-engineered
     the checker and added a delegating `to_dict`/`from_dict` pair to
     `main.py`, following the finding's own hint.
  4. **The loop repeated a byte-identical prompt.** Turn 9 — "we added
     to_dict/from_dict … still failed … We can only rewrite main.py" —
     escalated to a full standalone serializer: the 330 lines.
  5. **The sweep closed the goal on a proxy.** `diagnose_issue` inferred that
     the gate "pairs on names" (wrong); `file_ops/patch` renamed correct code
     in `save.py` to match the guess; the goal completed on the patch's own
     per-file validation. The round-trip check lives only in the session walk
     and was never re-run — it would still have failed. Nothing revisited the
     two in-session repairs made to `main.py` either.

Not the only option: returning `main.py` unchanged was available at both
repairs (nothing in it was wrong; the sweep fixed `save.py` anyway). The
flows made it the least likely move. Fix candidates: the checker follows
the DATA (whatever feeds `json.dump`, whatever consumes `json.load`, resolved
to its definition — method or function, any name) rather than a shape list;
an UNVERIFIED verdict is a note routed to the functional save/load goals
(runtime is the ground truth), never a violation charged to a file; real
findings name their owner file, never defaulting to the current one; a goal
opened by a finding closes on that finding's check, not a proxy; the repair
instruction is trimmed to the finding in hand; the vocabulary cap is gone
(done 2026-09-23).

**Checker replaced (2026-09-23).** `agent/actions/roundtrip_contract.py` holds
the save/load pair to the persisted shape the design declared, follows the
data under any name with each call in its own context, logs an unfollowable
side instead of charging it, and names the file to fix. On this artifact: 54
declared keys compared, 0 findings. Corpus: 22 artifacts with a declared
contract, 1 finding (true); mutation recall 86/97, every miss in a shape it
logs as unjudgeable.

## Context the scorecard does not show

  * **A cycle-capped artifact, not a completed one.** 30 cycles, 17 functional
    goals verified, 33 untested — combat blocking, NPC progress, the boss and
    save/load among them. Like 09-21, this is not the 2026-08-22 column.
  * **The strip did its job.** The structural walk ran 144 min (vs 305), 7/7
    files in-session (vs 5/6 + an 187-byte stub + a refused main.py), depth at
    file 5 21k tokens (vs 184k), zero context overflows. What the judge saw is
    the model's design and the functional phase's reach, no longer a keystone
    rebuilt from a thinking fragment.
  * **B's state integrity is a strength worth keeping**: whole-world save incl.
    a fight in progress and a conversation node, round-tripping everything the
    judge tried. The flip side (a save freezes the world definition) cost B9.
