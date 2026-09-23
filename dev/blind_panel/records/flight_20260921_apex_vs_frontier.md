# flight_20260921 — qwen3.8-flash-next APEX (STRUCTURAL-ONLY) vs FRONTIER

**Rubric:** TIER_RUBRIC v2.1 · one Opus judge · prompt extracted verbatim from
`FLIGHT_PROMPT.md`, only `{PACKET_ROOT}` substituted.

**Key** (held outside the packet root, judge never saw it):
- **A** = `~/ouroboros-runs/structural-q38fn-apex-20260921` —
  `qwen3.8-flash-next-177b-a6-apex`, top_phase **structural**, session mode,
  54 goals, 1.46 h turn span.
- **B** = `dev/blind_panel/anchors/v2.0/frontier-sonnet-20260803` — FRONTIER
  anchor, Claude Sonnet single shot.

Side-swapped against flight_20260829 (frontier sat on A there) so the
assignment doubles as the position-bias check. `stage.py` identifier scan
returned "no model names — judgeable" on both packets.

## Result

**Delivery: FRONTIER 3–1 · Character: FRONTIER 4–2 · OVERALL: FRONTIER.**
Panels agreed — no PANEL SPLIT. Not flagged CLOSE. METHODS §5 family caveat
applies (Opus judge, Claude-authored frontier).

Local artifact took three axes: **A2 state integrity**, **B5 ambition**,
**B9 workability**.

## READ THIS BEFORE COMPARING IT TO 2026-08-22

**This artifact stopped at the STRUCTURAL ceiling.** No functional phase, no
test gate, no quality gate, no polish. The 2026-08-22 qwen3.8-27b scorecard
that tied the frontier on Delivery was a COMPLETED artifact through the full
pipeline; the 2026-08-29 rerun had three polish entries on top of that.
Comparing the two numbers directly measures the missing phases, not the model.

The three defects that decided Delivery are precisely what those phases exist
to catch, and all three were verified independently in the tree afterwards:

  * **`flee` does not exist.** Advertised in help and README; the only
    occurrence anywhere in the artifact is `README.md:31`. Combat is
    inescapable.
  * **`examine` is a seam bug.** `parser.py:63` maps `"examine": "look"` and
    discards the target; `engine.py:435` defines `_look(self)` taking none.
    All 11 authored item/monster/NPC `description` strings in `world.json`
    are unreachable — a whole flavour layer dead behind one wrong binding.
  * **`?` kills the process.** `parser.py:144` does `first = tokens[0]`
    unguarded after `_clean_text` strips non-alphanumerics. Uncaught
    `IndexError`, in the main command loop, reachable by one keystroke.

## What the structural phase DID deliver

  * **WON** — victory reached and reproduced by the judge.
  * **45/47 conformance, NEAR-FULL** (unmet: 7 `examine`, 11 `flee` — both
    the defects above).
  * **12 rooms, 12/12 reachable, ZERO unplaced entities** (5/5 items, 4/4
    monsters, 2/2 NPCs). The campaign's most common decisive defect — seven
    prior artifacts, including one of our own anchors — absent again.
  * **The only schema-versioned save in the flight** (`schema_version`,
    per-room item lists, per-monster health/phase/cooldown, NPC node ids and
    `completed_nodes`, `active_combat`). Took A2 outright: the frontier
    tracebacks with `KeyError` on an unknown room id where this returns a
    clean `No save found.`
  * **The cleanest modification probe of the two** (B9): a new room, a new
    weapon and a damage change entirely inside `world.json`, **zero Python**.
    Full world externalisation — a non-programmer could author content.
  * **B5 ambition** on the externalised world, a real branching dialogue node
    graph with completion tracking, four dispatched monster behaviour
    handlers, and 12 rooms against the frontier's 9.

## The judge's closing distinction, worth keeping

> "A built the better machine for authoring a game and then shipped a game
> where the monsters guard nothing, the flavour text is unreachable, `flee`
> exists only in the README, and a single `?` kills the process. B built the
> smaller machine and shipped the game."

Also first for the campaign: **a cross-module seam bug that degraded a content
layer instead of blocking the win.** That class has been terminal seven times;
here it cost the flavour text and nothing else.

## Framework findings from the same run

Neither is charged to the model:
  * `engine.py` is **43,310 bytes** — one god-module holding the loop, every
    handler, dialogue and state. The judge flagged it against B9 while still
    awarding the axis. It is exactly the kind of file the `examine` seam hid in.
  * The structural phase reported `8/8 files clean` over a tree containing all
    three defects above. The syntax tier is skipped before the environment
    phase (`.py` only checked `elif ext in env_config`), and the AST typecheck
    returns `[]` on `SyntaxError`. Neither gate can see a wrong binding
    between two files that each parse.
