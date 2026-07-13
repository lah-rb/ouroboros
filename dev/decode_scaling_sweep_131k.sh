#!/bin/bash
# Batched capacity sweep at FULL 131k context: how many concurrent streams
# (working seats) does the single-context engine carry, and what does each
# cost? W = 1,2,4,8,16 seats sharing ONE 131k unified cell pool. Per point:
# per-stream + aggregate decode tok/s, errors, and wired-memory sample
# (seats are seq ids — expect wired ~flat across W, unlike pool contexts
# at ~9.2G each). Results: dev/decode_scaling_131k.csv. Restores a5 after.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
YAML="$ROOT/llmvp/configs/gpt-oss-120b-a5-bench-batched.yaml"
CSV="$ROOT/dev/decode_scaling_131k.csv"
[ -f "$CSV" ] || echo "n,rep,errors,per_instance_tps,aggregate_tps,total_tokens,concurrent_wall_s,wired_gb" > "$CSV"

wired_gb() {
  vm_stat | awk '/wired/ {printf "%.1f", $NF * 16384 / 1e9}'
}

for N in 1 2 4 8 16; do
  echo "════ seats $N (131k shared ctx) ════"
  /usr/bin/sed -i '' -E "s/^  max_concurrent_requests: [0-9]+/  max_concurrent_requests: $N/" "$YAML"
  pkill -TERM -f api/main.py 2>/dev/null; sleep 8
  printf 'gpt-oss-120b-a5-bench-batched' > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py >> logs/llmvp_server.log 2>&1 & )
  ok=""
  for i in $(seq 1 90); do
    curl -s -m 3 -X POST http://127.0.0.1:8008/graphql -H 'Content-Type: application/json' \
      -d '{"query":"query{health{availableInstances}}"}' 2>/dev/null | grep -q "\"availableInstances\": $N" && { ok=1; break; }
    pgrep -f api/main.py >/dev/null || break
    sleep 5
  done
  if [ -z "$ok" ]; then echo "$N,-,BOOT_FAIL,,,,," >> "$CSV"; continue; fi
  W=$(wired_gb)
  "$ROOT/llmvp/.venv/bin/python" "$ROOT/dev/decode_scaling_bench.py" "$N" 2 | while read -r line; do
    echo "$line" | python3 -c "
import json,sys
r=json.loads(sys.stdin.read())
print(f\"{r['n']},{r['rep']},{r['errors']},{r['per_instance_tps']},{r['aggregate_tps']},{r['total_tokens']},{r['concurrent_wall_s']},$W\")"
  done >> "$CSV"
  tail -2 "$CSV"
done

pkill -TERM -f api/main.py 2>/dev/null; sleep 8
printf 'gpt-oss-120b-a5' > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py >> logs/llmvp_server.log 2>&1 & )
echo "════ sweep done — a5 restoring ════"
column -t -s, "$CSV"
