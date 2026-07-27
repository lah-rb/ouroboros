# Blind panel — tier comparison, 2026-07-27

One Opus judge, 4 arms, randomized labels, play-don't-read protocol
(`METHODS.md`). Artifacts from `dev/TIER_RUN_2026-07-27.md`: game_challenge_boss,
top_phase=quality, 2h backstop, `OURO_REASONING_OFF=1`. None of the four
finished; all were stopped at the backstop.

## Result

| # | arm | score | model | run/surv | cover | depth | robust | craft |
|---|---|---|---|---|---|---|---|---|
| 1 | gamma | **41/50** | **gemma-4-31b** | 9 | 8 | **10** | 8 | 6 |
| 2 | delta | 36/50 | gpt-oss-120b-a5 | 9 | 6 | 8 | 9 | 4 |
| 3 | beta | 32/50 | laguna-S-2.1 | 9 | 3 | 4 | 9 | 7 |
| 4 | alpha | 21/50 | devstral-2-small-24b | 5 | 4 | 3 | 5 | 4 |

**Only gemma is completable.** The judge won it: 8 rooms, four monsters
including the boss, and the NPC hint chain is load-bearing rather than
decorative — with the Sun Stone the boss takes 40/turn, without it 10 against
200 HP while dealing 25, i.e. mathematically unwinnable without following the
hints.

## The goal counters inverted again

| model | goal ratio | rank by ratio | rank by panel |
|---|---|---|---|
| gpt-oss | 24/29 = 83% | 1 | **2** |
| devstral-2 | 19/27 = 70% | 2 | **4** |
| gemma | 17/36 = 47% | 3 | **1** |
| laguna | 9/39 = 23% | 4 | **3** |

Every position moved. The ratio leader placed second and the runner-up placed
last. This is the fourth round in which counters have misranked against play
(METHODS §1) — treat them as progress telemetry, never as the instrument.

## Decisive defect per arm

**gemma (41)** — nothing blocked play. Top defect is a partial save: 135 bytes,
player-state only, so items duplicate on reload (equipped sword *and* back on
the floor). Defeated monsters persist but room inventories do not. Also: one
boss phase not two (`models.py:29 phase: int = 1` is the only occurrence of
"phase" in the tree — field present, advancement logic absent), no restart after
death, and monsters do not actually guard (the judge walked past two, which is
how the HP for the win survived).

**gpt-oss (36)** — **no win condition exists anywhere.** The judge killed the
two-phase boss and play simply continued: `The Dark Overlord collapses.` then
`Threats: none`. A grep for congratul/won/victor over the whole tree returns
nothing while the defeat path is fully written. The losing branch shipped; the
winning branch was never written. Also no weapon or armour exists in the world
at all, so `equip` is dead and combat is fixed at 2 damage/turn. This arm has
the only *observed* two-phase boss in the panel.

**laguna (32)** — **the seam bug, and the most instructive result of the set.**
The judge: *"Beta reads like the winner — the best prose, the best help text,
the best refusals, a slot-based equipment model, a real key puzzle, and a
fully-populated YAML naming a boss called Malyster with a mirror-based weakness
and two NPCs whose dialogue correctly foreshadows it. A static review would rank
it first or second. In play it is a walking simulator: not one monster, not one
NPC, no combat, no boss, no win, no death."*

The cause is five missing lines. `loader.py` fills per-room `npcs`/`monsters`
lists from per-room YAML keys that **no room defines** — placement is carried on
each entity as `room_id`. The loader performs exactly that back-link, correctly,
**for items only**; the equivalent loop for NPCs and monsters is absent. So
items appear and everything that makes it a game is orphaned in
`world.npcs`/`world.monsters` forever. The author wrote the pattern once and did
not repeat it.

**devstral (21)** — **severed world graph.** Rooms 5-8 including the Boss Chamber
form an island with no edge from the start component, stranding the second NPC,
two of three monsters, and the entire endgame. Compounded by `parser.py`
whitelisting only n/s/e/w while the approach to the boss room is `down`, so the
boss is parser-unreachable even if the graph were repaired. Plus the worst
robustness failure in the panel: **EOF spins forever — 10,921,238 lines in 15
seconds**, which fills a disk when piped or redirected.

## What this says about laguna specifically

Its panel placement (3rd) understates it in a way the transcript makes explicit.
It has the **best engine and best prose** in the set and lost to a single
five-line omission — not a capability ceiling. That is consistent with the
run-level evidence: laguna wrote the most code by volume (~61k) and the richest
prose while completing the fewest goals, having spent 51% of its run inside long
generations with two hitting the token cap.

So the reading is: laguna's ceiling is high and its failures are throughput
(runaway generations eating the run) plus one seam bug of exactly the class the
transfer-shape/typecheck gates exist to catch.

## Protocol notes

- Seam bugs decisive in **2 of 4** arms (laguna's placement-key mismatch;
  devstral's severed graph plus direction whitelist), consistent with every
  prior round.
- gpt-oss's failure is a **different species** — not a mismatch but an
  *absence*: the losing path fully implemented, the winning path never written.
  Worth watching whether that asymmetry recurs.
- **Three READMEs assert working boss fights. Only gemma's holds up.** The
  protocol's rule to treat artifact self-claims as unverified earned its keep.
- Staging caught two leaks before judging: 16 `OUTCOME` sidecars carrying goal
  counts (now in `STRIP_GLOBS`), and an `Ouroboros` string reviewed as a false
  positive (framework name common to all arms, so non-discriminating).

## Confidence

Single judge. Gaps are 5 / 4 / 11 points. The 2nd-3rd gap (gpt-oss 36 vs laguna
32) is the one thin enough that judge-severity drift could flip it — METHODS
records an arm placing 2nd and then a distant 3rd on the SAME artifact across
rounds. 1st is safe at +5 over 2nd and rests on the only completable game.
