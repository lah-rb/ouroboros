#!/usr/bin/env bash
# Does the run-2 corpus adapter improve the BACKBONE's representation for
# the spectrum regression task? Separate question from generation quality.
#
# 'name' is the most interesting mode: on the base backbone the mineral
# name scored BELOW a constant-prompt control (0.4101 vs 0.4599). If LM
# adaptation on 14M tokens of mineral text made the name informative, it
# shows up there first.
set -u
cd "$(dirname "$0")"
PY=./.venv/bin/python
LOG=/tmp/claude-1000/-home-lah-rb-Repos-ouroboros/89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad
ADAPTER=~/models/olmo2-1b-spectra-lora
for MODE in formula both name; do
  for ARM in base adapted; do
    EXTRA=""
    [ "$ARM" = "adapted" ] && EXTRA="--lm-adapter $ADAPTER"
    echo "===== mode=$MODE backbone=$ARM ====="
    for SEED in 20260824 7 1337 42 2024; do
      $PY train_spectra_head.py --epochs 40 --device cpu --prompt-mode "$MODE" \
          --seed "$SEED" --tag="-adp-$ARM-$MODE-s$SEED" $EXTRA \
          > "$LOG/adp_${ARM}_${MODE}_s${SEED}.log" 2>&1
      grep -aE 'last-10' "$LOG/adp_${ARM}_${MODE}_s${SEED}.log" \
        || { echo "  seed $SEED FAILED:"; tail -4 "$LOG/adp_${ARM}_${MODE}_s${SEED}.log"; }
    done
  done
done
echo "===== ADAPTER ARMS DONE ====="
