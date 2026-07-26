# Decode ceiling — experimental methods

**Question.** What is the maximum aggregate decode throughput of the batched
engine on this box, and at what concurrency does it saturate?

**Why it exists.** Four swarm config headers carry the line *"Aggregate decode
rises monotonically with stream count (56→89 tok/s at N=1→6; ~200 at N=64)"*.
That ~200 figure has justified swarm sizing decisions, and as of 2026-07-25 it
is **uncited**: no surviving script, no stored results, no commit that reports
it. It predates the 2026-07-21 fan-out campaign (whose own measured numbers are
7.7 tok/s @ N=14 = 107.8 aggregate) and probably originated in a chat session
during the JIT-rework era, so it was never durable. Meanwhile every measurement
we can still point at clusters in the 71–110 band:

| source | measurement |
|---|---|
| `serving_perf_reference.md` §1 | saturates toward **~90** tok/s (N=1→6) |
| commit fbac6c4 | 7.7 tok/s @ N=14 = **107.8** aggregate |
| arm_swarm2 (2026-07-21) | **~71** tok/s effective |
| cf regen sweep (2026-07-25) | **100.5** @ c=16, **101.8** @ c=32 |
| cf regen live | **~112** sustained @ c=16 |

This experiment replaces the anecdote with a repeatable number.

---

## The measurement trap (the reason for two curves)

Two quantities both read like "throughput" and differ by roughly 2x:

```
aggregate_tok_s = total generated tokens / wall-clock of the whole wave
sum_of_rates    = mean(tokens / decodeMs) * N
```

`aggregate_tok_s` is what you actually get. `sum_of_rates` is **not a
throughput at all**: `decodeMs` is the server's decode register, which excludes
prefill *and* queue wait. When N exceeds available seats the surplus requests
queue, but their `decodeMs` still looks fast because it never counted the wait.
Multiply that by N and you manufacture a figure no wall-clock can reproduce.

`3 tok/s × 64 ≈ 192 ≈ "~200"` is exactly the shape of the recalled claim, which
is the leading hypothesis for its origin. **The harness therefore reports both
at every rung**, so the divergence is measured rather than argued.

---

## Design

**Independent variable:** N, concurrent in-flight requests. Ladder
`1,2,4,8,16,32,48,64,96,128`.

**Seats, not restarts.** The server runs
`configs/gpt-oss-120b-a5-decodeceiling.yaml` — identical to the production
`swarm-393k` except `max_concurrent_requests: 128`. Every rung then runs
against one warm server, and *client* concurrency is the only thing that
changes. Rationale:

- The production configs cap at 32, which is why every prior sweep stopped
  there and why N=64 was never testable. `LLAMA_MAX_SEQ` is 256 and
  `n_seq_max = n_working + personas + reasoning_levels`, so 128 working seats
  is legal.
- Idle seats are ~free: `kv_unified` means cells are shared and consumed per
  *actual* token, so 128 × ~300 tokens ≈ 38k of the 393,216-cell pool.
- One server for the whole ladder removes model-reload variance, which
  otherwise swamps the effect being measured.

*Caveat, stated honestly:* allocating 128 seats when only N are used may carry
a small fixed scheduling cost versus a server sized exactly to N. This trades a
possible small constant against removing reload noise entirely; the N=1 rung is
directly comparable to the historical single-stream figures (55–56 tok/s), so
any large constant would show up there.

**Workload — small prompts, 256-token generations.** Deliberate, and the reason
this is *not* the same experiment as the cf-regen sweep:

- The regen sweep used real corpus prompts (2–10k chars) and measured **100–112
  tok/s**, but that regime is prefill-dominated — `serving_perf_reference.md`
  warns *"fan-outs with many mid-size prompts are prefill-dominated"* and
  measures concurrent prefill as roughly serialized.
- With ~20-token prompts, prefill ≈ 0 and wall ≈ decode, so `aggregate_tok_s`
  approximates the pure decode ceiling. **This is the number the config header
  claims**, so it is the number to test.
