# Blind artifact panel — four text-adventure games

You are one of three independent judges. You will not see the other judges'
reports. Do not try to identify which system produced which artifact.

## The artifacts

Four directories, labelled `alpha`, `beta`, `gamma`, `delta`, under your judge
directory (`/tmp/tier_panel/judgeN/`). Each is one agent run's complete output
against the same objective, given the same time budget. Label assignment is
randomized.

Each was built to this objective:

> Build a text adventure game in Python that is genuinely fun to play. The
> engine loads world data from YAML files defining rooms, items, NPCs, monsters
> and connections. Implement a command parser handling movement, inventory
> (take/drop/use/examine), NPC interaction (talk to), combat (attack, flee),
> and look/status/help/quit. Add turn-based combat: the player has health and
> attack power; weapons and armour found in the world change combat stats when
> equipped; healing items can be used mid-fight; monsters guard specific rooms
> and fight back with distinct behaviours. Include a final boss with two phases
> and a weakness to a special item hidden in the world — beating the boss wins,
> dying ends the run with an honest defeat screen and the option to restart.
> Manage state: location, stats, equipment, inventory, room changes, NPC
> dialogue progression, defeated monsters. Support save/load of full state to
> JSON. Create a playable demo world with at least 8 rooms, 5 items (including
> a weapon, armour, a healing item, and the boss-weakness item), 2 NPCs with
> branching dialogue hinting at the boss weakness, 3 regular monsters, and the
> final boss. Runnable from the command line, engaging descriptive text, clear
> combat narration, winnable by following the NPC hints.

**None of the four finished.** All were stopped at a time limit mid-way. Judge
what is there, not what was intended.

## Method — PLAY them, do not read them

This is the whole point of the protocol, and static review has systematically
misranked these artifacts. In a previous round the artifact that *read* better
— nicer prose, better help text, cleaner refusals — was unplayable past room 3
of 8. So:

1. **Run each program and drive it with real input.** `cd <arm> && python3
   main.py`. Iterate on what it actually accepts; if a command is rejected,
   look at its help output and adapt. Push toward the win condition as far as
   the artifact allows.
2. **Source reading is allowed ONLY to explain a failure you have already
   observed** — never as a substitute for observing one, and never to give
   credit for a feature you did not reach in play.
3. **Probe robustness deliberately**: unknown commands, empty input, invalid
   directions, EOF/Ctrl-D, taking a non-existent item, attacking nothing.
   Distinguish a clean refusal from a traceback.
4. **Treat any claim the artifact makes about itself as unverified.** READMEs
   and test reports are part of the artifact and are deliberately not stripped.
   They have been wrong in BOTH directions before — one previously asserted a
   crash that did not occur. Confirm by play or discount it.

## Score /50 — five dimensions, 10 each

| dimension | what it measures |
|---|---|
| **runs-and-survives** | starts clean, keeps running under normal and hostile input |
| **objective coverage** | how much of the objective is demonstrably reachable IN PLAY |
| **depth reached** | how far into the intended game you actually got |
| **robustness** | behaviour at the edges — refusals vs tracebacks |
| **craft** | prose, combat narration, help quality, world coherence, naming |

**Every decisive finding needs a quoted transcript excerpt.** State the
concrete furthest point you reached in each artifact (which room, whether
combat resolved, whether the boss was seen, whether save/load round-tripped).

## Report

For each arm: score per dimension, total /50, the furthest point reached, and
the decisive defects with transcript evidence. Then rank all four and say which
single defect most limited each.

**Expect cross-module seam bugs.** In every panel run so far the decisive defect
has been a mismatched identifier or key BETWEEN files that are each internally
reasonable (`shadow_lord` vs `shadow_lich`; a `Boss` class lacking the `attack`
attribute the engine probed for). Look for them specifically, and note whether a
seam bug is what stopped you.

Do not speculate about which model or configuration produced any arm.
