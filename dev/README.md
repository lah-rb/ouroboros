# dev/ — operator tools & provenance

Curated 2026-07-16 (the great cleanup: 252 entries → this set). Everything
here is either a reusable operator tool or the provenance of a shipped
artifact. One-shot experiment scripts get deleted once their conclusions are
banked (memories / dev/archive/docs/) — do not let this directory re-rot.

## Active concept docs
- `JUDGE_STANDARD.md` — frozen judging standard v1.0 for Opus ground-truth panels (governs the pending 615-turn quarantine worklist).
- `TRAP_BRIEF.md` — deterministic-startup-fail blind-diagnose trap: evidence + fix options (open).
- `ORACLE_IMPROVEMENTS_PLAN.md` — TB2 oracle retry-differently / self-check backlog (open).
- `MULTI_MODEL_PLAN.md` — model-config hotswap + multi-model + provider gateway design (approved direction; phases pending; gates the LLM boss layer).

## Benchmarks & A/B harnesses
- `ab_reasoning_games.sh` / `ab_boss_game.sh` — adaptive-reasoning vs baseline game_challenge A/Bs (parallel arms on the batched server).
- `swe_eval.sh` — SWE-bench eval wrapper. `swe_taxonomy.py` — per-task failure-class taxonomy.
- `tb2_harbor_gptoss.sh` / `canary_tb2_gptoss.sh` — TB2/Harbor full + 8-task canary. `tb_resident_gptoss.sh` — legacy TB1 8-set runner.
- `tb_adapter_probe.py` / `tb_pty_probe.py` — tb adapter integration/isolation regression gates.
- `tau_gold_replay.py` / `tau_episode_smoke.py` — τ-bench adapter plumbing validation.
- `debate_ab.py` — the debate-vs-CoT apparatus (two-Opus-referee blind eval; keeper).
- `oracle_health.sh` — TB1 grader-health sweep. `reverify_oracles.py` — offline oracle re-scorer over preserved traces.
- **Tiering** — `ouroboros.py tier` (`agent/tier/`) runs the batch; `blind_panel/TIER_RUBRIC_v2.md`
  is the instrument (epoch v2.0; ladder in `blind_panel/LADDER.md`) and
  `blind_panel/CHALLENGE_v2_CHECKLIST.md` the judge's half of
  `missions/game_challenge_tier.yaml` (v2 brief). The `_v1` rubric/checklist are the ARCHIVED
  pre-epoch instrument for `tier_20260731-050209` and earlier — never used for new placements.
  `blind_panel/stage.py` strips and blind-scans each artifact. `tier_batch.sh` is the SUPERSEDED
  shell original, kept as the provenance of run `tier_20260729-010118`.
- `stop_token_audit.py` — every model's GGUF `eot`/`eom` vs its family's `gen_stop`. Belongs in
  the new-family battery: a model can declare a turn-terminator we never stop on, and **no guard
  we own can see it** — every degeneration detector looks for repetition, and a model
  role-playing both sides of a chat never repeats. Cost the glm-4.7-flash arm 83 minutes.
- `gguf_geometry.py` — KV geometry, hot-path bytes/token, chat template. Read the headers before
  sizing any config; note the formula's ×2 for separate K/V is wrong for MLA architectures
  (glm-4.7-flash measured at exactly half its predicted KV).

## LLMVP serving acceptance & perf

- `BATCHED_VISION_2026-08-26.md` — vision decode inside the batched
  multi-seq engine: P0 probe verdicts (KV-integrity GO), predictions,
  build log. Probes live in `llmvp/probe_vision_*.py`.

- `OCR_LANE_2026-08-29.md` — **can the figtext treatment be repeated on OCR?
  No.** Phase 0 refuted both levers before any product code: decode is 12.8 %
  of a crop request (so the batched path is the wrong architecture — it
  serializes encode) and pool width 2/4/8 is flat (so do not widen
  `vision_pool_size` or add `ocr*` lanes). OCR is bound by a ~92 ms FIXED
  per-request serving cost that scales with nothing. Probes:
  `llmvp/probe_ocr_{proxy,sweep,floor,analyze,stage_split}.py`.

