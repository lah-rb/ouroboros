#!/bin/bash
# Launch the LLMVP context-ceiling probe, detached, from anywhere.
#
#   bash dev/probe_ctx.sh <config>[,<config>...] [extra --probe-* flags]
#   bash dev/probe_ctx.sh active
#
# WHY A WRAPPER FOR A ONE-LINE COMMAND. `api/main.py` resolves ./configs,
# ./data and ./logs relative to the CWD, so it must be started from llmvp/.
# Every ad-hoc invocation of the form
#
#     daemonize.py LOG bash -c "exec .venv/bin/python api/main.py --probe-context X"
#
# inherits the caller's directory instead, fails with
# "can't open file '.../api/main.py'", and — because it is detached — reports
# success while writing that error to a log nobody reads. That happened three
# times on 2026-07-29, and twice it was mistaken for a probe that had started.
#
# So: the cd lives here, the launch is verified before returning, and a failure
# is a non-zero exit rather than a silent one.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp

[ $# -ge 1 ] || { echo "usage: bash dev/probe_ctx.sh <configs> [--probe-* flags]"; exit 2; }
CONFIGS=$1; shift

if pgrep -f "[p]robe-context" >/dev/null; then
  echo "a context probe is already running:"; pgrep -fl "[p]robe-context" | cut -c1-100
  exit 1
fi
if pgrep -f "[a]pi/main.py" >/dev/null; then
  echo "an LLMVP server is up — the probe boots and stops its own; stop it first:"
  pgrep -fl "[a]pi/main.py" | cut -c1-100
  exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
LOG=$HOME/ouroboros-runs/ctx_sweep_console_$STAMP.log
python3 "$ROOT/dev/daemonize.py" "$LOG" \
  bash -c "cd '$LLMVP' && exec .venv/bin/python api/main.py --probe-context '$CONFIGS' $*"

# VERIFY, do not assume. A detached launch that dies immediately still returns 0.
for _ in $(seq 1 15); do
  if pgrep -f "[p]robe-context" >/dev/null; then
    echo "probe running: $(pgrep -fl '[p]robe-context' | head -1 | cut -c1-90)"
    echo "console: $LOG"
    exit 0
  fi
  sleep 1
done
echo "PROBE DID NOT START — console tail:"
tail -5 "$LOG" 2>/dev/null
exit 1
