#!/usr/bin/env bash
# Prompt-content ablation. New filename rather than an edit of run_arms.sh:
# bash reads scripts incrementally and overwriting one mid-run corrupted the
# last driver at line 18.
#
# 'none' is the negative control -- every prompt identical, so a score above
# trivial there means a leak and voids the arm.
set -u
cd "$(dirname "$0")"
PY=./.venv/bin/python
LOG=/tmp/claude-1000/-home-lah-rb-Repos-ouroboros/89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad
for SEED in 20260824 7; do
  for MODE in both name formula none; do
    echo "===== seed=$SEED mode=$MODE ====="
    $PY train_spectra_head.py --epochs 40 --seed "$SEED" --device cpu \
        --prompt-mode "$MODE" --tag="-abl-$MODE-s$SEED" \
        > "$LOG/abl_${MODE}_s${SEED}.log" 2>&1
    grep -aE 'last-10|NEGATIVE' "$LOG/abl_${MODE}_s${SEED}.log" \
      || { echo "FAILED:"; tail -6 "$LOG/abl_${MODE}_s${SEED}.log"; }
  done
done
echo "===== ABLATION DONE ====="
