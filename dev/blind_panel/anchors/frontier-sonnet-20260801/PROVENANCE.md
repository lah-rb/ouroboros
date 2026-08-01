# FRONTIER anchor — provenance

**Role:** FRONTIER per `TIER_RUBRIC_v2.md` §5 — OUT-OF-BAND reference. Flights
against this artifact produce an AXIS SCORECARD only; they never move the
ladder and never gate a tier.

| | |
|---|---|
| generated | 2026-08-01 |
| model | Claude Sonnet 5 (`sonnet` alias, chat-session subagent) |
| brief | `missions/game_challenge_tier.yaml` objective, **v1.2 wording, verbatim** — the exact prose the 18-arm sweep received; no rubric, no checklist shown |
| protocol | SINGLE PASS: Write tool only · each file exactly once · no execution, no testing, no reading back, no revision · files written DIRECTLY to disk (never through batch extraction — no §20 exposure) |
| pacing | one Write per response (technical amendment after attempt 1; see below) |
| shape | 15 files · 9 py (a `game/` package + `main.py`) · 4 data YAMLs · README + requirements · 1,641 lines |
| tokens | 116,295 subagent tokens, 15 tool calls, ~13 min |

## Attempt history

**Attempt 1 (voided, zero files):** the agent exceeded the 64k per-response
output ceiling before its first Write completed (planning plus batched Write
payloads in one response) and was terminated. Nothing reached disk; the
directory was wiped. **Attempt 2** added the one-Write-per-response pacing
constraint — a transport accommodation, not a content change: still one pass,
no revision, no verification. This is the artifact.

## Intake (operator-side integrity, NOT a judgment)

All 9 py files parse; all 4 YAMLs load; README complete with EVEN fence parity
— the first non-truncated README of the campaign, precisely because it skipped
the extraction path; boots to a title screen on `python3 main.py`. The v2
smoke and facts pass happen when this anchor is first flown.

## Caveats stamped on every FRONTIER verdict (v2 §5)

1. **Family bias** — flight judges are Opus; same-family judges favour family
   outputs and the effect operates through style recognition, so blinding does
   not remove it. Direction known: tailwind for this artifact.
2. **Cross-pipeline** — a one-shot never runs the decomposed write path where
   the local field's dominant failure class (seams) is born. This measures
   distance-to-frontier, not our integration.
3. **It might not be out of reach** — a top-band artifact beating this is a
   result, not a broken anchor.

## Epoch note

Generated against the **v1.2 brief**, pre-epoch, by operator direction
("Lets ship frontier… have sonnet 5 produce the frontier artifact to judge
against", 2026-08-01) — its first use is judging against the v1.2 field
(laguna Q6_K comparison et al.). When epoch v2.0 opens with a changed brief,
this anchor REGENERATES under the new brief per the epoch rule; this tree
stays frozen as the v1.2-brief frontier reference.
