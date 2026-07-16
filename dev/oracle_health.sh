#!/bin/bash
# TB1 GRADER-HEALTH SWEEP — run EVERY reference solution.sh through its own grader
# (terminal-bench --agent oracle). A task whose GOLD solution fails to resolve is
# bit-rotted (unpinned grading deps drifted on PyPI, source builds broke, etc.) and is
# dead weight for capability scoring today. This maps the SCOREABLE CEILING of TB1.
#
# The oracle needs NO inference, so we free the LLM server first to give Docker builds
# full memory (astropy et al. compile from source). DO NOT run while a mission/TB run
# needs the server. Usage: oracle_health.sh [n_concurrent=6]
set -u
ROOT=/Users/lah-rb/Repos/ouroboros; LLMVP=$ROOT/llmvp
NC=${1:-6}; RUNID=oracle-health
LOG=/tmp/oracle_health.log
cd "$ROOT"; export PATH="$HOME/.local/bin:$PATH"; export PYTHONPATH="$ROOT"
: > "$LOG"
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# Free the model (graceful --stop, never pkill) — oracle is Docker-only.
(cd "$LLMVP" && uv run llmvp.py --stop) >>"$LOG" 2>&1; sleep 5
log "launching TB1 oracle sweep (ALL tasks, n=$NC) -> runs/$RUNID"
rm -rf "$ROOT/runs/$RUNID" 2>/dev/null
.venv/bin/tb run -d terminal-bench-core==0.1.1 --agent oracle \
  --n-concurrent "$NC" --run-id "$RUNID" --output-path "$ROOT/runs" >>"$LOG" 2>&1
log "ORACLE SWEEP DONE -> runs/$RUNID  (parse_error/unresolved = bit-rotted grader)"
