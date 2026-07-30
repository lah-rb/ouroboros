#!/bin/bash
# Architecture x cache-mode compatibility matrix (dev/CACHE_STATE.md).
#
# For each representative model: generate a temp config that REQUESTS
# the full resident stack (resident_seq_cache + snapshots), restart the
# server onto it, and run dev/cache_compat_matrix.py — the can_shift
# gate + prefill telemetry then reveal what each architecture actually
# supports. Temp configs are deleted and the production config restored
# at the end. Results: dev/bakeoff_results/cache_matrix.jsonl
set -uo pipefail
cd "$(dirname "$0")/.."
LLMVP=llmvp
OUT=dev/bakeoff_results/cache_matrix.jsonl
# THE SIX CANDIDATES (dev/CACHE_SWEEP_PLAN.md, 2026-07-29). 9 of 19 configs run
# full_replay; prior art already resolves three as correctly-recurrent
# (qwen3.6-27b, qwen3.6-35b-a3 = pure recurrent; step37 = step35 arch), leaving
# these. hy3 first: it is dense, so swa_full is a no-op and total KV is
# unchanged — the only candidate needing no KV re-check.
ROSTER=(hy3-reap-200b-a21 glm-4.7-flash laguna-xs-2.1 gemma-4-26b-a4b mistral-medium-3.5-128b qwen3.5-122b-a10)
# Baseline rows, for comparison against a KNOWN-flat config. Cheap and it is the
# only way to tell "this model cannot" from "the probe changed".
ROSTER+=(gpt-oss-120b-a5)
RESTORE=$(cat $LLMVP/active_config.txt)
mkdir -p "$(dirname "$OUT")"
# APPEND, never truncate: `: > $OUT` destroyed all prior rows on every run,
# which is how a results file becomes a snapshot of only the latest question.
# Each row carries run_id + flags, so mixed vintages stay distinguishable.
RUN_ID="sweep_$(date +%Y%m%d-%H%M%S)"

health_ok(){ curl -s -m5 -X POST http://localhost:8008/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { health { status } }"}' 2>/dev/null | grep -q '"ok"'; }
wait_ready(){ for _ in $(seq 1 90); do health_ok && return 0; sleep 10; done; return 1; }

restart_server(){
  (cd "$LLMVP" && uv run llmvp.py --stop) >/dev/null 2>&1 || true
  sleep 5
  (cd "$LLMVP" && uv run llmvp.py --backend) >/dev/null 2>&1 || true
  wait_ready
}

for cfg in "${ROSTER[@]}"; do
  # Resolve through the SAME search path the server uses (root/boss/experiments/
  # archive) — the hardcoded top-level check silently skipped anything that had
  # been reorganised into a subdir.
  CFG_PATH=$(cd "$LLMVP" && .venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from core.config import resolve_config_path
try: print(resolve_config_path('$cfg'))
except Exception: pass")
  [ -n "$CFG_PATH" ] || { echo "!! cannot resolve $cfg"; continue; }
  echo "== $cfg =="
  CFG_PATH="$CFG_PATH" uv run python - "$cfg" <<'PYEOF'
import sys, yaml
cfg = sys.argv[1]
import os
d = yaml.safe_load(open(os.environ["CFG_PATH"]))
d["model"]["resident_seq_cache"] = True
d["model"]["session_snapshot_max"] = 2
# DELIBERATELY NOT touching swa_full / kv_unified (2026-07-29).
#
# The old `setdefault(..., True)` flipped swa_full ON for any config that left it
# unset — and swa_full RAISES KV on an SWA model. Two of this roster's candidates
# (qwen3.5-122b-a10, mistral-medium-3.5-128b) leave it unset and are among the
# largest configs in the fleet, on a machine that has already hard-rebooted from
# a KV overshoot. Flipping a memory-sizing flag as a side effect of a
# COMPATIBILITY probe is the wrong risk to take.
#
# So this sweep answers exactly one question: can this arch host the resident
# cache AS CONFIGURED? A `false` here may mean "needs swa_full" rather than
# "cannot" — which is why the row records session_can_shift alongside the flags.
# Flipping those flags is a separate, per-model, KV-rechecked step.
if d["model"].get("swa_full") is None:
    d["model"]["swa_full"] = False
if d["model"].get("kv_unified") is None:
    d["model"]["kv_unified"] = False
yaml.safe_dump(d, open(f"llmvp/configs/{cfg}-cachetest.yaml", "w"), sort_keys=False)
PYEOF
  echo "$cfg-cachetest" > $LLMVP/active_config.txt
  if ! restart_server; then echo "!! $cfg failed to start"; rm -f "$LLMVP/configs/$cfg-cachetest.yaml"; continue; fi
  SWA=$(grep -m1 "^  swa_full:" "$CFG_PATH" | awk '{print $2}')
  echo "   flags: swa_full=${SWA:-unset(->false)}"
  uv run python dev/cache_compat_matrix.py --label "$cfg" --run-id "$RUN_ID" | tee -a "$OUT"
  rm -f "$LLMVP/configs/$cfg-cachetest.yaml"
done

echo "$RESTORE" > $LLMVP/active_config.txt
restart_server && echo "production config ($RESTORE) restored"
echo "matrix: $OUT"
