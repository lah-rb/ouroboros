# FLOOR DISPLACEMENT FLIGHT (second attempt) — post-prompt-fix continuation vs the SITTING Floor

*Dispatched 2026-08-20 from FLIGHT_PROMPT.md verbatim; ONE blind judge
(claude-opus-5), TIER_RUBRIC v2.1. KEY (revealed post-verdict): A =
dev/blind_panel/anchors/v2.0/floor-devstral-20260803/alpha (SITTING
FLOOR); B = tier_20260820-075618/staged-continuation/alpha (CANDIDATE —
devstral-2-small-24b, collapsed-diagnose framework + c0417a8
module_statement prompt fix; 2h arm + 100min operator-directed
continuation; 16/31 gate-verified goals). Candidate at B (rotation
honored — the first floor flight had the candidate at A). Scans clean.

VERDICT: Delivery A(Floor) 3-1 · Character 3-3 (no direction) ·
**OVERALL: B (CANDIDATE)** — SELF-FLAG: CLOSE, tension explicitly
recorded (the overall runs against Panel A's direction; Character has
no direction to split against). The judge rested the overall on the two
facts the rubric elevates: completability — the candidate was **WON**
(The Dark Sovereign killed at 5 HP; the anchor is UNWINNABLE, its boss
in an unreachable component) — and placement — candidate 8/8 rooms
reachable, every entity placed, vs the anchor failing BOTH placement
forms at once (5 of 9 rooms orphaned; boss, weakness item, 3 of 4 NPCs
stranded; all conversation dead).

FIRST WON-CLASS DEVSTRAL ARTIFACT in the campaign — with the heavy
caveat the judge charges in full: the win required source-read id
knowledge a player cannot obtain (snake_case ids never printed,
`rusted_broadsword` vs "Rusty Broadsword"), the win produces no
terminal state, and `save` hard-crashes on a NameError (the third
import seam, still unfixed at park). "A source-blind player is stopped
on turn one."

SAME-DAY ARC, one model: unenterable (1-9 vs guardian, morning) →
prompt fix c0417a8 → unstuck on the first lap → 10/31 → 16/31 honest →
**beats the sitting floor** (whose ~29 goals rode the old framework's
open false-pass channel). The pair of floor flights now measures the
prompting gap directly: same model, same day, 2-8 without the field in
the schema block, winner-with-CLOSE once it landed.

ANCHOR CONSEQUENCE: held for the operator — a CLOSE overall with
Delivery 3-1 AGAINST the candidate is a displacement case but not a
sweep; the judge names exactly what would flip it (weighting
source-blind playability above delivered-and-reachable scope).

Judge's verbatim record follows.*

---
# FLIGHT RECORD — TIER_RUBRIC v2.1, two-panel comparative

Both artifacts played interactively via `/Users/lah-rb/Repos/ouroboros/.venv/bin/python main.py` from scratch copies. Originals untouched.

---

## ARTIFACT A — record

**PREMISE LINE.** A generic cave crawl with no name of its own: you walk in from an entrance, pick up a rusty sword, and clear a hallway, a treasure room and an armory of two monsters. It authors a Dark Lord, a Legendary Sword and a Wise Man behind a door that does not exist. *"The air is thick with dark energy. A massive boss stands before you."* — the Boss Room description, which no player can ever read.

**COMPLETABILITY: UNWINNABLE.** Not "hard" — structurally impossible. The boss lives in `boss_room`, in a graph component nothing links into.

**ROOM GRAPH & PLACEMENT AUDIT — the decisive defect.**
9 rooms authored, **4 reachable** from `entrance`: entrance → hallway → {treasure_room, armory}. Two distinct failure forms, both present:
- **Disconnected component:** `secret_room → dungeon ⇄ boss_room` is a closed 3-room island. Nothing in the reachable set exits into it.
- **One-way sinks:** `garden` declares `south: armory` and `library` declares `north: hallway`, but neither armory nor hallway declares a reciprocal exit. Authored, linked outward, reachable from nowhere.

