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

## LLMVP serving acceptance & perf
- `batched_parity.py` — batched-decode determinism/isolation parity. `duo_soak.py` — multi-seat soak + latch-heal.
- `snapshot_stress.py` — snapshot-tier acceptance. `cache_strategy_stress.py` / `cache_compat_matrix.{py,sh}` — KV strategy & per-model compat.
- `decode_scaling_bench.py` / `swarm_3proc_bench.py` / `jit_exercise_131k.py` — throughput & pool-lifecycle benches.
- `ctx_decode_probe.py` — pool-beyond-trained capacity probe (needle+arithmetic at depth × N streams; proved 200k live cells on a 224k pool).
- `serving_perf_reference.md` — **measured failure edges & design rules** (2026-07-20/21): decode-vs-N, prefill-vs-size, shared-pool wedge zone, worker budget sizing. Read before sizing any fan-out experiment.
- `plot_swarm_perf.py` — render a swarm fan-out's `.agent/swarm_perf.jsonl` (active streams, per-symbol gantt, aggregate tok/s over time) + per-symbol actuals summary.
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

## Adaptive-reasoning provenance (shipped router)
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