- `FIGURE_DIGITIZER_2026-08-30.md` — **reading published spectrum plots as
  matrices instead of vision consumables** (Phase 1a: relocation + tiering).
  Corrects a claim I made to the operator: plot curves are recoverable as
  VECTOR paths in only 8% of LIBS papers (6.2% of figures), not two thirds —
  the high segment counts are text glyph outlines, so the raster CV path is
  tier 1. Crops carry no link back to their PDF, but NCC relocation
  re-derives page + rect at **100%** (median ncc 0.9972), so no extractor
  change is needed. Tier-N (native embedded image) available for **80%** of
  figures — but at a median **1.79×** gain, not the 2.8× an earlier small
  probe suggested: publishers standardise on 300 dpi, so the gain is pinned
  near 300/160. Wide survey plots therefore stay unresolvable at the line
  level; assignment has to refuse there. Four measurement-caught bugs, all
  mine, incl. a 40-page sweep cap that faked a 12% relocation failure rate and
  a figtext veto that was silently dropping 1,929 real spectrum figures.
  Tool: `tools/figure_digitizer/`; artifacts: `databank/figdata/`.

- `batched_parity.py` — batched-decode determinism/isolation parity. `duo_soak.py` — multi-seat soak + latch-heal.
- `snapshot_stress.py` — snapshot-tier acceptance. `cache_strategy_stress.py` / `cache_compat_matrix.{py,sh}` — KV strategy & per-model compat. **The compat matrix is the sweep harness — extend it, don't rebuild it** (`CACHE_SWEEP_PLAN.md` §sweep: raise depth 3→12, record the new `session_strategy` health fields, needle past the window).
- `caching/FEATURE_MATRIX.md` — **the operational view (2026-07-30)**: there are only THREE deployable strategies (pool+replay / pool+resident / batched+resident — batched hard-requires resident, so full_replay is unreachable there), and this maps all 15 cache/state features onto them with measured benefit, measured cost, and a per-model "what you can layer today" verdict. Read it before enabling any cache feature on a model. Headline: five features are on by config and OFF in reality, four of them under the production batched shape, and three announce it only at log.debug.
- `caching/` — **the caching corpus + formal experiment** (2026-07-29): `CORPUS.md` = every mechanism (M1–M20), claim, and measurement in one place with a 12+12 contradiction ledger and the vocabulary standard (prefix_reuse_rate, NOT "hit rate"); `EXPERIMENT.md` = the pre-registered design (blocks A–F, predictions recorded before any cell); `run_blocks.sh` = the cell runner (temp configs, intervention-landed hard assert, append-only results). Supersedes `archive/docs/CACHE_STATE.md` as the reference.
- `CACHE_SWEEP_PLAN.md` — **why session caching gates the tier campaign** (2026-07-29, from the hy3 arm): full_replay re-prefills the whole session every turn, so the 10th PTY command cost 58.8s of prefill for 20 tokens, prefill hit 60.8% of run wall, and goal throughput fell ~5× between half-hours. 9 of 19 configs are on that path (5 explicit, 4 by omission — `resident_seq_cache` defaults False); prior art (`archive/docs/CACHE_STATE.md`, measured 2026-07-02) resolves 3 as correctly-recurrent, leaving 6 candidates. Carries pre-registered predictions, one recorded correction, and the KV-recheck trap: SWA models need `swa_full` for resident and `swa_full` RAISES KV.
- `decode_scaling_bench.py` / `swarm_3proc_bench.py` / `jit_exercise_131k.py` — throughput & pool-lifecycle benches.
- `ctx_decode_probe.py` / `ctx_session_decode_probe.py` / `ctx_multiturn_probe.py` — pool-beyond-trained capacity probes (stateless needle matrix; session-cached clean decode at 200k; 430-round coordinator-interrogates-workers rehearsal — 215/215 recall, 1.1s round-trips).
- `context_ceiling_probe.py` — a SIGNPOST, not a tool: the context-ceiling probe moved into LLMVP proper (`api/main.py --probe-context <configs>|active [--probe-no-write]`). `probe_ctx.sh` launches it detached from anywhere. The probe measures the largest n_ctx a model will load AND decode, then writes `probe_verified_n_ctx` back into the config so the KV preflight stops guessing. **Its ceiling is a fraction of PHYSICAL memory — `iogpu.wired_limit_mb` is NOT a ceiling** (four passing measurements cross it); a clamp to it was added and retracted the same day for refusing production-proven configs.
- `hy3_ceiling_ladder2.sh` — SUPERSEDED by the above; kept for the round-1/round-2 reasoning it records. hy3's ceiling is now measured at 36864 (guard-bound lower bound) and lives in its config header.
- `chatml_think_probe.py` — five-minute think-budget spot-check (default vs `/no_think`) for new qwen-family distills, BEFORE they get a mission leg. The Qwopus lesson: a distill's real CoT budget can be 10x its billing, and one 32k-token design monologue burns an hour.
- `serving_perf_reference.md` — **measured failure edges & design rules** (2026-07-20/21): decode-vs-N, prefill-vs-size, shared-pool wedge zone, worker budget sizing. Read before sizing any fan-out experiment.
- `qwen3_loop_research.md` — **why Qwen3-family models enter deliberation orbits** (2026-07-22, verified 5-angle research): model-level GDN-hybrid propensity + our sampling below every official profile + 64-token penalty window blind to 800-token cycles. Ranked mitigations (config, penalty window, DRY sampler, degen-retry recipe).
- `plot_swarm_perf.py` — render a swarm fan-out's `.agent/swarm_perf.jsonl` (active streams, per-symbol gantt, aggregate tok/s over time) + per-symbol actuals summary.
- `gemma_pad_probe.py` — Gemma-4 thinking-toggle PADDING probe (2026-07-22): 5 arms (ON / OFF / OFF-no-guard / PAD / PAD-no-guard) proving a same-token-length filler in the `<|think|>` slot reads as ABSENCE, not activation — the trick that makes the zero-prefill head-swap legal for insert/delete toggles. `gemma4_chat_template.jinja` = the official Gemma-4 template it was derived from (also the reference for fixing our Gemma-3-shaped gemma.yaml; see OPEN_TASKS 7b).
- `anchor_probe.py` / `granularity_probe.py` / `gate_normalization_probe.py` — **the architecture-prompt anchoring trilogy** (2026-07-25). Question: ten greenfield runs across seven model families produced one skeleton (models/loader/parser/[combat]/engine/main) — is `design_architecture.yaml`'s worked example dictating it? VERDICT: **no, on all three counts.** (1) *Filenames* — renaming the example's five files moved nothing: 0/4 adoptions, and 8/8 samples returned `main.py` even where the example said `app.py` in three places including `run_command`. (2) *Granularity* — showing a 2-module vs 9-module vs no-count example spanned only 1.00 module of output (6.88 / 7.88 / 7.88), under the pre-registered 1.5 threshold; note the no-count arm equals the 9-module arm, so the task sets ~7-8 and the example nudges ~1 at the margin. (3) *Gate chain* — `design_reconcile` returned all 8 blueprints byte-identical (Jaccard 0.552→0.552) AND the critique passes 7/8, so in production the reconcile step usually never fires. Measured convergence for scale: production 0.784 vs single-model sampling 0.613 — production is tighter but the same family, so there was less anomaly than the raw eyeball suggested. CAVEAT on (3): reconcile was run without drift facts or `prior_rejection`, which production populates on the rejection path — it shows reconcile doesn't *spontaneously* normalize, not that it never does. Still-open genuine anchor, untouched by these: the hardcoded "Produce 4-7 functional goals" in `mission_actions.py`.
- `spec_bench.py` — speculative-decode A/B. `reasoning_headswap_spike.py` — per-request reasoning head-swap validation.
- `replay_runaway.py` — repetition-guard regression. `refresh_efficacy.py` — in-process context-refresh efficacy.
- `warm_flows.py` — pre-warm per-flow KV. `patch_cache_cfg.py` — idempotent config-field setter.
- `start_model.sh` / `warm_check.sh` / `server_keeper.sh` — server lifecycle (bring-up + KV check + warm; keep-alive).

