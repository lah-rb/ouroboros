#!/bin/bash
# Curator text bake-off across the model roster: restart the server per
# config (graceful --stop, never pkill — the pool leaks on hard-kill),
# run dev/bakeoff_text.py, restore the production config at the end.
#
# Usage: bash dev/bakeoff_run_all.sh [databank]
set -uo pipefail
cd "$(dirname "$0")/.."
LLMVP=llmvp
DATABANK="${1:-$HOME/corpora/ouroboros-hea/databank}"
ROSTER=(gemma-4-31b qwen3.6-27b devstral-2-small-24b gpt-oss-120b-a5 qwen3-next-coder-80b-a3)
RESTORE=$(cat $LLMVP/active_config.txt)

health_ok(){ curl -s -m5 -X POST http://localhost:8008/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { health { status } }"}' 2>/dev/null | grep -q '"ok"'; }
wait_ready(){ for _ in $(seq 1 180); do health_ok && return 0; sleep 10; done; return 1; }

restart_server(){
  (cd "$LLMVP" && uv run llmvp.py --stop) || true
  sleep 5
  (cd "$LLMVP" && uv run llmvp.py --backend) || true
  wait_ready
}

for cfg in "${ROSTER[@]}"; do
  if [ ! -f "$LLMVP/configs/$cfg.yaml" ]; then
    echo "!! no config $cfg.yaml — skipping"; continue
  fi
  echo "== $cfg =="
  echo "$cfg" > $LLMVP/active_config.txt
  if ! restart_server; then echo "!! $cfg failed to start — skipping"; continue; fi
  uv run python dev/bakeoff_text.py --databank "$DATABANK" --label "$cfg" \
    || echo "!! bakeoff failed on $cfg"
done

echo "$RESTORE" > $LLMVP/active_config.txt
restart_server && echo "production config ($RESTORE) restored"
echo "results: dev/bakeoff_results/summary.md"
