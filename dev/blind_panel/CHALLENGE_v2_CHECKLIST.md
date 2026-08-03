# CHALLENGE v2 — conformance checklist (judge's half of the pair)

The checkable half of `TIER_RUBRIC` v2's **binary** conformance verdict —
deliberately rubric-unversioned beyond the brief pairing, because this list is
the CHALLENGE's contract and does not turn over when the rubric is amended.
Paired with `missions/game_challenge_tier.yaml` **version: 2** (the paragraph
the model receives). The model never sees this list, and never sees the rubric.

**Verdict = binary.** `NEAR-FULL` unless more than 20% of the 47 items are
unmet (unmet ≥ 10) **or** an element of the brief's core loop is missing
(rubric §7) — then `SIGNIFICANTLY-DEVIATED`. There is no per-item score.

**v2 change (2026-08-03):** the brief's YAML-data-files sentence was removed —
prescribing where world data lives measured compliance with an architectural
opinion, not the model's judgment — so v1 items #3–#8 are dropped. The one
requirement that sentence carried beyond format ("the connections between
them") is re-homed in the brief itself as "at least eight CONNECTED rooms"
(item 38). 53 → 47 items, renumbered.

---

## The two rules that make this checkable

**1. PRESENCE, not reachability.** An item is met if it exists in the code or
the shipped data. *It does not have to be reachable in play.* If the brief asks
for three monsters and three monsters are authored, the model conformed — even
if a loader bug orphans all three so none can ever be met.

Unreachability is charged on the rubric's axes (delivery), never here.
Conformance asks *did it do what was asked*; the rest of the rubric asks *did
it deliver*. One number hides that; two dimensions show it.

Item 38's "connected" is still a presence check: it is a property OF the
authored data (the room graph, read from wherever the model chose to define
it), not of play. Rooms count as connected if the authored exits link all of
them into one component reachable from the starting room.

**2. EVERY ITEM IS TRACEABLE.** Each row quotes the phrase in the objective it
comes from. Nothing may be checked that the model was not asked for. If the
objective is edited, this file is re-derived AND a new epoch opens (rubric §1).

Count-based items are checked by **counting**. Named features are checked by
**finding them**. This is exactly the boring conformance a judge skips, which is
why it is a checklist and not a judgement.

---

## Launch — 2

| # | requirement | from the brief |
|---|---|---|
| 1 | Runs from the command line — one obvious entry point | "The game runs from the command line" |
| 2 | A title screen is shown before play begins | "It should open on a title screen before play begins" |

## Command surface — 15

| # | requirement | from the brief |
|---|---|---|
| 3 | `go` with north / south / east / west | "movement (go north/south/east/west)" |
| 4 | `take` | "inventory handling (take, …)" |
| 5 | `drop` | "(…, drop, …)" |
| 6 | `use` | "(…, use, …)" |
| 7 | `examine` | "(…, examine, …)" |
| 8 | `equip` | "(…, equip)" |
| 9 | `talk to` | "conversation (talk to)" |
| 10 | `attack` | "combat (attack, flee)" |
| 11 | `flee` | "combat (attack, flee)" |
| 12 | `look` | "look, status, help, and quit" |
| 13 | `status` | "look, status, help, and quit" |
| 14 | `help` | "look, status, help, and quit" |
| 15 | `quit` | "look, status, help, and quit" |
| 16 | `help` output lists the real command vocabulary | "Help should tell a new player what they can actually type" |
| 17 | `status` shows health, equipment **and** location | "status should show health, equipment, and location at a glance" |

## Combat — 16

| # | requirement | from the brief |
|---|---|---|
| 18 | Turn-based combat loop | "Combat is turn-based" |
| 19 | Player has health **and** attack power | "The player has health and attack power" |
| 20 | A weapon changes combat stats once equipped | "weapons … change those stats once equipped" |
| 21 | Armour changes combat stats once equipped | "armour found in the world change those stats once equipped" |
| 22 | A healing item is usable mid-combat | "healing items can be used in the middle of a fight" |
| 23 | Three regular monsters | "Three regular monsters" |
| 24 | Monsters are placed to guard specific rooms | "guard specific rooms" |
| 25 | Monsters fight back | "and fight back" |
| 26 | Monsters have distinct behaviours, not one shared script | "each with its own behaviour rather than a shared script" |
| 27 | A final boss exists | "The final boss" |
| 28 | The boss has two phases | "has two phases" |
| 29 | The boss has a weakness to one specific item | "a weakness to one special item" |
| 30 | The weakness item is placed in the world, not held at start | "hidden somewhere in the world" |
| 31 | A win path exists on defeating the boss | "beating it wins the game" |
| 32 | Death produces a defeat screen | "dying ends the run with an honest defeat screen" |
| 33 | The defeat screen offers a restart | "that offers a restart" |

## State and content — 14

| # | requirement | from the brief |
|---|---|---|
| 34 | Player state tracked: location, stats, equipment, inventory | "location, stats, equipment, inventory" |
| 35 | World state tracked: room changes, NPC dialogue progression, defeated monsters | "room changes, NPC dialogue progression, and which monsters have already been defeated" |
| 36 | Full state saves to JSON | "support saving … all of it to JSON" |
| 37 | State loads from JSON | "saving and loading all of it to JSON" |
| 38 | At least 8 rooms, connected (one component from the start room) | "at least eight connected rooms" |
| 39 | At least 5 items | "and five items" |
| 40 | A weapon among the items | "among them a weapon" |
| 41 | A piece of armour among the items | "a piece of armour" |
| 42 | A healing item among the items | "a healing item" |
| 43 | The boss-weakness item among the items | "and the boss-weakness item" |
| 44 | Two NPCs | "plus two NPCs" |
| 45 | NPC dialogue branches — PRESENCE-LENIENT (operator ruling 2026-08-03): staged topic progression MEETS this item; the strength gradation (fixed single line < staged progression < state/choice-conditional dialogue) is judged comparatively on axes B6/B7, not here | "whose branching dialogue" |
| 46 | NPC dialogue hints at the boss weakness | "hints at what the boss is vulnerable to" |
| 47 | Combat is narrated | "narrates combat clearly" |

---

## Reporting

Report the tally as `met / 47` with the **unmet items listed by number**, then
the binary verdict (`NEAR-FULL` / `SIGNIFICANTLY-DEVIATED`, with the trigger —
count or core-loop — named). The unmet list is the useful artifact — across
arms it shows which parts of a brief models systematically drop, which no
single verdict can.

Item 31 deserves a specific note. In the 2026-07-27 panel gpt-oss shipped a
fully-written defeat path and **no win condition anywhere in the tree** — a grep
for congratul/won/victor returned nothing. That asymmetry is worth watching for
across every batch; it is checked here as presence (does a win path exist in
code) and separately as delivery on the rubric's axes.