## Health & memory monitoring
- `contam_monitor.py` — live stub-emission (souring) monitor. `canary_probe.py` — pluggable server-health probes. `marathon_health.py` — per-run yield/souring report. (Classifiers live in `agent/trace_health.py`.)
- `mem_ledger.sh` / `mem_snapshot.sh` / `footprint_sampler.sh` — differential & one-shot memory diagnostics.
- `docker_bounce.sh` — self-gating Docker Desktop restart (image/VM memory reclaim).
- `cli_smoke.py` / `smoke_test.py` — import-rot + compiled-flow smoke harnesses.

## Curator / scholarly ops
- `second_opinion_denials.py` — reflip denials for the gemma second-opinion pass.
- `redownload_unresolved.py` — alternate-repository OA PDF retry.
- `repair_econ.py` — regenerate-vs-diagnose economics from production notes.

## Adaptive-reasoning provenance (router DEMOTED to experimental 2026-07-25)

**`ADAPTIVE_THINKING_STATUS.md` is the current status doc** — failure shape
(3 clamps), live results (blind boss panel 31.7 vs 24.7; router inert since
~07-17), web research (behavior cloning / performative prediction / routing
collapse / Math-Shepherd), and the tree-walk gold-label plan. The archived
decision-layer doc below is the BUILD LOG, not current belief.

