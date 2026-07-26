# Archived runs

Parked missions kept for possible resumption, with their `.agent/` state
intact. Both were 7h `game_challenge_boss` runs with a quality top phase,
parked at the wall-clock cap rather than completed.

| run | model | final self-report | blind panel (3 Opus judges) |
|---|---|---|---|
| `step37_boss` | step-3.7-flash 196B MoE | 6/6 structural, 5/8 functional | **29.0 / 50** |
| `mistral_boss2` | mistral-medium 128B dense | 7/8 structural, 8/26 functional | **21.7 / 50** |

## Resume

```bash
uv run ouroboros.py start --working-dir dev/archived_runs/<run> --max-wall-clock 7h
```

Both are `paused` with budget exhausted, so a resume continues from the parked
state. Point `active_config.txt` at the matching model first — resuming
step37's mission on a different model is a new experiment, not a continuation.

## Read the caveats before comparing these

`dev/BOSS_COMPARISON_PROVENANCE.md` — the two runs did **not** share a
goal-derivation prompt (the count-anchor removal landed between them), so
their goal COUNTS are not comparable. The panel scores are, because judges
play the artifact and never see the ledger.

## What the panel found (both artifacts)

Neither game is winnable, and in both the decisive defect was a **cross-module
seam bug** — the fourth consecutive round where that held:

- `step37_boss`: combat engine checks `shadow_lord` / `crystal_shard`; the
  world defines `shadow_lich` / `crystal_of_dawn`. Weakness and victory are
  both dead code. `describe_room` reads static YAML instead of `room_states`.
- `mistral_boss2`: `combat.py` never imports `EquipmentSlot` which it uses on
  four lines — **every attack is a fatal NameError**, putting the entire
  combat half of the spec out of reach. `equipped` is written with string keys
  and read with enum keys in seven places.

`mistral_boss2` also self-reported "Player can equip weapons and armor which
modify combat stats" as COMPLETE with an empty `verification_evidence` field;
all three judges found armor cannot be equipped by any command. Treat goal
status in these files as a claim, not a measurement.

## Note on traces

`.agent/traces/` is gitignored (8.3MB for one run). Perf figures come from
`uv run ouroboros.py trace --working-dir <run> --format summary`, which needs
the traces — so analysis must run on the local copy, not a fresh clone.
