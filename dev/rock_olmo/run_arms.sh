#!/usr/bin/env bash
# Frozen arms on CPU (32 cores, and the arm is an MLP once 1,952 prompts are
# embedded); the contended 3060 is left to the LoRA arm. Full logs to files —
# a grep in the pipeline once hid an argparse failure as silence.
set -u
cd "$(dirname "$0")"
PY=./.venv/bin/python
LOG=/tmp/claude-1000/-home-lah-rb-Repos-ouroboros/89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad
for SEED in 20260824 7 1337; do
  echo "===== FROZEN seed=$SEED (cpu) ====="
  $PY train_spectra_head.py --epochs 40 --seed "$SEED" --device cpu \
      --tag="-s$SEED" > "$LOG/frozen_s$SEED.log" 2>&1
  grep -E 'best val|last-10' "$LOG/frozen_s$SEED.log" || tail -5 "$LOG/frozen_s$SEED.log"
  echo "----- retrieval control, same split -----"
  $PY retrieval_baseline.py --seed "$SEED" 2>&1 | grep -E 'k=5|reference'
done
echo "===== DONE ====="
