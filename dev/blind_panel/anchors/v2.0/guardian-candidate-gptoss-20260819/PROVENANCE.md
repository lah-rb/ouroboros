# guardian-candidate-gptoss-20260819 — CANDIDATE, not yet the Guardian

Frozen 2026-08-19 (operator directive) as a potential replacement for
guardian-gptoss-20260803, pending the anchor-refresh decision.

## Provenance
- Source: `tier_20260819-202151/staged/arm01/alpha`, byte-identical copy
  (staged litter — out.txt, tmp_output.txt, the 4/5-failing tests/ — kept
  deliberately: an anchor is CHARGED AS IT SHIPS, same as any candidate).
- Producer: gpt-oss-120b-a5, grinder league, 2h wall, session mission,
  top-phase quality. 121 min, 24/32 goals, 82 cycles, 0 degenerations,
  0 py_fail, 0 junk-target diagnoses.
- Framework: the 2026-08-19 stack — role-not-reply verification (dd7af76),
  explorer gate prompts (6d07d92), refutable untested claims (691fefb),
  §15 sweeps, honest probe transcripts (fbdf5a4), and decisively the
  collapsed-diagnose/uncapped-investigation change (8e1e1cf).

## Why it is a candidate
It beat the sitting Guardian 8-2 in a blind flight (Delivery 3-1,
Character 5-1, no split, no CLOSE flag) and is the first gpt-oss artifact
to WIN its own game under judging — a reproduced two-sided ending — where
the sitting Guardian is UNWINNABLE (no win path in its tree). Same model
family, same wall as the sitting anchor: the delta is the framework.
Record: `dev/blind_panel/records/flight_20260819_guardian_vs_gptoss_rerun.md`.
Longitudinal series vs the sitting Guardian: 2-8 → 4-6 → 3-7 → 8-2.

## Known defects (charged in the flight; carry into any promotion decision)
- id-only parser: refuses the display names it prints (`take Sharp Steel
  Sword` fails; `take steel_sword` works). B7 nearly flipped on this;
  B8 was lost on it.
- help omits examine/status/go/restart (false-passed our own sweep twice).
- Combat modal loop swallows non-combat verbs, incl. a phantom "You save."
- Three uncaught tracebacks: EOF at name prompt, EOF in combat, `load`
  with no save file.
- Crystal-shard boss weakness is flavour text only (no code reads it).
- Regular monsters deal 0 damage vs base defence (only the boss fight is
  real — and it is genuinely good: narrated phase change, real loss).

## Promotion checklist (before this replaces guardian-gptoss-20260803)
1. Floor recalibration first: rerun devstral-2 (floor model) on this same
   framework version and fly it vs the SITTING guardian — if the floor now
   beats the old guardian too, the whole anchor set moves together.
2. One confirmation flight of this candidate vs the sitting guardian with
   sides swapped (it sat A; a B-side result guards position bias).
3. Update stage.py pseudonym/denylist handling if the anchor id scheme
   changes, and LADDER.md citations that reference guardian-gptoss-20260803.