Rebuild chain: `build_trusted_set.py` (legacy labels → trusted manifest) →
`build_trusted_trainset.py` (→ train_dataset_trusted_v1.jsonl) →
`train_reasoning_router.py` (→ models/reasoning_router_v1.joblib, gitignored).
Growing the label set: `build_panel_batches_v3.py` + `extract_panel_result.py`
(panel harness under JUDGE_STANDARD), `cf_run_level.py` (counterfactual runner).
Data: `trusted_labels_v1.json` (2,129 gold), `label_quarantine_v1.json`
(panel worklist), `train_dataset_trusted_v1.jsonl`, turn-content sources
(`clean_turns_v3.json`, `*_labeling.json`) + label manifests (`*_labels*.json`,
`grow_*`), `sour_prompts.json` (canary authoring probe).

## Archive
- `archive/docs/` — finalized design docs with closing-status stamps.
- `archive/` (rest) — pre-cleanup archived material.
- `OPENALEX_MIRROR_2026-09-03.md` — OpenAlex API now meters by credit; the CC0 parquet snapshot (784 GB) mirrored to a dedicated USB drive with a local DuckDB title→DOI index; the disk, not the network, is the bottleneck.
- `OPENALEX_MIRROR_SUBSTITUTION_2026-09-04.md` — can the mirror replace the API? 99% DOI coverage; topics recall 64–96% of accepted at 10M–67M pools but do not separate accepted from denied; title terms carry half of what abstract search did; enrichment is a JOIN (`openalex_local_enrich.py`); 228 fresh OA leads.
- `coinage_quarantine.py` — review the pack coinage guard's quarantine (keys a pack coined past 40 new and 2× reused on a mature registry, held out of `key_registry.json`): list / promote-shared / promote / drop / drop-paper, backups beside both files.
- `apply_key_compaction.py` — validate + apply an agent-proposed registry compaction (aliases, quarantine promotions, drops); refuses cross-unit, cross-type, digit-parameterised, min/max and chained merges. Results: `KEY_COMPACTION_2026-09-04.md`.
- `widen_scalar_keys.py` / `apply_key_renames.py` / `apply_suffix_swaps.py` — the 2026-09-04 follow-ups: unify a scalar key and its plural twin onto a shallow list (corpus leaf count asserted as the invariant), rename malformed keys, swap _min/_max values a reviewer confirmed against the paper.
- [DENIAL_REVIEW_2026-09-05.md](DENIAL_REVIEW_2026-09-05.md) — the review prompt contradicted the charter (a peak table IS an accepted form); 68% of muse / 88% of qwen corpus_fit denials name an in-scope technique in their own summary. Fix, re-judgement run, and the bad-brief error that produced `denial_family_guard.py`.
- [expand_local_seat_on_figtext_drain.py](expand_local_seat_on_figtext_drain.py) — waits for figtext to drain, then re-applies the proven 3060 layer-split rung ([40,12] @ 131,072 cells) so local lanes can curate the 100k+ tail beside the remote lane; exact-revert fallback, relaunches the mission with `OUROBOROS_DISABLE_LANES=ocr`. Result in `~/tmp/seat_expansion_result.json`.
- [heal_parked_oversize.py](heal_parked_oversize.py) — the `curate_oversize` park used to append a 3-field row and wipe the paper's extraction record (last-row-wins); this restores each parked paper's full pre-park row from the append-only history and un-parks the ones a seat can now take. Dry-run default.
