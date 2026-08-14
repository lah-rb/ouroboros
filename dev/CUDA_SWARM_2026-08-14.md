# Context swarm on the 3090/3060 rig: the device boundary is everything

*Measured 2026-08-14 on the NVIDIA box, first bench after the port. Probes:
`dev/cuda_swarm_probe.py`, `dev/cuda_batch_scaling.py`. llama.cpp built from
source with CUDA (arch 86), Qwen2.5-7B-Q4_K_M as the controlled load generator
and muse-glimmer-30B-kquant-dynamic as the production model. Greedy
(temp 0 / top_k 1), `cache_prompt: false`.*

## The prediction being tested

On the M1 Ultra, two tenants sharing the one device serialized in a strict
order (`PARALLEL_LANES_2026-08-13.md` §7d), and the hypothesis fitted to that
ordering was **unified memory bandwidth as the shared bottleneck** — two
decoders saturate it and contend, a compute-bound tenant fills the gaps they
leave. No bandwidth counters were ever read, so it stayed a hypothesis.

This rig has two separate bandwidth domains. That makes the hypothesis
falsifiable: put the same two decoders on different GPUs and serialization
should collapse.

Serialization is defined exactly as on the M1, so the numbers compare:

    S = (t_concurrent - max(ta, tb)) / (ta + tb - max(ta, tb))

## Result: it collapses to zero

| arm | serialization | aggregate speedup |
|---|---|---|
| two 7B servers, BOTH on the 3090 | **1.043** | 0.98x |
| 7B on the 3090, 7B on the 3060 | **0.0011** | 1.48x |

Same model, same prompts, same protocol, same process shape. The only variable
is which device each server sits on.

The per-tenant throughput is what makes it unambiguous:

```
same GPU    solo 133.5 / 140.1  ->  concurrent 66.6 / 66.5   (each HALVED)
cross GPU   solo 140.5 /  68.0  ->  concurrent 142.8 / 67.9  (both UNCHANGED)
```

Cross-GPU, the concurrent wall (15.17 s) equals the slower leg's solo wall
(15.16 s). The faster tenant ran entirely for free. Neither felt the other.

**The M1 hypothesis is confirmed, and sharpened.** It was never about process
topology — the M1's own controlled re-run had already put in-process vs
cross-process at Δ0.044. It is about whether the two tenants share a memory
bus. Give them their own and contention disappears completely.

## And CUDA is WORSE than Metal at sharing one device

The M1 got 0.575–0.622 for text×text on one device — partial overlap. Two
processes on one CUDA device get **1.043**: none at all, and marginally worse
than running them back to back. CUDA time-slices between processes (no MPS
daemon here), so the second process buys nothing and costs context-switching.

This inverts the porting instinct. On the M1, "run the second model in its own
process" was free. Here it is actively wrong.

## What DOES scale on one device: batching inside one context

One process, one context, N sequence slots, 7B on the 3090:

| streams | aggregate tok/s | per-stream | speedup |
|---|---|---|---|
| 1 | 140.9 | 142.0 | 1.00x |
| 2 | 254.4 | 128.7 | 1.81x |
| 4 | 346.9 | 88.5 | 2.46x |
| 8 | 386.3 | 49.0 | 2.74x |
| 16 | **1025.0** | 66.8 | **7.28x** |

The 16-stream point looked like an artifact — it finished in less wall time
(2.0 s) than the 8-stream run (2.65 s) while doing twice the work, and
per-stream throughput ROSE where batching should push it down. It was re-run
with repeated levels in one sweep: 1025.0 and 1021.3 on two independent
16-stream measurements, 386.3 and 375.9 at 8. It reproduces to within 1%.

The explanation is the point of batching: at 16 sequences the weights are read
ONCE per forward pass and amortised across all of them, so aggregate throughput
is bounded by compute rather than by bandwidth-per-sequence. The soft spot is
the 8-stream point, which sits below the trend — a scheduling/ubatch effect not
worth chasing, since both endpoints are solid.

**The contrast is the actionable finding**: on one GPU, a second PROCESS buys
0.98x and a second SLOT in one context buys up to 7.28x.

## Production model

muse-glimmer-30B-kquant-dynamic on the 3090:

| streams | aggregate tok/s | per-stream |
|---|---|---|
| 1 | 35.7 | 36.2 |
| 2 | 64.9 | 33.1 |
| 4 | 98.4 | 24.9 |

**35.7 tok/s against 20.74 on the M1 Ultra — 1.72x**, same model, same quant,
both greedy single-stream. That is the first hard number on whether the move
was worth it for decode, and it is before any tuning.

## Serial vs parallel on ONE server — and size helps

Two processes cannot even be attempted for muse: 19.65 GB twice does not fit a
24 GB card. The only concurrency available to a large model is slots inside one
context, so the question becomes N calls back to back versus N at once, with

    S = (t_parallel - t_one) / (t_serial - t_one)

| N | muse-30B S | speedup | Qwen-7B S | speedup |
|---|---|---|---|---|
| 2 | **0.054** | 1.90x | 0.147 | 1.76x |
| 4 | **0.148** | 2.76x | 0.213 | 2.46x |
| 8 | **0.196** | 3.38x | 0.269 | 2.79x |

