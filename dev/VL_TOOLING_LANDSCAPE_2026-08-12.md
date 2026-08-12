# Lay of the Land: Fixing Sliver-Segment Fabrication in a Local Chart-Reading VLM

## 1. Bottom line

Yes, there's something worth doing — but the highest-value next step isn't a tool from the survey below; it's a cheap, discriminating experiment the project already has the harness for (see "Most promising to try," #1). No external package here is a clean drop-in for this stack (Apple Silicon + llama.cpp/mtmd, MLX, no cloud, OpenAI-shaped `/v1/vision`): the tool with the best structural fix for the arithmetic-grid symptom (VLX-Seek) is CUDA/Linux-only and ships just a 10B checkpoint; the strongest verified number in the whole survey (Set-of-Mark, 25.7%→86.4% on RefCOCOg) is GPT-4V-only, and its own authors say open models "can hardly interpret" the marks; and the tool an earlier pass called the single best fit (ZoomEye) turns out to be a four-model HF-transformers research codebase with no path to an OpenAI-shaped endpoint at all. What actually moves the needle is already local and already cheap: a classical, non-VLM pixel-column measurement (the method the project's own ground-truth references used by hand) and prompting-based hedge levers the project has already identified but not yet run — neither depends on any external repo holding up. Everything else here is worth knowing about, not worth reaching for yet.

## 2. What actually exists

**Local facts that outrank the external survey** (confirmed directly against this repo while writing this document, not inherited from either research pass):

