#!/bin/bash
# Decode-scaling sweep: pool sizes 1..4, per-instance + aggregate decode tok/s.
# Results: dev/decode_scaling.csv. Restores production a5 at the end.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
YAML="$ROOT/llmvp/configs/gpt-oss-120b-a5-bench.yaml"
CSV="$ROOT/dev/decode_scaling.csv"
[ -f "$CSV" ] || echo "n,rep,errors,per_instance_tps,aggregate_tps,total_tokens,concurrent_wall_s" > "$CSV"

for N in 1 2 3 4; do
  echo "════ pool size $N ════"
  /usr/bin/sed -i '' -E "s/^  max_concurrent_requests: [0-9]+/  max_concurrent_requests: $N/" "$YAML"
  pkill -TERM -f api/main.py 2>/dev/null; sleep 8
  printf 'gpt-oss-120b-a5-bench' > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py >> logs/llmvp_server.log 2>&1 & )
  ok=""
  for i in $(seq 1 90); do
    curl -s -m 3 -X POST http://127.0.0.1:8008/graphql -H 'Content-Type: application/json' \
      -d '{"query":"query{health{availableInstances}}"}' 2>/dev/null | grep -q "\"availableInstances\": $N" && { ok=1; break; }
    pgrep -f api/main.py >/dev/null || break
    sleep 5
  done
  if [ -z "$ok" ]; then echo "$N,-,BOOT_FAIL,,,," >> "$CSV"; continue; fi
  "$ROOT/llmvp/.venv/bin/python" "$ROOT/dev/decode_scaling_bench.py" "$N" 2 | while read -r line; do
    echo "$line" | python3 -c "
import json,sys
r=json.loads(sys.stdin.read())
print(f\"{r['n']},{r['rep']},{r['errors']},{r['per_instance_tps']},{r['aggregate_tps']},{r['total_tokens']},{r['concurrent_wall_s']}\")"
  done >> "$CSV"
  tail -2 "$CSV"
done

pkill -TERM -f api/main.py 2>/dev/null; sleep 8
printf 'gpt-oss-120b-a5' > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py >> logs/llmvp_server.log 2>&1 & )
echo "════ sweep done — a5 restoring ════"
column -t -s, "$CSV"
