# Cache strategy sweep — measure what each model can actually cache

**Opened 2026-07-29**, from the hy3 tier arm. Gates all further tier runs and
re-runs (operator decision, same day).

## Why: the measured cost

hy3-reap-200b ran its tier arm on the full-replay session path. Every PTY turn
re-prefills the entire session, so cost is O(n²) in turns:

    turn:      1      2      3      4      5      6  ...   10
    prompt: 1591 → 2493 → 3038 → 4708 → 5188 → 6073 → 9929 tok
    eval:    8.1s  12.1s  14.9s  24.8s  27.6s  32.4s → 58.8s

The 10th command cost **58.8s of prefill to produce 20 tokens**. Aggregate over
the arm (trace registers, cross-checked against the server log):

| | 32 min | 58 min |
|---|---|---|
| goals complete | 10/37 | 12/37 (+2) |
| prefill share of run wall | 50.0% | **60.8%** |
| cache hit rate | 35.4% | **33.4%** |

Goal throughput fell ~5× between the halves while prefill's share rose 11
points. 907.6s of the run's 973s of prefill at the 32-min mark was PTY session
re-prefill — **93% of all prefill was re-reading tokens the server already had.**

The consequence is not merely slowness. Reaching the two-phase boss needs ~20-25
commands; extrapolating the measured curve that is ~29 minutes of almost pure
re-prefill. Deep play is priced out, and the campaign's recurring defect is
precisely artifacts whose authored world **play cannot reach** (3 of 4 judged so
far). An agent that cannot afford to play deep may be unable to DISCOVER
unreachability.

## Reconciling with the prior A/B (which said full_replay was fine)

`config.py`'s own comment on `session_full_replay` records an earlier A/B
(`dev/archive/state_exp`) measuring it **"wall-clock-neutral on a real workload —
concentrated in deep-session tails (+44% prefill at P90) while the mean is
flat"**, and advises setting it false only "where the model is a plain transformer
running **shallow** sessions".

That is not contradicted by this finding, and the difference matters:

- **Different comparison.** The A/B measured full_replay against **legacy
  save_state** — both of which re-prefill or serialize. The flat alternative is
  **resident**, which that A/B did not include.
- **The workload moved into the tail.** The A/B's own caveat is the prediction:
  cost concentrates in deep sessions. Agent PTY charters ARE deep sessions —
  mean depth 5.3, max 10, with `diagnose_issue` opening the deepest. What was a
  P90 tail in the earlier workload is the common case in this one.

So the earlier conclusion was right about its workload and is being read outside
it. Worth stating plainly, because "+44% at P90, mean flat" and "60.8% of run
wall" describe the same mechanism at two different session depths.

## What the mechanism actually is

`session_manager.session_turn` picks one of three paths, in this precedence:

1. **resident** — `seq 0` already holds static + every prior turn, left live.
   No restore, no re-prefill. Only the new delta prefills. **Flat.**
2. **full_replay** — `load_state(static)` then replay the whole dynamic history.
   Safe, and **quadratic**.
3. **legacy save_state** — the path OPEN_TASKS §4 retires.