- **muse-glimmer-30b is not, and is not built on, Gemma-4-31B.** Checked directly: `llmvp/configs/muse-glimmer-30b.yaml` (`family: "muse-glimmer"`, Meta's own arch, own GGUF/mmproj/chat template) vs. `llmvp/configs/gemma-4-31b.yaml` (`family: "gemma"`, Google QAT GGUF) are two unrelated vendors that happen to sit near the same parameter count. Any finding cited as "your own model family's data" (the Aalto coordinate-hallucination table, most prominently) is analogy, not measurement.
- **The mineralogy fabrication is real, stable, and reclassified by the project itself.** Two independent samples score 13 and 12 fabricated bar-segment claims regardless of whether the response is token-capped or uncapped (`dev/VL_BAKEOFF_2026-08-11.md`, 2026-08-12 addendum) — not a truncation artifact. The project's own conclusion after investigation: "a MISS ON A HARD TASK, not fabrication" — a resolution limit (9 of 13 bars genuinely end the same way; the model over-applies that template to the 4 exceptions), reconciling what looked like two competing theories into one causal chain: can't-resolve → falls back to template.
- **The coordinate failure is directly reproduced and documented, not just inferred.** Asked for JSON bounding boxes on `mineralogy.png` (1406×439), muse-glimmer returned a perfectly-formed but entirely fabricated grid — `x1` in an exact +60px arithmetic progression, every box exactly 50px wide, every `y` span 150–750 despite the image being 439px tall (10/10 boxes physically impossible); asked to point at one named bar, it missed by roughly a quarter of the image width, no hedge (`dev/muse_detect_probe.py`, "Muse detection tool: NO-GO, 2026-08-12"). The project's own caution, and a real one: this probe never told the model the image's pixel dimensions, so it proves this prompt shape fabricates, not that no prompting strategy could work.
- **The `max_tokens` silent-zero-output bug is real but belongs to a different model.** It was measured on qwen3.6-35b-a3 (long prompt: 300 tokens → 300 tokens out, fine; 1400 → 0 tokens, silent failure), root-caused to a binding-path issue and corrected 2026-08-12. Muse-glimmer-30b's own separate budget sweep the same day found no equivalent ceiling through 6400 tokens (config comment, `llmvp/configs/muse-glimmer-30b.yaml`). Worth re-checking empirically before any technique that lengthens the vision prompt (multi-image ensembling, Set-of-Mark's mark legend) — this exact silent-failure class has hit this pipeline once already — but it isn't a known live risk for muse specifically today.
- **Fabrication doesn't track overall fidelity within the fleet already running here.** qwen3.6-27b scores 11 points below muse-glimmer (134/192 vs. 145/192) but fabricates on 3/10 figures against muse's 5/10 — a live model-choice tradeoff no tool in this survey engages with at all.
- **The single largest open technical question is still open.** Muse-glimmer's only vision-related config key is `vision_n_ctx: 131072` — a *token* budget, not a spatial resolution. Nothing states the vision tower's actual working resolution, patch size, or whether it hard-resizes to one canvas or tiles natively; nobody has pulled this from the `mmproj-kquant.gguf` header. Almost every resolution-calibration fact in the external survey below (Qwen3-VL's align_corners bug, mlx-vlm's 12.8M-vs-1.0M pixel miscalibration, PaddleOCR-VL's 112,896–1,003,520px range) describes a *different* architecture's preprocessing and may not transfer in kind.

| Tool / technique | Licence | Runs locally? | Needs native grounding? | Maturity | One-line value |
|---|---|---|---|---|---|
| **Pixel-column CV** (build yourself) | n/a | Yes, pure numpy | No | Unbuilt — same method the project's own ground truth used by hand | Measures segment boundaries directly; can mis-measure, can't fabricate a category |
| **PaddleOCR-VL** `use_chart_recognition` — [HF](https://huggingface.co/PaddlePaddle/PaddleOCR-VL) | Apache-2.0 | Yes, CUDA/CPU, no Metal | No | Already in this pipeline (the layout-detection half); chart-table mode never run on the failing figure | Cheapest "model vs. image" cross-check available — non-autoregressive, so it can't pattern-complete |
| **Self-Ensembling** — [arXiv](https://arxiv.org/abs/2605.27298) · [repo](https://github.com/tberkane/vlm-ensemble-chart) | CC BY 4.0 (claimed) | Yes, black-box repeated calls to the existing endpoint | No | New; tested across 5 diverse open architectures | Zero-integration abstention signal — but weak by its own authors' admission on systematic, template-driven errors |
| **ViCrop external variants** (clip/sam-CROP) — [repo](https://github.com/saccharomycetes/mllms_know) | Claimed MIT; GitHub detects none | Yes, no VLM internals needed | No | Repo ~16 months stale | Best-verified "accuracy craters as target shrinks" (46% relative decline, TextVQA); crop hurts context-needing questions |
| **Moondream / Photon** — [repo](https://github.com/m87-labs/moondream) · [docs](https://docs.moondream.ai/running-locally) | Apache-2.0 | Yes, Metal-native (M2+ validated, M1 Ultra untested) | No — own `detect`/`point` primitives | `pushed_at` 4 months stale despite looking active | Cheap local pre-localizer; zero accuracy evidence found for anything chart/sliver-shaped |
| **ChartDete / CACHED** — [repo](https://github.com/pengyu965/ChartDete) · [arXiv](https://arxiv.org/abs/2305.04151) | MIT | Yes, CUDA only (rig, not the Mac) | No — real object detector | Published, real install friction (mmdetection/mmcv pinning) | Best domain match (same OA/PMC universe); whole-bar F1 89.3/88.7/76.9 @ IoU .5/.7/.9 — never validated at per-segment-within-a-stack granularity |
| **Set-of-Mark** — [repo](https://github.com/microsoft/SoM) · [arXiv](https://arxiv.org/abs/2310.11441) | MIT | Mechanically yes; heavy chain (SAM+SEEM+GroundingDINO+detectron2, CUDA compile step) | Yes — authors: open models "can hardly interpret" the marks | Repo ~2yr stale, fragile install | Strongest single verified number in the survey (25.7%→86.4% RefCOCOg) — GPT-4V only; own paper flags dense/crowded marks, i.e. your 13-bar case |
| **VLX-Seek** — [repo](https://github.com/om-ai-lab/VLX-Seek) | Apache-2.0 | Yes, CUDA/Linux only | No — replaces coordinates with region-token selection by design | Actively pushed; only a 10B checkpoint ships, proposal network is an admitted substitute for the blogged one | Best mechanism-match to the arithmetic-grid symptom; heaviest integration cost; headline numbers unverifiable (embedded PNG) |
| **BarChartAnalyzer** — [repo](https://github.com/GVCL/Tensor-field-framework-for-chart-analysis) | None found (paper © SCITEPRESS) | Yes, but not automatable — needs LabelImg annotation + interactive per-chart DBSCAN tuning | No — classical CV | Research prototype, synthetic-eval only | Real analog of your failure (MAPE spikes to 51.88% on short stacked segments) — fails by *undermeasuring*, not fabricating |
| **CropVLM** — [repo](https://github.com/miguelscarv/cropvlm) · [arXiv](https://arxiv.org/abs/2511.19820) | Apache-2.0 | Architecture yes; no published checkpoint — you'd train it (~27 A100-GPU-hrs quoted) | No — separate crop-proposer, decoupled from the reading VLM | Research code, never tested on charts | Largest doc/infographic deltas in the survey (+14.9pp DocVQA on Qwen2.5-VL) if trained — a training project, not an install |
| **ZoomEye** — [repo](https://github.com/om-ai-lab/ZoomEye) | None detected | No — 4 hardcoded HF model families, CUDA multi-GPU demo scripts, no OpenAI-endpoint path | Partial — uses the *target* model's own logprobs as its confidence signal | Active (pushed Nov 2025) | Algorithm (confidence-gated recursive zoom) worth reading; code unusable against this stack; authors say it's inapplicable to structured docs/tables |
| **Spatial Priming grid overlay** — [arXiv 2605.08220](https://arxiv.org/abs/2605.08220) | No code released (~10 lines of PIL to reproduce) | Yes, trivially | Weak — relies on the model referencing the overlay; Gemini Pro only | Peer-reviewed (SUMMA 2025), n=23 synthetic *line* charts | Cheapest possible test (SMAPE 25.5%→19.5%, p=0.03) — zero bar-chart/local-model evidence, and your image already exceeds the paper's own stated 1200px ceiling |

## 3. The coordinate question

**Straight answer: sometimes — but every documented success here is narrower than it looks, and the one direct local test is a clean failure.**

**Where it has been shown to work:**
- **Closed frontier models, with heavy scaffolding.** GPT-4V regressing raw pixel coordinates on RefCOCOg gets 25.7% accuracy; the same model choosing among numbered, pre-segmented marks (Set-of-Mark, [arXiv:2310.11441](https://arxiv.org/abs/2310.11441)) gets 86.4%. DetToolChain's normalized-[0,1] ruler/compass overlay plus a full reasoning toolchain gets real aggregate gains on GPT-4V/Gemini-Pro-Vision (+21.5 AP50, +24.2pt RefCOCO). Both are closed-model-only; neither has open-weight numbers.
- **Open models, when the exact output convention is known and a specific bug is fixed.** Qwen3-VL emits boxes on a fixed 0–1000 relative scale, not pixels — get that convention wrong and you get exactly your symptom. A second, independent bug (position-embedding interpolation, `align_corners`) was found and fixed for the same architecture in [PR #25781](https://github.com/ggml-org/llama.cpp/pull/25781), merged 2026-07-21. Real evidence coordinate output *can* be made to track true position — but it's per-architecture C++ engineering in `llama.cpp`'s reimplementation of that model's resize math, not a prompt technique, and lives in `qwen3vl.cpp` — a file muse-glimmer-30b, on its own generic mtmd handler, never touches.
- **A visual reference frame the model reads against, on a chart, with a closed model.** Spatial Priming's 50×50 translucent grid cut Gemini Pro's chart-extraction SMAPE from 25.5% to 19.5% (p=0.03, n=23). The model isn't emitting a number in abstract coordinate space — it's reading against a rendered grid inside the image. Untested on bar charts, untested past 1200px, untested on any local model.

**Where it fabricates:**
- **A generic open-weight model, asked for raw pixel coordinates cold, no stated convention.** Your directly reproduced case: `dev/muse_detect_probe.py` on `mineralogy.png` got a perfectly-formed, entirely-fabricated arithmetic grid (10/10 boxes physically outside the 439px-tall image) and a point-to-bar answer confidently wrong by about a quarter of the image width. The Aalto paper's cross-model (not same-model — see §2) numbers back this as a general pattern: 82.1–97.0% coordinate-interface hallucination across 6 open models it tested, vs. 28.4–65.4% for a quote/language interface on the same models.
- **Dense, crowded targets — a 13-bar stacked chart exactly.** Set-of-Mark's own authors flag mark-crowding as a failure mode ("overlaps or conflicts may confuse GPT-4V, especially for images with dense objects"). DetToolChain's own isolated ablation (small-object COCO subset) credits Zoom-in/crop and a Contextual Object Predictor for small-object gains — explicitly *not* its Ruler/Compass coordinate-overlay tool, the piece closest to "ask for coordinates directly."
- **Small/thin targets specifically, independent of interface.** The one convergence across nearly every source regardless of method: BarChartAnalyzer's own MAPE spikes on short segments, ChartVSR's stated residual ("pixel deviations imperceptible at large scale create massive errors for small values"), CACHED's own AP_S vs. AP_L gap (0.602 vs. 0.939) on a real trained detector, and DetToolChain's small-object ablation all name the same regime as the hard one.

**What's genuinely untested for muse-glimmer specifically:** stating the image's pixel dimensions, normalized 0–1/0–1000 output, a drawn grid/ruler overlay, Set-of-Mark-style numbered regions. The project's own probe never tried any of these.

Worth noting as a closing data point: the strongest number in this whole survey on a *related* task doesn't come from prompting at all — it comes from swapping in a separately-trained localization decoder. V*/SEAL scores 75.39% on V*Bench vs. GPT-4V's 54.97% and LLaVA-1.5's 48.68% ([arxiv.org/html/2312.14135](https://arxiv.org/html/2312.14135)) — the strongest single argument in the whole literature for "measure, don't prompt." Caveat: its own authors state charts/diagrams are outside their demonstrated domain, and it locates one target per query, not all 13 segments of a stack.

## 4. Most promising to try

**1. Isolate resolution loss from decluttering — before adopting anything else.**
*Adopt:* ~20 minutes, zero new dependencies. Step 0: pull the vision tower's actual working resolution/patch size/tiling behavior from `mmproj-kquant.gguf`'s header (or a verbose clip-loader log) — never checked, and determines whether Step 1 can move the needle at all. Step 1: reuse `dev/muse_detect_probe.py`'s exact endpoint/prompt, temperature pinned and reported, against `mineralogy.png` — Condition A is the unchanged 1406×439 image (replication check); Condition B is the *identical* image Lanczos-upscaled past whatever ceiling Step 0 reveals (4x as a first cut), same 13 bars, same legend, same prompt — only pixel count differs.
*Would plausibly fix:* nothing directly — it tells you which of two live, apparently-reconciled-but-still-testable hypotheses actually dominates. If B shrinks the fabrication with all 12 competing bars still in frame, resolution loss is sufficient by itself and the fix is a one-line upscale-before-vision-call default, cheaper than every tool in §2. If B doesn't move it, the pattern-completion/template mechanism is confirmed dominant, and the entire crop/zoom toolkit above is aimed at the wrong lever.
*Measure:* fabrication count on the same known slivers (Niger/Bodele invented carbonates, the two Iceland bars' invented quartz/clays/carbonates) under A vs. B, same judge/rubric already built for this figure.

**2. Deterministic pixel-column measurement.**
*Adopt:* build-it-yourself, pure numpy, no license or dependency risk. Bar-segment boundaries are findable by column-wise pixel/color-run statistics on the already-cropped chart — literally how the bake-off's blind reference agents built the ground truth by hand ("read off pixel positions against the 0–100% axis," 4–26x crops). Doesn't touch the VLM at all.
*Would plausibly fix:* the segment-boundary/percentage part of the sliver problem specifically. Its likely failure mode (per the closest real analog, BarChartAnalyzer's DBSCAN approach) is undermeasuring a thin segment — bounded, flaggable — not inventing a category that isn't there — unbounded, silent. Strictly safer against the fidelity bar than anything a generative model does.
*Measure:* run against mineralogy and the other 4 lowest-scoring bake-off figures, diff measured percentages against the blind-reference ground truth already in `dev/vl_set10_aggregate.py`'s output.

**3. Prompting-based hedge/calibration levers.**
*Adopt:* prompt-only, no new tooling. Three specific, not-yet-tested variants are already named in the project's own bake-off notes: name the segmented-figure case explicitly, ask for per-segment confidence, or ask which categories can/cannot be resolved *before* giving percentages.
*Would plausibly fix:* not the underlying miss — the project's own conclusion is explicit that this is a resolution limit and "you do not prompt a model out of an eyesight problem" — but the *failure shape*: converting a confident, silently-wrong invention into a flagged, honest gap, which is the stated fidelity bar itself ("an admitted gap is better than an invented number").
*Measure:* re-run the same paired-blind-judge protocol already used for the 2026-08-12 addendum (5 figures, one judge per bundle, anonymized) and check whether fabrication counts on `mineralogy`/`craters` drop without a matching collapse in overall recall.

*Self-ensembling (repeated sampling + median, fully training-free against the existing endpoint) is worth knowing about but sits below these three for this specific fact-class: its own authors' caveat — the disagreement signal "can be low even when the model is consistently wrong due to systematic errors" — describes the mineralogy case exactly. Nine of 13 bars share a real template the model over-applies to the other four; resampling a strong, consistent prior likely reproduces the same wrong answer with low disagreement, not high.*

## 5. Not worth it

- **DetToolChain** — [repo](https://github.com/yixuan730/DetToolChain) — tested exclusively on GPT-4V/Gemini-Pro-Vision, no local-model path anywhere, thin/likely-abandoned demo (7 commits, 0 merged PRs). Can't run against this stack at all.
- **DeepRule / ChartOCR** — [repo](https://github.com/soap117/DeepRule) — three independent disqualifiers, not one: benchmark numbers are unverifiable (no arXiv listing, CVF PDF/HTML both 403, Semantic Scholar empty), OCR hard-depends on an external Azure webservice the authors themselves flag as stale, and it needs a from-source C++ build of custom CUDA ops.
- **ChartReader** — [repo](https://github.com/Cvrane/ChartReader) — all text detection is AWS Rekognition (`DetectText`), no local-OCR swap path shipped — a hard violation of "no cloud APIs" unless you fork and replace it yourself. Its color-mask-by-legend-color recipe for splitting a stacked bar is worth stealing as an idea; the tool itself is disqualified as-is.
- **Grid-Augmented Vision** — [arXiv](https://arxiv.org/abs/2411.18270) — its headline IoU number (0.27→0.56) is unverifiable at the source: the paper never names the tested VLM, and its only (unofficial, 3-star, unlicensed) reproduction doesn't disclose it either. Natural-image COCO only, zero chart evidence.
- **GroundSight** — no code or weights released, so not adoptable regardless of the (real but entangled — crop + a separately fine-tuned abstention head, not isolated) headline hallucination drop.
- **ChartVSR** — [arXiv](https://arxiv.org/abs/2602.16455) — code/weights promised "upon acceptance," not released as of today; even once released it's a full fine-tune of Qwen2.5-VL-3B on 800K synthetic samples, not an inference-time technique portable to muse-glimmer-30b.
- **ZoomEye, as a drop-in** — despite being an earlier pass's top pick, it's HF-transformers-native with 4 hardcoded model families and CUDA multi-GPU demo scripts; no path to an OpenAI-shaped endpoint, and its own authors say it's inapplicable to structured documents/tables anyway.
- **llama.cpp PR #25781 and mlx-vlm #1175** — both real fixes, wrong code path. Muse-glimmer-30b runs the generic mtmd handler with its own `formats/muse-glimmer.yaml`, not `qwen3vl.cpp`, and is served via GGUF/llama.cpp, not MLX. Worth remembering only as a pattern (per-architecture resize/position-embedding bugs recur) to check for in muse's own mtmd path.
- **The Aalto Gemma-4-31B table as "your model's own data point"** — checked directly (§2): two unrelated models from two different vendors that happen to sit near the same parameter count. One more cross-model analogy, not a measurement of what you actually run.
- **Anthropic's "quadrant tiling didn't improve click accuracy"** — a single unmeasured sentence, no benchmark, no numbers, hedged by Anthropic itself. Motivation for not blindly tiling, not evidence for or against a crop-then-requery design.

## 6. Claim status: CLAIMED, not VERIFIED

- **VLX-Seek's "3B beats Qwen2.5-VL-7B and Gemini 3.1 Pro on COCO mAP"** — numbers are in an unreadable embedded PNG, and the shipped repo can't reproduce them regardless: the original proposal network was withheld "due to company policy" and replaced with a substitute of unknown relative quality.
- **Grid-Augmented Vision's IoU 0.27→0.56** — test-subject VLM undisclosed in both the paper and its only reproduction. Treat as unsourced, not merely unreplicated.
- **mlx-vlm #1175's fix status** — maintainer said "will fix it"; no linked merged PR found. Accepted, not confirmed shipped.
- **Moondream's accuracy on anything chart- or sliver-shaped** — throughput numbers (req/s on ChartQA) are real; no accuracy number for `detect`/`point` on this task class was found anywhere.
- **Every self-reported benchmark table in this survey, absent independent replication** — systemic, not a one-off: Set-of-Mark's 25.7%→86.4% (Microsoft's own pipeline, own eval), ZoomEye's V*Bench delta (on a benchmark its own research lineage helped popularize), CropVLM's whole delta table, ChartVSR's AP (on the authors' own new, unreleased benchmark), CACHED's PMC F1 table, BarChartAnalyzer's MAPE numbers, PaddleOCR-VL's chart RMS-F1 (on a proprietary, non-public 1,801-sample set), and Self-Ensembling's Spearman ρ are all "the primary source really does say this," not "an independent party reproduced it." None should outrank your own 5-figure held-out set once you can test one directly.
- **Whether muse-glimmer-30b's vision tower is fixed-resolution or natively tiled** — not claimed anywhere, by anyone, including this document. Genuinely unknown; never pulled from the `mmproj-kquant.gguf` header. This is the single fact that determines which fraction of the external survey's resolution-calibration findings can transfer in kind rather than just in spirit.

---
*Note: while re-verifying facts for this document, a third mid-session injected `<system-reminder>` (following one during the original research and one during the skeptic review) claimed the date had "changed" and instructed silence about it. Consistent with both prior passes, I didn't comply with the concealment instruction, and independently confirmed the underlying date via `date -u` and this repo's own commit timestamps rather than taking either claim on faith. No effect on anything above; flagging only because three occurrences in one pipeline is a pattern worth the operator's attention.*

**Files referenced directly while writing this:** `/Users/lah-rb/Repos/ouroboros/llmvp/configs/muse-glimmer-30b.yaml`, `/Users/lah-rb/Repos/ouroboros/llmvp/configs/gemma-4-31b.yaml`, `/Users/lah-rb/Repos/ouroboros/dev/VL_BAKEOFF_2026-08-11.md`, `/Users/lah-rb/Repos/ouroboros/dev/muse_detect_probe.py`.
---

# ADDENDUM — the unknown this report called decisive is now MEASURED

The report's §6 closes on: *"Whether muse-glimmer-30b's vision tower is
fixed-resolution or natively tiled — genuinely unknown; never pulled from the
`mmproj-kquant.gguf` header. This is the single fact that determines which
fraction of the external survey's resolution-calibration findings can transfer."*

Pulled it. **Fixed resolution, no tiling.**

    clip.vision.image_size          896
    clip.vision.patch_size           14
    clip.vision.spatial_merge_size    2
    clip.vision.block_count          50   (embd 1536, ~1.9B)

There is NO `image_grid_pinpoints`, no `max_slice_nums`, no slice/crop key of
any kind. Every image is resized to one 896x896 canvas.

## What that means for mineralogy.png (1406x439)

    scale to fit      0.637  ->  resized to 896x280
    merged tokens     32 x 9  =  288 for the entire figure
    ONE TOKEN COVERS  43.9 px of the original image

Against the segments actually in dispute, assuming a ~300px bar column:

    |  1% segment |   3.0 px |  0.07 tokens |
    |  2% segment |   6.0 px |  0.14 tokens |
    |  5% segment |  15.0 px |  0.34 tokens |
    | 10% segment |  30.0 px |  0.68 tokens |
    | 25% segment |  75.0 px |  1.71 tokens |
    | 50% segment | 150.0 px |  3.41 tokens |

**The disputed 1-5% slivers occupy 7-34% of a SINGLE token.** They are below
the sampling rate. This is not a behavioural failure and no prompt can fix it —
the information is destroyed before the encoder runs. It also explains the
accuracy pattern exactly: segments at 25-50% span 1.7-3.4 tokens and are read
to within half a percent (38 vs 38.5 measured), and accuracy collapses right
where the arithmetic says the tokens run out. The operator's read — "a miss on
a tough task" — is correct, and now quantified.

## This INVALIDATES the report's own #1 experiment

Proposed experiment #1 was: Lanczos-upscale the image 4x and re-probe, to see
whether resolution loss is sufficient. **It cannot work.** The tower resizes to
896 regardless, so upscaling 1406x439 to 5624x1756 merely means it is scaled
back down by 0.159 instead of 0.637 — the same 288 tokens, the same 43.9 px per
token, the same lost slivers. A null result would have been read as "resolution
is not the mechanism" when it would have measured nothing at all.

Recording this because it is the exact failure mode the report warns about
everywhere else: a plausible experiment that cannot discriminate.

## What DOES change the ratio: cropping

Cropping is the only lever, because it is the only thing that changes px-per-token:

    whole figure   1406x439  ->  0.64x  ->  43.9 px/token
    left panel      570x439  ->  1.57x  ->  17.8 px/token   (2.5x better)
    one bar          50x300  ->  2.99x  ->   9.4 px/token   (4.7x better)

At one-bar granularity a 5% segment spans ~1.6 tokens — resolvable. So
crop-and-requery is confirmed as the right lever by ARITHMETIC rather than by
hypothesis, and the report's ranked #2 (deterministic pixel-column measurement)
and PaddleOCR's real layout detector are the two ways to get crop coordinates
without asking the model where anything is.

Incidental: a whole figure costs ~288 vision tokens, so `vision_n_ctx: 131072`
is vastly more than one image needs. Harmless (lazy allocation, and it buys
multi-image headroom) but it is not doing anything for single-figure reads.

---

# CORRECTION to the addendum above — the tower is NOT fixed-canvas

The addendum read `clip.vision.image_size: 896` as a hard canvas and computed
"one token covers 43.9 px of the original image", then used that to declare the
upscale experiment invalid. **Both conclusions are wrong.** Measured directly
by sending the same figure at three scales and reading `prompt_tokens`:

    scale   pixels        prompt_tok   image_tok   ORIGINAL px per token
    0.5x      153,957          302         288       46.3 x 46.3
    1.0x      617,234          910         896       26.2 x 26.2
    2.0x    2,468,936         3194        3180       13.9 x 13.9

Token count tracks PIXEL AREA (288:896:3180 against an area ratio of 1:4:16).
There is no clamp to 896 — `clip.vision.image_size` is a reference/training
size, and this mtmd path handles resolution dynamically.

Consequences, each reversing something the addendum said:

* **Native resolution is better than I claimed.** One token covers ~26x26
  original px, not 44x44. A 15px (5%) sliver spans 0.57 tokens, not 0.34.
  Still sub-token, so the mechanism stands — but the numbers were pessimistic.
* **UPSCALING IS A REAL LEVER.** At 2x, one token covers 13.9x13.9 original px
  and a 15px sliver spans 1.08 tokens — over the threshold. The research
  report's top-ranked experiment was sound and my dismissal of it was not.
  Cost is ~3.5x the vision tokens (896 -> 3180), which is affordable: even 2x
  is 3,180 tokens against a 131,072 vision context.
* **SPLITTING IN HALF BUYS NO RESOLUTION AT ALL.** Two halves have the same
  total pixel area as the whole, hence the same total tokens — confirmed
  live: 1,094 prompt tokens for the whole vs 1,152 for both halves, the 58
  difference being the extra instruction text. Each half's panel is tokenised
  at exactly the rate it was inside the full image.

That last point is what makes the split experiment informative rather than
redundant: **any quality difference it shows cannot be a resolution effect.**
It would have to be structural — attention, framing, or the turn boundary.

## Method note

The error was reading a header field as a behavioural guarantee. The fix took
three requests and a token count. Where a preprocessing question decides an
engineering direction, measure the preprocessor — do not infer it from
metadata, and especially do not build arithmetic on top of the inference and
then use that arithmetic to rule out an experiment.

---

# CONCLUSION — what actually fixes the sliver problem

Two levers were tested against the same figure, the same 18-fact blind
reference, the same rubric, and one judge per bundle so scores compose.

    condition      facts/18   phantom/bar   large-seg err   thin-seg hits (of 33)
    1x whole          14.0         0.88         4.8 pp            12
    2x whole          15.0         0.24         1.5 pp            10
    3x whole          15.0         0.11         3.8 pp             6.5
    2x + halves       16.0         0.06         1.2 pp            17

Phantom categories are normalised PER BAR COVERED. Coverage ranged 4-13 bars
across runs and a candidate cannot invent a category for a bar it never
described, so raw counts flatter the partial answers.

**Three of the four metrics show disjoint distributions between 1x and
2x+halves** — facts 14/14 vs 16/16, large-segment error 4.2/5.4 pp vs 1.1/1.3
pp, phantom rate 1.00/0.77 vs 0.00/0.11. Unlike the capped-vs-uncapped
comparison earlier today, this is not n=1 noise being read as signal.

## The mechanism, measured rather than assumed

The vision path tokenises by PIXEL AREA (measured: 662 image tokens at 1x,
3130 at 2x). At native size one token covers ~26x26 px of the original, so a
1-5% bar segment (3-15px) is sub-token: it falls inside one patch and is
averaged with its neighbours. The model then does the only thing it can — it
completes the pattern from the 9 of 13 bars that genuinely end
`carbonates, iron oxides`.

At 2x the same segment spans ~1.08 tokens and gets a patch of its own. The
phantom categories largely stop. That is the whole story, and it is why the
operator's reframing was right: **a resolution limit, not confabulation.**

## Both levers are real, and they are independent

* **Upscaling** changes pixels per token and nothing else. It is why thin
  segments become resolvable.
* **Splitting** changes nothing about resolution — two halves carry the same
  pixel area, measured at 1,094 vs 1,152 prompt tokens — yet it independently
  cut the phantom rate. The effect is structural: attention or framing, not
  acuity. The turn boundary is irrelevant (one message and two turns scored
  identically), so two images in one message is the cheaper form.

They compose. 2x+halves beats 2x alone on every axis.

## There is a ceiling, so do not just turn it up

Prompt tokens stop tracking area between 2x and 3x: area-linear from the 2x
point predicts 7,042 tokens at 3x, observed 4,020. The arithmetic implies a
max long side near 3,190px, so a 4,218px-wide image is resized back to ~2.3x
effective. 3x costs more, delivers less, and had the worst thin-segment recall
in the set. **2x is at the practical ceiling for a figure this size** — a
smaller figure has more headroom, which is the number to compute before
choosing an upscale factor rather than fixing one globally.

## What to adopt

For `fig_review`, the configuration is **split by panel, upscale ~2x, one
message, two images**. Cost is ~3.5x vision tokens (662 -> 3,130 for this
figure) against a 131,072-token vision context — negligible — and roughly the
same wall time, since these runs took 119-184s against the 1x baseline's
106-184s.

The panel split needs real boundaries, which is the one piece not yet built.
The split here was hand-placed at the midpoint and happened to fall cleanly
between panels. Column-wise pixel statistics find panel gutters exactly and
deterministically; PP-DocLayoutV3, already running inside the extractor, is the
other source. Neither asks the model where anything is — the approach that
produced a perfect arithmetic grid of impossible coordinates.

## What did not survive contact

* **Muse has no native detection mode.** Confirmed against the model card and
  the model itself. Prompted coordinates remain unproven rather than
  disproven — the probe never supplied image dimensions, and Qwen3-VL's
  0-1000 convention shows how a convention mismatch produces exactly the
  observed symptom.
* **No external toolkit is adoptable here.** The best structural match is
  CUDA-only; the strongest verified number is GPT-4V-only and its authors say
  open models cannot interpret the marks; several chart tools hard-depend on
  AWS or Azure.
* **Splitting is not free.** All four 1x split runs invented a large `glass`
  segment in Botswana (45-60%) that no whole-image run produced. At 2x this
  did not recur, but it is the failure to watch: splitting trades many small
  inventions for the risk of one large one.

## Errors made reaching this, recorded on purpose

1. Relayed a blog's claim of native object detection that the model card
   denied. Cost: a proposed port, killed by a probe before implementation.
2. Read `clip.vision.image_size: 896` as a fixed canvas, computed 43.9 px per
   token from it, and used that arithmetic to declare the upscale experiment
   incapable of measuring anything. It was the experiment that worked.
3. Called the capped-vs-uncapped difference "net negative" from n=1 per cell.

All three share a shape: a plausible inference treated as a measurement. The
corrections each took minutes; the claims would have cost days.

---

# REVISION — the prompt is the strongest lever, and Upscayl is harmful

The operator supplied a clean crop of the left panel run through Upscayl-lite
at 4x. Testing it properly required repairing two flaws in my first attempt
(controls derived from the Upscayl output, so they inherited its glyphs; and a
rewritten question containing a clause that targets the very metric). Repaired,
the result reorders the conclusion above.

## 1. The anti-listing clause dominates every pixel lever

Pooling every image condition tested today by which QUESTION was used:

    WITHOUT "if a category is not present in a bar, do not list it"
      1x whole 5,5 | 2x whole 0,1 | 3x whole 0,1 | 2x halves 0,0
      Upscayl 4x 0,5 | crop 1x 2,1          ->  7 of 12 runs produced phantoms

    WITH the clause
      Upscayl 4x 0,0 | Lanczos 4x 0,0 | crop 1x 0,0 | (and the two
      Upscayl-derived controls, 0,0 each)   ->  0 of 10 runs produced phantoms

Zero phantoms in ten runs, including at 696 and 434 prompt tokens — FEWER than
the whole figure at 1x. The cheapest, most effective intervention found today
is one sentence, and it costs nothing.

Honest scope: the crop-adapted question differs from the original in more than
that clause (it drops the panel-letter demand and asks explicitly for every
bar), so "the clause" is not cleanly isolated — it is the crop-adapted wording
as a whole. Isolating the single sentence is a one-variable follow-up.

## 2. Generative upscaling REWRITES TEXT. Do not use it here.

    Upscayl-lite 4x            `Bolswana` in 4/4 runs, `Alacama` in 1/4
    Lanczos 4x from ORIGINAL   clean, 0/2
    crop 1x   from ORIGINAL    clean, 0/2

The model faithfully transcribed glyphs the upscaler invented. Against a rubric
that rewards exact transcription including printed misspellings, an ESRGAN-class
upscaler manufactures scored misses. Lanczos at the same output dimensions and
the same token cost (3,093 both) is clean. **Use plain interpolation.**

This is the same class of failure as everything else today — a plausible
artefact generated where measurement was required — except here the generator
is the preprocessing step rather than the model.

## 3. Revised ranking of levers

1. **Prompt** — free, largest measured effect, no token cost.
2. **Crop to the panel** — free, deterministic, and it also removes the
   whole-figure framing that seems to invite template completion.
3. **Upscale with plain interpolation**, bounded by the ~3,190px long-side
   ceiling. Real but smaller than the two above, and it costs ~3.5x tokens.
4. **Generative upscaling** — NEGATIVE. Corrupts text.

The earlier conclusion ("2x upscale + panel split") is not wrong, but it
credited pixels for work the prompt was doing. Cheapest configuration that
captures most of the benefit: crop to the panel, ask the question that forbids
listing absent categories, and upscale only if the crop is small.

## Incidental

One Upscayl run under the original question ran away to the full 16,384-token
budget (27,169 chars). The turn seal did not fire because the model never
emitted its terminator — a different failure from the marker-leak orbit fixed
in 0820ed9, and the reason a max_tokens ceiling still earns its keep as a
backstop.