- Both regimes are real. Quote the prefill-heavy figure for swarm planning and
  this one for the engine ceiling; do not mix them.

**Repeats.** 2 per rung, best-by-aggregate reported (a slow rep indicates
contention, not a lower ceiling), with every rep retained in `results.json`.

**Recorded per rung:** aggregate, per-stream decode, sum-of-rates, mean
prefillMs/decodeMs, p50/p95 latency, error count and samples.

---

## Running it

```bash
# 1. point the server at the 128-seat config and restart
cd llmvp && echo gpt-oss-120b-a5-decodeceiling > active_config.txt
#    (stop/start per your normal server procedure; confirm health poolSize=128)

# 2. run the ladder
python dev/decode_ceiling/bench.py --repeats 2

# 3. graph it
python dev/decode_ceiling/plot.py
```

Outputs `results.json` and `ceiling.png` beside the script.

**Restore afterwards:** put `active_config.txt` back to the production config
(`gpt-oss-120b-a5-swarm-393k`) and restart. The 128-seat config is an
experiment fixture, not a production shape.

---

## Reading the result

- **If `aggregate_tok_s` plateaus in the 90–115 band** and `sum_of_rates`
  climbs toward ~200 at N=64: the claim was the artifact. Correct the four
  config headers to the measured ceiling and record the trap.
- **If `aggregate_tok_s` genuinely reaches ~200 at N=64:** the claim stands,
  the production cap of 32 is leaving ~2x on the table, and swarm sizing should
  be revisited.
- **If rungs above the seat count error or wedge:** that is the admission
  ceiling asserting itself, and is itself the answer to "how far can we push."

Watch the error column at high N regardless — the batched seat-leak on
watchdog cancel (OPEN_TASKS) is a known sharp edge, and a 128-way wave is the
most aggressive thing we have ever pointed at this engine.


---

# RESULT (2026-07-26) — the claim was TRUE and I was wrong about it

**Measured: 202.5 tok/s aggregate at N=64, 3.19 tok/s per stream.** The
recalled figure was "64 streams stable at ~3 tok/sec" ≈ 200 aggregate. Both
numbers, exactly.

Max allocatable seats: **128** — the top of the ladder, so not a discovered
ceiling. Zero errors at every rung, monotonic throughout, 5.41x batching gain,
still climbing where the ladder stopped. Full curve is in
`serving_perf_reference.md` §1 and `results.json`; graph in `ceiling.png`.

## What this document got wrong, and why it matters

**1. There is no `sum_of_rates` artifact here.** The hypothesis above — that
"~200" came from `mean(per-request decode) × N` rather than a true throughput —
is refuted: the two track within 1-2% at EVERY rung. The mechanism is real, but
only when N exceeds available seats, because queued requests are credited a
`decodeMs` that never counted their wait. It was never what produced the
original number. I constructed a tidy explanation for a figure that simply did
not need explaining.

**2. "128 seats were never structurally possible."** Reasoned from the absence
of any config or commit setting `max_concurrent_requests` above 32. Nobody
having configured it is not the same as it not working. 128 allocates and
decodes clean.

**3. "Idle seats are ~free" — this one was right, and more so than expected.**
128 streams × ~84 tokens is 2.7% of a 393,216-cell pool. Seat count is not a
constraint at all; the pool is.

## The methodological lesson

The uncited number was correct and the archaeology was wrong. An undocumented
measurement is not the same as a false one, and "I cannot find where this came
from" does not license "it must be an artifact." The cheap experiment should
have come first — it took 20 minutes and would have saved a long, confident,
incorrect argument.

## Follow-up experiment, on the other axis

This ladder answers "how many seats", which we now know is the wrong question.
The one that matters for sizing: hold N fixed and grow PER-STREAM CONTEXT until
the pool binds, to find the real admission boundary and test whether the swarm
gate's 80%-of-pool rule is right, conservative, or optimistic.