Selection is `resident = self._backend._resident_active`, which is
`resident_seq_cache` (a REQUEST) **and** `memory_can_shift()` (the arch's answer).
Per the gate's own comment, `memory_can_shift()` is:

- **true** on SWA models with `swa_full` (+ `kv_unified`), and on can-shift
  hybrids (Qwen3-Next)
- **false** on interleaved-SWA **without** `swa_full` (gpt-oss / OLMo 3 / Gemma
  class), and on pure-recurrent memory

Denial is silent and lands on a different path, which is the §4 wrinkle.

## PRIOR ART — read this before running anything (found 2026-07-29, after the
## first draft of this plan)

`dev/archive/docs/CACHE_STATE.md` already carries a **measured** architecture ×
cache-mode matrix (2026-07-02) and the harness that produced it
(`dev/cache_compat_matrix.{py,sh}`). It changes this plan in three ways and makes
it much cheaper. Its durable findings:

| Model | Arch class | gate | session mode (turn-2/3 fresh tok) |
|---|---|---|---|
| gpt-oss-120b-a5 | SWA MoE | ✅ | resident-live (12/21) |
| gemma-4-31b | SWA dense | ✅ | resident-live (37/46) |
| devstral-2-small-24b | dense (tekken) | ✅ | resident-live (8/16) |
| qwen3-next-coder-80b-a3 | hybrid recurrent (DeltaNet) | ✅ | resident-live (12/21) |
| qwen3.6-27b | **pure recurrent** | ❌ forced off | full-replay (~1.7k/turn) |

1. **"Every non-pure-recurrent architecture supports the FULL resident stack"**,
   needle-verified, bit-identical to legacy at temp 0. The prior is much stronger
   than I assumed.
2. **`resident_seq_cache: true` is measured-safe to set in ANY config** — the gate
   degrades gracefully to exactly the path these 9 are already on. So this sweep
   is not a risky experiment; it is *set the flag, restart, read one line*.
   Worst case is no change.
3. **Resident sessions WINDOW rather than stop.** `_window_resident_seq` drops the
   oldest ~half and shifts the tail down, keeping the static head, "letting a
   session run indefinitely" where the legacy path raised at the context guard.
   My depth-ceiling analysis below was wrong because of this and is corrected.

Also carried forward: **windowing on the hybrid arch is deliberately untested**,
and the matrix **skipped step37 for time** ("expected identical to gpt-oss; run
the probe before trusting") — a guess that the later OPEN_TASKS §4 finding
contradicts.

## The fleet, and PRE-REGISTERED predictions

Written before measuring. Their job is not to be right — it is to make a
surprise legible. `resident_seq_cache` defaults to **False** and
`session_full_replay` to **True**, so an unset config is on the quadratic path.

**9 of 19 configs run full_replay today** — but prior art already resolves three
of them as CORRECT, leaving **6 real candidates**:

| config | status | basis |
|---|---|---|
| qwen3.6-27b | **correctly full_replay** | pure recurrent, measured 2026-07-02 |
| qwen3.6-35b-a3 | **correctly full_replay** | same family, pure recurrent |
| step37-flash-196b-a11 | **correctly full_replay** | step35 arch reports can_shift=False (§4, 2026-07-25) |
| hy3-reap-200b-a21 | candidate | dense, 81 layers — neither FALSE case applies |
| glm-4.7-flash | candidate | glm4 is MLA, not iSWA |
| laguna-xs-2.1 | candidate | sibling laguna-s runs resident with flags on |
| gemma-4-26b-a4b | candidate | sibling gemma-4-31b measured ✅ |
| mistral-medium-3.5-128b | candidate | devstral (same tekken family) measured ✅ |
| qwen3.5-122b-a10 | candidate, unsure | family position between next (✅) and 3.6 (❌) unknown |

**CORRECTED PREDICTION (before any measurement, on prior evidence I had not yet
read):** my first draft predicted `qwen3.6-35b-a3` would report can_shift=**TRUE**
because its `swa_full`/`kv_unified` flags are already correct. The 2026-07-02
matrix measured the Qwen3.6 family as **pure recurrent → gate forces off**. The
flags do not carry the arch. Recording the correction rather than quietly editing
it, because the reason matters: this is prior art, not a result.

That leaves one headline prediction standing: **hy3 can go flat with a one-line
change**, and four candidates with a working same-family sibling.

Flags as they stand, for the six candidates. **For SWA-class models resident
requires `swa_full` + `kv_unified`, and `swa_full` RAISES KV** — so a candidate
needing those flags flipped also needs its KV re-checked against the ceiling
before adoption. That is the real risk in this work, not the resident flag itself.

| candidate | swa_full | kv_unified | needs flags flipped? | KV re-check needed |
|---|---|---|---|---|
| hy3-reap-200b-a21 | false | false | **no** — dense, swa_full is a no-op | no (total KV unchanged) |
| glm-4.7-flash | false | false | unknown — MLA | if swa_full turns out required |
| laguna-xs-2.1 | false | false | **yes** (sibling has both) | **yes** |
| gemma-4-26b-a4b | false | false | **yes** (sibling has both) | **YES — 32 KB/tok class, and its full 262,144 range was just unlocked** |
| mistral-medium-3.5-128b | unset | unset | probably | yes |
| qwen3.5-122b-a10 | unset | unset | unknown | yes |

## Landed already (2026-07-29, server-free)

`memory_can_shift()` is now asked at **every** load, whether or not resident was
requested, and one line reports the effective strategy:

    🧩 session strategy: full_replay (resident_requested=False,
       memory_can_shift=True, n_ctx=32768, n_seq_max=1, n_ctx_seq=32768)
       — resident AVAILABLE but not enabled; this model is paying full
         re-prefill per session turn

Previously the gate ran only when resident was REQUESTED, so hy3's answer did
not exist anywhere. `info["session_strategy"] / ["session_can_shift"] /
["resident_requested"]` expose it programmatically, because *never requested*
and *requested but denied* both leave resident inactive and only the second is a
bug. Unknown is `None`, never collapsed to False.

19 tests, 7 mutations bite (`tests/test_session_strategy_report.py`), including
two source-order guards — the unconditional placement lives in `initialize()`
and cannot be unit-tested without a real model.

## The sweep — EXTEND the existing harness, do not build a new one

`dev/cache_compat_matrix.sh` already does the whole job: per model it writes a
temp config requesting the full resident stack, restarts the server onto it, runs
`cache_compat_matrix.py` (3-turn session + needle recall + snapshot
capture/end/fork/purge, discriminating mode by `freshPrefillTokens`), then deletes
the temp config and restores production. Rows land in
`dev/bakeoff_results/cache_matrix.jsonl`.

**Run it with `ROSTER` set to the six candidates.** Its temp-config trick is
exactly right here — it does not mutate a production config, which matters
because four candidates need `swa_full`/`kv_unified` flipped and those change KV.

Three extensions worth making first, all small:

1. **Depth 3 → 12.** Three turns proves the MODE but not the CURVE. The hy3 cost
   is quadratic, so the interesting numbers are at turn 8-12, and turn-2/3 fresh
   prefill would have looked merely "a bit high". This is the change that makes
   the harness measure what we now care about.
2. **Record the new `session_strategy` / `session_can_shift` /
   `resident_requested` health fields** instead of inferring from
   `residentActive` alone — that is what tells *never requested* from
   *requested and denied*.
3. **Assert a needle PAST the window** on any config where resident engages, since
   windowing now silently drops old turns.

Measurement 1 (the arch answer) is free and needs no probe at all: **launch and
read one log line**, now that `memory_can_shift()` is asked unconditionally.

Existing rows also record `n_ctx_seq` implicitly; make it explicit, because when
it bites it is a silent context amputation rather than a slowdown (OLMo's 65k
became 5,632/seq, 2026-07-22).

### The worst case, in full — and it settles the window question

`diagnose_issue` session `e1d997a98422432b`: **10 turns, 586s, 94.4% prefill.**

| turn | prompt | prefill | generated | prefill per token |
|---|---|---|---|---|
| 1 | 1,499 | 7.1s | 23 | 0.3s |
| 6 | 10,344 | 62.0s | 23 | 2.7s |
| **8** | 12,172 | **75.7s** | **13** | **5.8s** |
| 9 | 13,513 | 87.0s | 413 | 0.2s |
| 10 | 14,273 | **93.2s** | 28 | 3.3s |

553s prefill, 33s decode. **Turn 8 spent 75.7 seconds of prefill to produce 13
tokens.** Nine of the ten turns were single commands of 13-28 tokens; only turn 9
(413 tokens) was a substantive answer.

What it was working on: diagnosing the `look` command. Its conclusion, from the
completion immediately after the session closed — *"The diagnose step confirmed
that the `look` command is correctly wired to `_look()` and routes properly"*.
**9.8 minutes, 94% of it re-reading its own transcript, to establish a non-bug.**
A negative result is a legitimate result; the price is the finding.

**This kills the window-vs-flatness trade for this workload.** Long generations
and deep prompts never coincide:

| | max generated | at prompt |
|---|---|---|
| stateless calls | 8,102 | 4,790 (shallow) |
| session turns overall | 2,989 | 4,790 (shallow) |
| **session turns with prompt >10k** (n=10) | **413** | deep |

At `n_ctx_seq = 16,384`, turn 10's 14,273-token prompt leaves 2,111 for
generation — and the largest generation at any deep prompt in the entire run was
413. **~5× the headroom needed.** Peak `total_ctx` across all 243 calls was
16,066 (49.0% of the window), median 4,562. Halving the window for resident costs
this workload nothing measurable.

Caveat kept honest: 16,066 is within 2% of the halved window, and this arm was
cut short by the wall. What makes that safe rather than marginal is that resident
**windows** past the limit while full_replay **raises** — the failure mode past
16,384 is graceful.

### The hy3 trade, worked

Enabling resident costs hy3 half its window (`32768/2 = 16,384`) and does **not**
touch total KV, so the 0.4 GB of preflight headroom is irrelevant — this is a
window-vs-flatness trade, not a memory blocker:

| | per-turn prefill | depth 10 | behaviour at the window |
|---|---|---|---|
| full_replay (32,768 window) | rises 8.1s → 58.8s | **311s** measured | hard stop ~34 turns |
| resident (16,384 window) | ~926 tok delta ≈ 5.1s | **~51s** predicted | **windows and continues** |

**~6× cheaper, and it does not stop.** My first draft called this "half the depth
ceiling" — wrong: `_window_resident_seq` drops the oldest ~half and shifts the
tail down while keeping the static head, so a resident session runs indefinitely
and loses old turns instead of failing. Full_replay's larger nominal window is
headroom it cannot afford to reach (depth 34 ≈ 46 min). So the flat path wins on
cost AND on graceful degradation. Prediction to be tested, not a result.

The one thing to watch on adoption: windowing drops old turns, so a charter that
depends on turn-1 context in turn-30 would see it disappear. Full_replay never
silently forgets — it just cannot get to turn 30. Worth a needle check past the
window, which `cache_compat_matrix.py` already does per turn.

## Write-back

Mirroring `probe_verified_n_ctx`: a measurement outranks an estimate, and binding
it to the weights means a requant invalidates it.

```yaml
probe_verified_cache:
  strategy: resident          # MEASURED, not requested
  memory_can_shift: true
  session_prefill_flat: true  # (1) the acceptance test
  fork_parity: byte_identical # (2) the correctness bar
  n_ctx_seq: 16384            # (3)
  max_session_depth: 16       # (4)
  probe_verified_weights_bytes: ...
```

## Still open

- **OPEN_TASKS §4** should become three-valued rather than two: *unsafe* /
  *safe-but-quadratic* / *safe-and-flat*. Its "either flag is fine" premise is
  correct on safety and hides an order-of-magnitude cost difference. Validation
  needs the three branches it already describes; the new log line makes the
  landing observable but does not yet refuse a bad one.
- **OPEN_TASKS §11b** is reprioritized, not strengthened: resident already
  solves sessions on shiftable archs. Measure first — if most of the 9 can flip,
  11b shrinks to stateless completions and genuinely-non-shiftable archs.
- **Tier comparability.** The stage spec is "all settings optimized to each
  model's advantage", and 9 of 19 arms are not. glm (34/100, quadratic) and
  gpt-oss (54/100, flat) sit on opposite sides. n=2 and nowhere near causal, but
  a tiering instrument must not measure this by accident.
- hy3's in-flight arm is a **baseline with a known, quantified handicap** — worth
  recording as such so its score is never read as clean.
