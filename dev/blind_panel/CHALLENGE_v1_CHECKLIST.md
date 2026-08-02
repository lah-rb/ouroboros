# CHALLENGE v1 — conformance checklist (judge's half of the pair)

> **ARCHIVED (2026-08-03).** Pairs with brief version: 1 only — the pre-epoch
> record of `tier_20260731-050209` and earlier. Superseded by
> `CHALLENGE_v2_CHECKLIST.md` (47 items, binary verdict) for all epoch-v2.0
> judging. Never use this list against the v2 brief.

The denominator for `TIER_RUBRIC` §3.4 — deliberately unversioned, because this
list is the CHALLENGE's contract and does not turn over when the rubric is
amended. Naming a rubric version here only creates a second place to go stale.
Paired with
`missions/game_challenge_tier.yaml`, which is the **paragraph the model
receives**. The model never sees this list, and never sees the rubric.

**Score = `round(10 × met / 53)`.**

---

## The two rules that make this scoreable

**1. PRESENCE, not reachability.** An item is met if it exists in the code or
the shipped data. *It does not have to be reachable in play.* If the brief asks
for three NPCs and three NPCs are authored in YAML, the model conformed — even
if a loader bug orphans all three so none can ever be met.

Unreachability is punished, hard, by §3.1 no-broken-functions (UNREACHABLE,
−2 each) and by §3.5 ambition|completeness. It is **not** punished here.
Conformance asks *did it do what was asked*; the rest of the rubric asks *did it
deliver*. Keeping those apart is what made the laguna-S result legible: it would
score near-full marks on this list — boss, weakness, two NPCs, three monsters,
all correctly authored — and be destroyed on delivery. One number hides that;
two dimensions show it.

**2. EVERY ITEM IS TRACEABLE.** Each row quotes the phrase in the objective it
comes from. Nothing may be scored that the model was not asked for. If the
objective is edited, this file is re-derived or conformance starts penalising
models for requirements they never received.

Count-based items are checked by **counting**. Named features are checked by
**finding them**. This is exactly the boring conformance a judge skips, which is
why it is a formula and not a judgement.

---

## Launch and data — 8

| # | requirement | from the brief |
|---|---|---|
| 1 | Runs from the command line — one obvious entry point | "The game runs from the command line" |
| 2 | A title screen is shown before play begins | "It should open on a title screen before play begins" |
| 3 | World data lives in YAML file(s) | "lives in YAML data files" |
| 4 | YAML defines rooms **and** the connections between them | "rooms … and the connections between them" |
| 5 | YAML defines items | "items" |
| 6 | YAML defines NPCs | "NPCs" |
| 7 | YAML defines monsters | "monsters" |
| 8 | World content is not hardcoded in Python | "kept separate from the engine code, so that the world can be extended without editing Python" |

## Command surface — 15

| # | requirement | from the brief |
|---|---|---|
| 9 | `go` with north / south / east / west | "movement (go north/south/east/west)" |
| 10 | `take` | "inventory handling (take, …)" |
| 11 | `drop` | "(…, drop, …)" |
| 12 | `use` | "(…, use, …)" |
| 13 | `examine` | "(…, examine, …)" |
| 14 | `equip` | "(…, equip)" |
| 15 | `talk to` | "conversation (talk to)" |
| 16 | `attack` | "combat (attack, flee)" |
| 17 | `flee` | "combat (attack, flee)" |
| 18 | `look` | "look, status, help, and quit" |
| 19 | `status` | "look, status, help, and quit" |
| 20 | `help` | "look, status, help, and quit" |
| 21 | `quit` | "look, status, help, and quit" |
| 22 | `help` output lists the real command vocabulary | "Help should tell a new player what they can actually type" |
| 23 | `status` shows health, equipment **and** location | "status should show health, equipment, and location at a glance" |

## Combat — 16

| # | requirement | from the brief |
|---|---|---|
| 24 | Turn-based combat loop | "Combat is turn-based" |
| 25 | Player has health **and** attack power | "The player has health and attack power" |
| 26 | A weapon changes combat stats once equipped | "weapons … change those stats once equipped" |
| 27 | Armour changes combat stats once equipped | "armour found in the world change those stats once equipped" |
| 28 | A healing item is usable mid-combat | "healing items can be used in the middle of a fight" |
| 29 | Three regular monsters | "Three regular monsters" |
| 30 | Monsters are placed to guard specific rooms | "guard specific rooms" |
| 31 | Monsters fight back | "and fight back" |
| 32 | Monsters have distinct behaviours, not one shared script | "each with its own behaviour rather than a shared script" |
| 33 | A final boss exists | "The final boss" |
| 34 | The boss has two phases | "has two phases" |
| 35 | The boss has a weakness to one specific item | "a weakness to one special item" |
| 36 | The weakness item is placed in the world, not held at start | "hidden somewhere in the world" |
| 37 | A win path exists on defeating the boss | "beating it wins the game" |
| 38 | Death produces a defeat screen | "dying ends the run with an honest defeat screen" |
| 39 | The defeat screen offers a restart | "that offers a restart" |

## State and content — 14

| # | requirement | from the brief |
|---|---|---|
| 40 | Player state tracked: location, stats, equipment, inventory | "location, stats, equipment, inventory" |
| 41 | World state tracked: room changes, NPC dialogue progression, defeated monsters | "room changes, NPC dialogue progression, and which monsters have already been defeated" |
| 42 | Full state saves to JSON | "support saving … all of it to JSON" |
| 43 | State loads from JSON | "saving and loading all of it to JSON" |
| 44 | At least 8 rooms | "at least eight rooms" |
| 45 | At least 5 items | "and five items" |
| 46 | A weapon among the items | "among them a weapon" |
| 47 | A piece of armour among the items | "a piece of armour" |
| 48 | A healing item among the items | "a healing item" |
| 49 | The boss-weakness item among the items | "and the boss-weakness item" |
| 50 | Two NPCs | "plus two NPCs" |
| 51 | NPC dialogue branches | "whose branching dialogue" |
| 52 | NPC dialogue hints at the boss weakness | "hints at what the boss is vulnerable to" |
| 53 | Combat is narrated | "narrates combat clearly" |

---

## Reporting

Report the tally as `met / 53` with the **unmet items listed by number**. The
unmet list is the useful artifact — across arms it shows which parts of a brief
models systematically drop, which no single conformance score can.

Item 37 deserves a specific note. In the 2026-07-27 panel gpt-oss shipped a
fully-written defeat path and **no win condition anywhere in the tree** — a grep
for congratul/won/victor returned nothing. That asymmetry is worth watching for
across this batch; it is checked here as presence (does a win path exist in code)
and separately as delivery under §3.1.