**The LARGER model parallelises BETTER at every level** — lower serialization
and higher speedup, against the naive expectation that a heavier model would
concurrency worse. At N=2 muse's second call costs 5.4% of its solo time.

It is the bandwidth argument again, from the other side. A forward pass reads
19.65 GB of weights for muse and 4.68 GB for the 7B; batching amortises that
one read across every sequence in the pass, so the model with more to amortise
gains more from a slot. Single-stream both sit near 70% of the 3090's ~936 GB/s
(muse 684 GB/s effective, 7B 651 GB/s), which is what makes them comparable.

*A first version of this probe discarded the response body and timed the call
alone. It reported `t_one = 0.11 s` for a 30B — 1163 tok/s, impossible — and
every S computed from it was meaningless, because with t_one ~ 0 the formula
collapses to t_parallel/t_serial, the inverse speedup wearing another metric's
name. The probe now returns tokens generated, sets `ignore_eos` so every call
does identical work, refuses to run when the baseline comes up short, and
reports both arms' token counts (verified equal: 256/256, 512/512, 1024/1024).*

## What this means for the pipeline

1. **Put muse on the 3090 and paddle on the 3060.** That is the OCR × text pair
   that scored the M1's best serialization (0.342). Here the device split makes
   it ~0.00 — genuinely free, better than the best pairing the M1 could offer,
   and it is exactly the co-residence the operator asked for.
2. **Never run two model processes on one GPU expecting throughput.** It is
   strictly worse than serial. Where two tenants must share a device, they have
   to share a CONTEXT — the batched engine, not a second server.
3. **The `decode_mode: "batched"` work ports directly and pays more here.**
4. Workload SHAPE mattered on the M1 because everything shared one bus. With
   the device split it stops being the scheduling variable; placement is.

## Setup notes for the next person

* **CUDA 12.0 refuses gcc > 12** and Ubuntu 24.04 defaults to gcc-13. Build
  with `-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/gcc-12` or the first CUDA source
  file fails.
* `-c` on llama-server is TOTAL and divided across slots, so a fixed `-c` with
  a growing `--parallel` silently shrinks every slot's window. The scaling
  probe sizes it per slot for this reason.
* `pkill -f llama-server` **matches its own shell** when the pattern appears in
  the invoking command line, killing the caller (exit 144). Use `[l]lama-server`.
* Readiness must be HTTP 200 on `/health`, never a bare connection — the server
  binds its port before loading weights. The same trap produced 0.00 tok/s
  across every arm of the dflash probe on the M1.

## Addendum: the OCR lane must be its own PROCESS here, not an LLMVP secondary

Phase 2b residency was built on the M1 because a second process there meant a
second weight load into the SAME unified memory. On this rig the 3060 has its
own memory, so that reason is gone — and residency turns out to be actively
blocked.

Loading paddle as a hot secondary works for the WEIGHTS: `main_gpu: 1` put them
on the 3060 (GPU1 went 236 -> 878 MiB). The first vision request then killed
the server:

```
allocating 840.90 MiB on device 0: cudaMalloc failed: out of memory
GGML_ASSERT(buffer) failed
```

**Device 0** — where muse already holds 21 GiB. The projector does not follow
the model. `MTMDChatHandler.__init__` takes
`(mmproj_path, verbose, use_gpu, image_min_tokens, image_max_tokens, ...)`:
`use_gpu` is a BOOL and there is no device index anywhere in the signature, so
the mmproj always lands on the default CUDA device. A resident secondary VL
model cannot be placed on its own card through this binding.

That is not worth fixing here, because the measurement says the alternative is
free: cross-process AND cross-device was 0.0011. So on multi-GPU the OCR lane
runs as its own `llama-server` pinned with `CUDA_VISIBLE_DEVICES=1`, which is
what `--vl-backend llamacpp` already does.

Smoked end to end on a real paper, 3060 only:

```
12 pages, 10 verified, numeric 0.972, span 0.938, 2 figures,
max_repeat 43, 51.6 s  (~4.3 s/page against ~7 s/page on the M1)
```

**The M1's residency conclusion does not port.** It was correct there and is
wrong here, for a reason that is about memory topology rather than about
software: residency exists to avoid duplicating weights in a shared pool, and
there is no shared pool.

## More setup notes

* `/tmp` is on the 106 GB ROOT partition, `/home` has 718 GB. The LLMVP suite
  writes ~37 GB of pytest temp data and filled the root filesystem completely.
  Run it with `TMPDIR=/home/lah-rb/tmp`.
* `n_ctx: 131072` from the M1 config does NOT fit a 24 GB card. Computed from
  the GGUF header: 52 layers x 2 KV heads x (128+128) x 2 B = 52.0 KiB/token,
  so 131072 needs 6.50 GiB of KV on top of 18.3 GiB of weights = 25.6 GiB.
  llama.cpp reports this as "Failed to create llama context with model", which
  names neither memory nor context. 32768 fits at 20.7 GiB total.
* muse's chat path spends its first tokens in a reasoning block. `max_tokens:
  120` returned EMPTY content with `finish_reason: stop`; 700 returned a
  correct 87-token answer. Agent turn budgets must clear the reasoning block or
  turns come back blank rather than truncated.
* `llama-server` must be on PATH for `--vl-backend llamacpp` (it spawns by
  name, not by path).