Authored-but-unreachable entities: **boss `Dark Lord`**, **weakness item `legendary_sword`**, `healing_herbs`, `book_of_knowledge`, and **3 of 4 NPCs** (`wise_man`, `gardener`, `librarian`). The one NPC that *is* placed reachably (`guard`, in both entrance and hallway) is unreachable for a different reason — see below.

**SECOND SEAM BUG — conversation is 100% dead.** `engine.py:291-294`: an `else: current_index += 1; continue` intended for the dialogue-scan loop lands inside `for npc_id in self.current_room.npcs`. Every dialogue line whose condition is the literal token `condition: none` — which is the *opening line of all four NPCs in `world.yaml`* — skips the NPC entirely and falls through to the not-found message. A token the data emits (`none`) that the engine has no case for:

```
> talk to guard          [standing in Entrance, npcs: [guard]]
There is no guard here.
```
Verified by forcing the first condition to `talked_once`, which prints correctly. **Conversation, a named element of the brief, fails in every room for every NPC.** Attribution: model-innate.

**CONFORMANCE: 40/47.** Unmet: **3** (`go` absent from parser and engine — only bare directions), **23** (two regular monsters, not three), **26** (all three `behaviour: aggressive`, field never read by any code), **28** (boss phases in neither code nor data), **29** (no weakness relation anywhere — only flavour text on the item description), **33** (no restart offered), **38** (9 rooms authored, 4 connected from start).
**Verdict: SIGNIFICANTLY-DEVIATED — trigger = CORE-LOOP** (checklist rule 1's own named example: "a boss whose two phases appear in neither code nor data"), not count. Item 29 recorded as borderline.

**STATE INTEGRITY.** No `save`/`load` command exists — `save_game`/`load_game` are fully written in `engine.py:380-405` and dispatched from nowhere. No round-trip is possible in play. Process relaunch resets cleanly (`load_world()` re-reads the YAML; nothing is written to disk). One real corruption, observed: **equipping is not idempotent and never removes the prior bonus.**

```
> equip Magic Amulet     (×3, plus a prior Rusty Sword)
Attack: 34               [base 5]
```

**ROBUSTNESS (10 probes)**

| probe | result |
|---|---|
| unknown command `xyzzy` | clean refusal |
| empty input | clean (skipped, re-prompt) |
| invalid direction `up` | clean: "You can't go that way." |
| verb, no target (`take`,`use`,`attack`,`talk to`) | clean but blanket: "I don't understand that command." |
| nonexistent target | clean: "There is no X here." |
| attack with nothing present | clean refusal |
| use a non-usable item | clean: "You can't use the Rusty Sword." |
| shell metachars `north ; rm -rf /` | **silent misinterpretation** (benign — extra tokens dropped, moves north) |
| 67-char garbage | clean refusal |
| **EOF / Ctrl-D** | **uncaught `EOFError` traceback** |

Worst impact: EOF traceback. **No reachable crash from any in-game command.**

**MODIFICATION PROBE.** Added a 10th room (`crypt`) with a reciprocal exit from `armory`, and changed `sword` attack 5→9. **`world.yaml` only; zero Python touched; both worked first try** (Attack 5→14 on equip, Crypt reachable). Nothing broke. Caveat: the data layer is only half the story — the shipped graph is itself broken, and adding an NPC would be pointless since `talk to` is dead.

**FURTHEST POINT REACHED.** Treasure Room, Dragon defeated, all 4 reachable rooms cleared, all 4 reachable items held. **Reading the source was NOT required to reach it** — A prints Exits, Items and Monsters on every room render and accepts the display names it prints. Source reading only *explained* the two failures I had already hit in play.

---

## ARTIFACT B — record

**PREMISE LINE.** A gothic castle crawl with a real motif: light against darkness, engraved on a silver locket, whispered by an Elder Librarian, and worn as a name by The Dark Sovereign. Eight rooms, all connected, all reachable, ending in a boss chamber you can actually walk into and kill. *"A moonlit courtyard surrounded by crumbling stone walls. The faint sound of dripping water echoes in the distance. Statues of forgotten nobles stand silent guard."*

**COMPLETABILITY: WON.** I defeated The Dark Sovereign at 5 HP. **But the win produces no terminal state whatsoever** — no victory screen, no game end, the prompt just returns:

```
> You defeated the The Dark Sovereign!
> Health: 5, Attack: 5, Defense: 2
> There's nothing to attack here.
```
The boss chamber still describes "a towering figure cloaked in shadow" afterwards. Death is equally non-terminal — I played on at **Health: -1**.

**ROOM GRAPH & PLACEMENT AUDIT — clean.** 8 rooms authored, **8 reachable**, one component, all links reciprocal. Every item (5/5), every monster (5/5), and both NPCs are placed in a room. **No orphan component, no unplaced entity.** This is the one campaign-standard defect B does not have.

**SEAM BUG 1 — the weakness never fires.** `combat.py:54` gates the silver-locket branch on `self.monster.name.lower() == "boss"`. The monster's `name` is `"The Dark Sovereign"`; `"boss"` is its **id**. A display name compared against an identifier — the canonical seam. Doubly dead: the check sits *after* `monster_turn()` returns, so a losing round returns "You have been defeated." before the locket is consulted. The locket, the inscription, the librarian's hint and the Secret Garden are a complete four-part arc killed by one string comparison. Attribution: model-innate.

**SEAM BUG 2 — `save` hard-crashes the game.** `engine.py:198` calls `save_game(...)`; `engine.py:1-6` never imports it from `saver`.

```
> save
NameError: name 'save_game' is not defined
```
Process dies, all progress lost. This is an advertised help-text command. Attribution: model-innate.

**SEAM BUG 3 — the game refuses every name it prints.** All noun verbs (`take`/`drop`/`use`/`equip`/`examine`/`talk`) compare against the bare **snake_case id**, never the display name.

```
> take Rusty Broadsword
There is no rusty broadsword here.
> take rusted_broadsword
You picked up Rusty Broadsword.
> equip Rusty Broadsword          [the name inventory just printed]
You don't have rusty broadsword.
> talk to knight                  [the brief's literal form]
There is no to knight here to talk to.
```
Note `rusted_` vs "Rusty" — not even a case transform. `look` lists no items, no exits, no monsters, no NPCs; the room description is all you get; `examine` does not search monsters. **There is no in-game affordance anywhere that reveals an id.** I obtained every item by reading `world.yaml` — knowledge a player has no way to obtain.

**SEAM BUG 4 — the documented movement verb does not work.** `parse_command` returns a direction only for a bare direction word; `go`/`move`/`walk` put it in `target`, and `handle_command` then reports `"Please specify a direction to move."` So `move north` — the first line of B's own help — always fails, and `north` (undocumented) is the only form that works.

**CONFORMANCE: 37/47.** Unmet: **11** (`flee` exists only in the README — absent from the entire tree), **13** (`status` likewise absent; only `stats`), **16** (help teaches `move [direction]`, which cannot work, omits the bare-direction form that does, and advertises `save`, which crashes), **17** (`stats` shows health/attack/defense only — no equipment, no location), **22** (`fight()` resolves the entire battle atomically; there is no mid-combat decision point at which a healing item could be used), **26** (all `behavior: aggressive`, never read), **28** (boss phases in neither code nor data), **31** (grep for congratul/won/victor returns nothing — killing the boss is mechanically identical to killing a skeleton), **32** (death produces one string and play continues at negative health — no terminal state), **47** (combat narrates a single outcome line; no damage numbers, no blow-by-blow, no monster announcement).
**Verdict: SIGNIFICANTLY-DEVIATED — BOTH triggers fire**: count (10 unmet ≥ 10) *and* core-loop (boss phases absent; no terminal states in either direction).

**STATE INTEGRITY.** Worst of the two. `save` crashes the process. `saver.load_game()` reads the JSON and then `return None` under the comment *"For now, we'll return None to indicate the function is not yet fully implemented"* — so `load` always says "No saved game found" and `main.py`'s `continue` branch is permanently dead code (its prompt still names it). `new` does **not** rebuild the world — `create_world()` runs once in `__init__`, so a second `new` inherits the already-dead monsters and already-looted rooms:

```
> new
> attack
There's nothing to attack here.        [Skeleton Guard still dead from the prior game]
```
Additional: the healing potion is **never consumed** (`use` ×3 → Health 75, still in inventory — the game is unlosable once you hold it); `take` never removes the item from the room; `drop` appends a duplicate id.

**ROBUSTNESS (10 probes)**

| probe | result |
|---|---|
| unknown command `xyzzy` | clean refusal |
| empty input | clean |
| invalid direction `up` | clean: "You can't go that way." |
| verb, no target | clean **and specific**: "Take what?" / "Talk to whom?" |
| nonexistent target | clean |
| attack with nothing present | clean refusal |
| use a non-usable item | clean: "You use X, but nothing happens." |
| shell metachars | clean (trailing junk dropped) |
| 77-char garbage | clean refusal |
| **`save`** | **`NameError` — process dies, progress lost** |
| **EOF / Ctrl-D** | **uncaught `EOFError` traceback** |

Worst impact: reachable hard crash on an advertised command.

**SHIPPED TESTS (interaction-attributed, charged).** `tests/` ships 6 tests, **4 failing**. Their own assertions encode the broken syntax: `engine.handle_command(parse_command("move north"))` — the test suite reproduces defect 4 and was evidently never run green.

**MODIFICATION PROBE.** Added a 9th room (`crypt`) with a reciprocal exit from `secret_garden`, and changed `rusted_broadsword` attack 8→14. **`world.yaml` only; zero Python touched; both worked first try.** Nothing broke.

**FURTHEST POINT REACHED.** **Won the game.** **Reading the source WAS required** — I state this explicitly: I could not pick up a single item without `world.yaml` for the ids, could not see any monster (they are invisible to `look` and to `examine`), and found the working movement syntax only by accident against the help text. A source-blind player is stopped on turn one, unarmed, and dies to the second monster in a game that then refuses to end.

---

## THE TEN AXES

### PANEL A — DELIVERY

| axis | choice | justification |
|---|---|---|
| **A1 working surface** | **A** | A's advertised surface works except `talk to`; B's advertised `move [direction]` never works, `save` kills the process, and six more verbs are gated behind ids the game never prints. |
| **A2 state integrity** | **A** | A relaunches into a faithfully rebuilt world and writes nothing; B's `save` NameErrors, its `load` is a declared stub, and `new` reuses an already-looted, corpse-strewn world. (A's equip-stacking to Attack 34 is charged against it and still doesn't close the gap.) |
| **A3 robustness** | **A** | Both traceback on EOF, but B adds a reachable hard crash on a help-advertised command; A has no in-game input that crashes it. |
| **A4 delivered scope** | **B** | 8/8 rooms, 5/5 items, 5/5 monsters and 2/2 NPCs reachable and a boss I actually killed, against A's 4 of 9 rooms, 2 fights, zero working NPCs and no ending. |

**Delivery: A 3–1**

### PANEL B — CHARACTER

| axis | choice | justification |
|---|---|---|
| **B5 ambition** | **B** | B reached for the brief's central chain end-to-end — a hidden locket, an NPC hint that names it, a secret room to hide it in, and an actual double-damage branch in `combat.py` — plus a separate CombatEngine, a saver, a title gate and tests; A never attempted the weakness relation at all and left it as one sentence of item flavour text. *(Closest axis: A reached deeper in exactly one place, a six-condition dialogue scanner, and authored 9 rooms/4 NPCs/7 items against B's exact-minimum 8/2/5.)* |
| **B6 imagination** | **B** | "Gothic Castle Adventure" has a place, a register and a motif that runs from the locket's inscription *"Light shall banish the darkness"* through the Elder Librarian to The Dark Sovereign; A is titled "Text Adventure Game" and its rooms are called Entrance, Hallway, Armory. |
| **B7 experience (UX)** | **A** | I only won B by reading `world.yaml` for ids the game never prints and which differ from the names it does print — a player is stopped on turn one by `take Rusty Broadsword` → "There is no rusty broadsword here", in a room that lists no exits, no items, and does not mention the skeleton standing in it; A announces exits, items and monsters, and reports damage and remaining HP both ways, so its loop is legible even though it dead-ends in four rooms. |
| **B8 craft (UI)** | **A** | Input tolerance and help accuracy are this axis's named terms and A wins both outright — it accepts the names it prints, and its help describes what actually works, while B's help teaches a movement verb that cannot work and a `save` that crashes; A's labelled Health/Attack/Defense/Location/Inventory/Weapon/Armor block also beats a one-line `stats` with no location, no equipment, and numbers that never move when you equip anything. B's better error phrasing and the doubled title banner ("Game started!" then a second "Type 'new' to start a new game") do not offset that. |
| **B9 workability** | **B** | Both modification probes were data-only and clean, so this turns on logic organisation: B has real seams — `models` / `world` / `game_state` / `combat` / `parser` / `saver` / `engine` with a genuine GameState API and a test scaffold — against A's single 433-line god-class whose decisive bug is a direct product of its 100-line nested dialogue block. |
| **B10 documentation** | **A** | Same boilerplate skeleton in both, but B's README contradicts play on four counts (`go` movement, `flee`, `status`, save/load) and then delegates the vocabulary to a help text that is itself wrong, while A's README enumerates a command list that is 12/15 accurate — and neither document mentions the snake_case ids without which B cannot be played at all. |

