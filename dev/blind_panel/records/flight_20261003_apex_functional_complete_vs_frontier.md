# flight_20261003 — qwen3.8-flash-next APEX, functional phase COMPLETE (114/114) vs FRONTIER — 3-judge panel

**Rubric:** TIER_RUBRIC v2.1, with three Opus judges. Judge 1 self-flagged CLOSE, which fired the §6(a) escalation, so judges 2 and 3 were added. Each prompt was extracted verbatim from `FLIGHT_PROMPT.md`, with `{PACKET_ROOT}` the only substitution. Packets were built with `make_judge_packet.py` (`--arm-identifier qwen3.8-flash-next-177b-a6-apex` on the candidate). The identifier and anchor scans came back clean on both packets, and a manual grep for model and framework strings found nothing. RUBRIC, CHECKLIST and INSTRUCTIONS are byte-identical across A and B. Each judge got its own packet copy (`~/judge_flights/flight_20261003{,_j2,_j3}`), so their saves could not collide. After judge 1 played, its packet artifacts were confirmed unmodified.

**Key** (held outside every packet root; no judge saw it):
- **A** = `dev/blind_panel/anchors/v2.0/frontier-sonnet-20260803`: the FRONTIER anchor, a single shot from Claude Sonnet ("The Ashen Keep").
- **B** = `~/ouroboros-runs/tier_20260924-191710/staged/arm01/alpha`: `qwen3.8-flash-next-177b-a6-apex`, session mode, `top_phase: functional`, contemplator league with no time or cycle stop. Mission `4cee1f5fbc74` **completed 114/114** in 12,593 min (8 d 17 h 53 min): 38 files, 27 py_ok / 0 py_fail, 5 degenerations ("The Hollow Choir").

The sides are swapped relative to flight_20260924, where the candidate sat on A. This doubles as the position-bias check.

**Judged as shipped.** Operator direction: the judgement is of the framework plus model interaction. B therefore includes every edit the repair loop made during the functional phase, including the false-premise design changes listed in `~/ouroboros-runs/tier_20260924-191710/FIX_LIST.md` §§16 and 23. Nothing was reverted before staging.

## Result

**OVERALL: FRONTIER, 3–0 across judges. Two of the three self-flagged CLOSE.**

| | judge 1 | judge 2 | judge 3 | panel (axis majority) |
|---|---|---|---|---|
| Delivery | 2–2 | FRONTIER 3–1 | 2–2 | **2–2 (level)** |
| Character | 3–3 | FRONTIER 4–2 | 3–3 | **3–3 (level)** |
| overall | FRONTIER, **CLOSE** | FRONTIER, not CLOSE | FRONTIER, **CLOSE** | **FRONTIER 3–0** |

No judge reported a PANEL SPLIT; a level panel points nowhere. The METHODS §5 family caveat applies: Opus judges, Claude-authored frontier. The axis lines were re-added for each judge and sum to the tallies they reported.

**This is the closest any field artifact has come to the frontier.** Two independent judges called the candidate level on both panels. The best previous Character result against the frontier was 2–4 (APEX 09-21). Delivery 2–2 had been reached three times before (08-19, 08-22, 09-23).

### Per-axis picks

| axis | j1 | j2 | j3 | majority | |
|---|---|---|---|---|---|
| A1 working surface | A | A | A | **FRONTIER** | unanimous |
| A2 state integrity | A | A | A | **FRONTIER** | unanimous |
| A3 robustness | **B** | A | **B** | **local 2–1** | B: zero tracebacks under EOF anywhere vs A's EOFError at the title and quit-confirm; j2 weighed B's silent flee and Ctrl-C tracebacks heavier |
| A4 delivered scope | **B** | **B** | **B** | **local** | unanimous: locks and keys, map, menu dialogue, defend/unequip, vertical exits, 18 passing tests |
| B5 ambition | **B** | **B** | **B** | **local** | unanimous: a counter-relic per monster, escalation, data-driven engine |
| B6 imagination | **B** | **B** | **B** | **local** | unanimous: "teeth arranged in rows like pews", a candle-ghost in a lantern, a wax-and-bone bishop |
| B7 felt play | A | A | A | **FRONTIER** | unanimous |
| B8 craft/UI | A | A | A | **FRONTIER** | unanimous |
| B9 workability | **B** | A | **B** | **local 2–1** | B's probes were data-only in `world.json` with zero code edits and the tests still green; j2 weighed world content hardcoded in generic code |
| B10 documentation | A | A | A | **FRONTIER** | unanimous |

**Facts both sides agree on (all three judges):**
- Both artifacts WON. Both scored 47/47 NEAR-FULL.
- All 9 rooms are reachable in each, with no unplaced entity.
- No judge needed source knowledge to route around an obstacle in either game.

### What decided it (all three judges, B's side)

