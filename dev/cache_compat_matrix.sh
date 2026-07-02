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
ROSTER=(gpt-oss-120b-a5 gemma-4-31b devstral-2-small-24b qwen3-next-coder-80b-a3 qwen3.6-27b)
RESTORE=$(cat $LLMVP/active_config.txt)
: > "$OUT"

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
  [ -f "$LLMVP/configs/$cfg.yaml" ] || { echo "!! no $cfg.yaml"; continue; }
  echo "== $cfg =="
  uv run python - "$cfg" <<'PYEOF'
import sys, yaml
cfg = sys.argv[1]
d = yaml.safe_load(open(f"llmvp/configs/{cfg}.yaml"))
d["model"]["resident_seq_cache"] = True
d["model"]["session_snapshot_max"] = 2
d["model"].setdefault("swa_full", True)
d["model"].setdefault("kv_unified", True)
yaml.safe_dump(d, open(f"llmvp/configs/{cfg}-cachetest.yaml", "w"), sort_keys=False)
PYEOF
  echo "$cfg-cachetest" > $LLMVP/active_config.txt
  if ! restart_server; then echo "!! $cfg failed to start"; rm -f "$LLMVP/configs/$cfg-cachetest.yaml"; continue; fi
  uv run python dev/cache_compat_matrix.py --label "$cfg" | tee -a "$OUT"
  rm -f "$LLMVP/configs/$cfg-cachetest.yaml"
done

echo "$RESTORE" > $LLMVP/active_config.txt
restart_server && echo "production config ($RESTORE) restored"
echo "matrix: $OUT"
