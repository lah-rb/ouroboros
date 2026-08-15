# dflash on the 3090/3060: NET POSITIVE, and the M1 prediction holds

*Measured 2026-08-14. Probe: `dev/dflash_cuda_probe.py`. Target
muse-glimmer-30B-kquant-dynamic on the 3090, draft dflash-kquant (1.63 GB),
greedy (temp 0 / top_k 1), `-c 4096`, n_predict 200.*

## Result

| arm | tok/s | vs baseline | acceptance | mean accepted run |
|---|---|---|---|---|
| baseline (no draft) | 26.35 | — | — | — |
| dflash n_max 3, draft on the 3090 | — | — | — | failed to start |
| dflash n_max 3, draft on the 3060 | 29.19 | **+11%** | 0.332 | 1.99 |
| dflash n_max 8, draft on the 3060 | **36.33** | **+38%** | 0.183 | 2.44 |

**The M1's prediction was right.** That write-up closed dflash for Apple silicon
with "the 3.1x on an RTX 5090 is the same physics pointing the other way —
re-run this probe on the 3090/3060 rig before assuming either result". It is
the other way.

The comparison that makes it physics rather than luck is the acceptance rate.
At n_max 8 the M1 measured 0.185 and this rig measures 0.183 — the SAME draft
quality, since it is the same draft model against the same target. The M1 turned
that into **-50%** and this turns it into **+38%**. Nothing about the prediction
changed; only the cost of the verify step did. Speculative decoding trades
bandwidth-bound sequential decode for compute-bound parallel verify, and these
two machines sit on opposite sides of that trade.

Note also that longer drafts help HERE and hurt THERE. On the M1 the default
n_max 3 was the best case and 8 was catastrophic; here 8 beats 3 by a further
25 points despite acceptance falling 0.332 -> 0.183, because a wider verify
batch is nearly free on a GPU with compute to spare.

## The draft goes on the OTHER card

Same-card placement never started:

```
llama_init_from_model: failed to initialize the context: dflash requires
  ctx_other to be set (this warning is normal during memory fitting)
srv load_model: [spec] failed to measure draft model memory
```

`--spec-draft-device CUDA1` puts the draft on the 3060 and works first time.
That is the same lesson as the swarm bench from a different angle: this rig's
second card is the place to put anything that would otherwise contend with the
target, and drafting is exactly such a thing.

## The engagement guard fired on a REAL result, and that is worth recording

The probe marks an arm INERT when it cannot see drafting statistics, because
the M1 run was corrupted by `--spec-type` defaulting to `none` — three arms at
byte-identical tok/s that read as "no speedup" rather than "never engaged".

That guard was carried over with the M1's log pattern (`n_drafted=` /
`n_accept=`). This llama.cpp prints:

```
draft acceptance = 0.18310 (  117 accepted /   639 generated), mean len =  2.44
```

so the pattern matched nothing and both working arms were reported INERT — a
+38% result declared meaningless. The guard was right to exist and wrong in its
string. **A guard keyed to a log message is only as good as that message**, and
a ported guard needs its pattern re-confirmed against a real log on the new
build before its verdict is trusted in either direction.

## Open

* Same-card drafting is unexplored — it fails during llama.cpp's memory
  fitting, not obviously from real exhaustion (muse 18.3 GiB + draft ~1.5 GiB +
  KV should fit 24 GiB). Worth one look if the 3060 is ever wanted for OCR at
  the same time, since these two uses now compete for it.
* n_max above 8 is untested here; the M1's collapse at 16 may not repeat.
* Not yet wired into LLMVP — llama-cpp-python exposes no model-drafting API
  (dev/DFLASH_SD_2026-08-14.md), so this remains a llama-server capability.