1. **Flee is a silent soft-trap.** It prints "You break away", but the player stays in the room, every move is blocked, and the monster restarts at full HP.
2. **The boss weakness gates nothing.** Phase 1 never attacks ("guards itself, watching for an opening"). Phase 2 is "exposed" with or without the chalice, so all three judges won without ever touching it. That makes the NPCs' hint ("shell cracks only when the chalice is offered") false in play.
3. **Monster flavour leaks across monsters.** The ghost's shriek fires for every monster, the Thrall's escalation text fires for the ghost, and "The Bone Sentinel steps aside" is printed for the Thrall.
4. **Smaller defects:** a meta hint line is appended to every dialogue node; "You use the relic" is printed on a plain mace attack; choosing "Leave" first silences an NPC for good; no damage numbers; the full status block reprints after every command.

## Post-verdict attribution, verified in the history store

The judges labelled B's decisive defects model-innate, correctly under charge-what-ships. The run's dulwich history shows where each one entered the tree. Most were **introduced by the functional-phase repair loop**, not by the model's original build (seq 236–251):

| judged defect | introduced by | origin |
|---|---|---|
| Flee soft-trap (every exit blocked while a monster lives) | patch `3ffeff05e6c4` (seq 26712) | the re-diagnosis of goal `87b734ed185e`, "Bone sentinel physically blocks the reliquary." (FIX_LIST §10). The original build's `flee` did not move the player either, but nothing blocked walking away. The flee-room fix was later diagnosed (60988) and never dispatched (§12) |
| Phase 2 auto-"exposed", so the chalice is optional | patch `920b995b47e5` (seq 75423) | the fix for goal `364b751b`, "Hollow Bishop second phase is exposed.": `_try_advance_phase` sets `weakness_triggered = True` on every phase advance |
| Phase 1 never attacks | `68c1d3f63376` | a design change from a blind diagnosis, with no observed failure behind it (§16, third) |
| The ghost's shriek fires for every monster | patch `7e0f7979b9cf` (seq 23131) | the fix for "Shrieking choir ghost weakens player attacks", placed in the generic monster-hit branch (§17) |
| "The Bone Sentinel steps aside" printed for other monsters | patch `ffe839d3e53f` (seq 33149) | the Sentinel environmental-bypass fix, which hardcodes the name in `engine._move` |
| A meta hint line appended to every dialogue node | patch `44d64627a20d` (seq 41145) | the goal's sentence pasted into the formatter (§10.4) |
| "You use the relic" on a plain mace attack | patch `9f2923a26543` (seq 106063) | the 10-03 Sentinel weakness fix routed plain attacks into the relic-use branch (§12) |
| Hint text vs play | dialogue rewrite `0fec9480ebe5` and splice `790e6f36a000` | §16 seventh, §23. The contradiction itself comes from `920b995b47e5` above |

**Reading.** The three axes the frontier won most clearly from B's play (A1, A2 and B7), and the "central mechanic gates nothing" argument all three judges used for the overall, rest mostly on repair-loop edits. The model's own build gave B the axes it won outright: A4, B5 and B6, plus A3 and B9 by majority. Read this as a framework finding, not a counterfactual score. No judge saw the pre-repair tree, and the unrepaired build was never play-verified end to end.

## Disclosures

- **Judge 2, wall deviation (self-reported):** it ran `git diff --no-index` once, "purely as a diff tool", to compare its modified scratch copy against the packet artifact. No repository or history was read, and it switched to plain `diff` afterwards. No identity leak is possible from that command. Recorded per METHODS 3b and not corrected.
- **Packet-builder bug found while building this flight.** `make_judge_packet.py <staged>/armNN` treated the arm directory as the artifact, because `_is_artifact`'s `*/main.py` glob matched `alpha/main.py`. It copied both `alpha/` and the `judge1/` play copy (66 files). The packet was rebuilt from `<staged>/armNN/alpha` (33 files) before any judge launched. `FLIGHT_PROMPT.md` already says to pass `alpha`. The builder's arm-dir fallback should check `alpha/` before the glob.

## Movement across the APEX frontier flights

| | 09-21 structural-only | 09-23 strip on, functional (capped) | 09-24 contract checker (capped) | **10-03 functional COMPLETE** |
|---|---|---|---|---|
| goals | structural | capped at 30 cycles | 27/80 (capped) | **114/114** |
| Delivery (local–frontier) | 1–3 | 2–2 | 1–3 | **2–2** (panel) |
| Character (local–frontier) | 2–4 | 1–5 | 1–5 | **3–3** (panel) |
| local axes | A2, B5, B9 | A2, A3, B5 | A3, B9 | **A3, A4, B5, B6, B9** |
| judges | 1 | 1 | 1 | **3 (2 CLOSE)** |

Each flight has a different design, so read direction, not size. This is the first artifact in the series to run its functional phase to completion.

**Against every frontier flight of the epoch:**
- **B6 imagination:** taken from the frontier for the first time, unanimously. Every earlier flight gave B6 to the frontier, including 08-15, 08-19, 08-22 and 09-23.
- **Character 3–3:** the best Character result yet. The previous best was 2–4 (09-21).
- **Delivery 2–2:** this level was reached before, by 08-19, 08-22 and 09-23.
- **A4 delivered scope:** taken once before, by the completed structural qwen3.8 artifact (08-22).