**Character: 3–3 (no direction)**

---

## TALLIES AND OVERALL

> **Delivery: A 3–1 · Character: 3–3 · Overall: B**

**SELF-FLAG: CLOSE.**

**NOT A FORMAL PANEL SPLIT** — Panel B is evenly divided at 3–3 and therefore has no direction to disagree with Panel A. But I am flagging the tension explicitly, because my overall runs *against* Panel A's direction and the operator should see that plainly rather than infer it.

**Why B overall.** The two facts the rubric elevates above every axis both point the same way. Completability: A is **UNWINNABLE** — no code path in the shipped tree can bring a player to its boss — while B is **WON**. Placement: A fails the campaign's most common decisive defect in both of its forms at once, orphaning five of nine rooms, its boss, its weakness item and three of its four NPCs, and then kills conversation in every room it did connect; B's graph and placement are clean, 8 of 8. Both artifacts carry a decisive cross-module seam bug, but they sit at different depths — B's are a display-name-vs-id comparison and a missing `import`, surface repairs that leave a coherent castle intact behind them; A's are a room graph that was never connected and a `continue` that escapes its loop, which leave a four-room fragment with no boss, no conversation and no ending. B also has a premise, a voice, a designed weakness arc, and modules a person can work in.

**Why this is close, and what would flip it.** A judge who weights *source-blind playability* above delivered-and-reachable scope should call this A, and the case is strong: more of what A offers works, nothing a player types crashes it, and a player with no access to the tree gets a small legible arc out of A while getting nothing at all out of B — unarmed, unable to lift a single object, dying to the second monster in a game that then refuses to end. I charged exactly that at B7, B8 and B10 and it is why Panel B could not resolve. I did not let it decide the overall because A4 already charged the same inaccessibility, and because "the boss is not in the game" is a deeper failure than "the boss has an unguessable name."
