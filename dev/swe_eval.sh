#!/bin/bash
# SWE-bench eval wrapper. Two modes:
#   dev/swe_eval.sh --gold RUNID [ids]   — write gold predictions + evaluate;
#                                          MUST resolve 100% (the go/no-go).
#   dev/swe_eval.sh RUNID <preds.jsonl>  — evaluate an existing predictions JSONL.
#
# The official evaluator pulls per-instance images and runs graded containers,
# so it needs Docker (+ Rosetta on Apple silicon — the amd64 verifiers segfault
# under bare QEMU). The LLM server is NOT needed for evaluation (grading is
# deterministic), so it is safe to run alongside a paused mission host.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"; export PATH="$HOME/.local/bin:$PATH"; export PYTHONPATH="$ROOT"
DATASET="princeton-nlp/SWE-bench_Verified"
NW=${SWE_MAX_WORKERS:-4}

if [ "${1:-}" = "--gold" ]; then
  RUNID=${2:-swe-gold}; IDS=${3:-}
  OUT="$ROOT/runs/swe/$RUNID"; PREDS="$OUT/predictions.jsonl"
  echo "[gold-oracle] writing gold predictions -> $PREDS"
  uv run python -m adapters.swe.run_pilot --run-id "$RUNID" --gold \
    ${IDS:+--instances "$IDS"} || exit 1
else
  RUNID=${1:?usage: swe_eval.sh RUNID <preds.jsonl>}; PREDS=${2:?predictions path}
fi

echo "[eval] $PREDS  (run_id=$RUNID, workers=$NW)"
uv run python -m swebench.harness.run_evaluation \
  --dataset_name "$DATASET" --predictions_path "$PREDS" \
  --run_id "$RUNID" --max_workers "$NW"
echo "[eval] report: *.$RUNID.json"
